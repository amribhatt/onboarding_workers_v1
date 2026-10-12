"""The engine's vocabulary: the job spec, the evaluation and what it holds, the two errors, the canonical form.

No stage logic here; `worker.engine` re-exports the public names."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, get_args

from worker.money import ZERO, cents, to_decimal
from worker.pipeline import STAGES
from worker.spool import page, read

ENGINE_VERSION = "engine-1"
ChoiceKind = Literal["map_column", "transform", "supply", "declare_missing", "exclude", "correct_id", "map_account",
                     "accept_flag", "set_rule_option"]
CHOICE_KINDS = get_args(ChoiceKind)
STEP_STAGE = {
    "lift": "read", "lift_blocks": "read", "flatten_compact": "read", "lift_gl_lines": "read", "join": "read",
    "distinct": "read", "resolve_references": "map", "map_accounts": "map", "transform": "convert",
    "match_existing": "convert", "assign_ids": "convert", "check_data_quality": "check", "reconcile": "check",
    "write": "write",
    # the console family (`worker.engine_console`): the job the person's console holds, read, then decided whole
    "console_read": "read", "console_check": "check",
}
_CHOICE_STAGE = {"map_column": "read", "map_account": "map", "transform": "convert", "supply": "convert",
                 "declare_missing": "convert", "exclude": "convert", "correct_id": "convert",
                 "accept_flag": "check", "set_rule_option": "check"}


class Refused(Exception):
    """The engine will not run or write this, and says why in plain words. Raised before any effect."""

    def __init__(self, code: str, sentence: str) -> None:
        super().__init__(f"{code}: {sentence}")
        self.code = code
        self.sentence = sentence


class StageFailed(Exception):
    """An infrastructure fault that outlasted its retries. The stages sealed so far stay sealed: run again."""

    def __init__(self, stage: str, detail: str = "") -> None:
        super().__init__(f"stage {stage} failed" + (f": {detail}" if detail else ""))
        self.stage = stage
        self.detail = detail


_OMIT_WHEN_EMPTY = frozenset({"field_recipes", "derived_sources"})  # a field a job without it must not show (its digests stay as were)


def plain(x: Any) -> Any:
    if is_dataclass(x) and not isinstance(x, type):
        return {f.name: plain(getattr(x, f.name)) for f in fields(x)
                if not (f.name in _OMIT_WHEN_EMPTY and not getattr(x, f.name))}
    if isinstance(x, Mapping):
        return {str(k): plain(v) for k, v in sorted(x.items(), key=lambda kv: str(kv[0]))}
    if isinstance(x, list | tuple):
        return [plain(v) for v in x]
    if isinstance(x, set | frozenset):
        return sorted((plain(v) for v in x), key=str)
    if x is None or isinstance(x, bool | int | float | str):
        return x
    return str(x)


def canonical(x: Any) -> str:
    return json.dumps(plain(x), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(*parts: Any) -> str:
    return hashlib.sha256(canonical(list(parts)).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SourceRef:
    kind: Literal["file", "evaluation", "derived"]
    ref: str  # inbox-relative path, an eval_key, or (derived) the sha256 of an approved layout program's table
    sheet: str | None = None
    header_row: int | None = None
    fingerprint: str = ""  # sha256 of the file bytes; a mismatch is Refused("source_changed"); "" = not pinned


@dataclass(frozen=True)
class PinSet:
    """What is in force, fixed at job start. Left empty it is filled from what the loader finds now and recorded
    in the manifest; given, it must equal what is on disk, or the job is refused (a promotion mid-job never
    changes it)."""

    skills: Mapping[str, str] = field(default_factory=dict)
    target: str = ""
    controls: str = ""
    golden_coa: str = ""
    engine: str = ""
    schema_versions: Mapping[str, int] = field(default_factory=dict)
    models: Mapping[str, str] = field(default_factory=dict)  # role -> model id, for the roles used


@dataclass(frozen=True)
class Choice:
    kind: ChoiceKind
    subject: str  # "row:14|Commitment_Date", "src:Sheet!14", "field:Close_Date", "key:|1000|Expense", ...
    value: Any
    scope: Literal["job", "sponsor"]
    by: str
    reason: str


@dataclass(frozen=True)
class Recipe:
    """The steps, as data. Each binding is `{"step": <name>, ...}`; see `STEP_STAGE` for the closed list."""

    checksum: str = ""
    bindings: tuple[Mapping[str, Any], ...] = ()

    @staticmethod
    def of(*bindings: Mapping[str, Any]) -> Recipe:
        return Recipe(sha(list(bindings)), tuple(dict(b) for b in bindings))


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    sponsor: str
    record_type: str | None
    entity: str | None
    sources: tuple[SourceRef, ...]
    pins: PinSet
    recipe: Recipe | None
    choices: tuple[Choice, ...] = ()
    context: Mapping[str, str] = field(default_factory=dict)  # gp_id, fund, period, trace_id (not in eval_key)

    def _body(self) -> dict[str, Any]:
        ctx = {k: v for k, v in self.context.items() if k != "trace_id"}
        return {"job_id": self.job_id, "sponsor": self.sponsor, "record_type": self.record_type,
                "entity": self.entity, "sources": self.sources, "pins": self.pins, "recipe": self.recipe,
                "choices": self.choices, "context": ctx}

    def eval_key(self) -> str:
        return sha("eval", self._body())

    def stage_key(self, stage: str, approval: Approval | None = None) -> str:
        """Only the inputs that stage depends on, chained on the stages before it: a changed fill re-runs convert
        and check, not read and map."""
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        ctx = {k: v for k, v in self.context.items() if k != "trace_id"}
        steps = list(self.recipe.bindings) if self.recipe else []

        def mine(name: str) -> list[Any]:
            return [dict(b) for b in steps if STEP_STAGE.get(str(b.get("step"))) == name]

        def chosen(name: str) -> list[Choice]:
            return [c for c in self.choices if _CHOICE_STAGE.get(c.kind) == name]

        key = sha("read", self.record_type, self.entity, self.sources, ctx, mine("read"), chosen("read"),
                  self.pins.engine)
        if stage == "read":
            return key
        key = sha("map", key, mine("map"), chosen("map"), self.pins.skills.get("map-accounts"), self.pins.golden_coa)
        if stage == "map":
            return key
        key = sha("convert", key, mine("convert"), chosen("convert"), self.pins.target,
                  self.pins.skills.get("build-static-records"))
        if stage == "convert":
            return key
        key = sha("check", key, mine("check"), chosen("check"), self.pins.skills, self.pins.controls,
                  self.pins.schema_versions, self.pins.models)
        if stage == "check":
            return key
        return sha("write", key, mine("write"), approval.approval_id if approval else "")


@dataclass(frozen=True)
class Approval:
    """Built by the gate (the console's Approve button, `interrupt_on`), never by the engine."""

    approval_id: str
    by: str
    eval_key: str
    findings_digest: str
    acknowledged: tuple[str, ...] = ()
    at: str = ""


@dataclass(frozen=True)
class RuleRef:
    rule_id: str
    file: str
    skill: str
    pin: str
    control: bool


@dataclass(frozen=True)
class Finding:
    id: str
    rule: RuleRef
    severity: str
    family: str
    field: str
    message: str
    count: int
    ranges_text: str
    locator: Mapping[str, str]
    sample: tuple[int, ...]
    options: tuple[str, ...]
    detail: Mapping[str, Any] = field(default_factory=dict)  # what the producing function returned, whole


@dataclass(frozen=True)
class Missing:
    field: str
    rows: int
    reason: Literal["no_source", "blank_in_source", "declared_missing", "unsupplied"]


@dataclass(frozen=True)
class RowOutcome:
    i: int
    src_row: int
    status: Literal["ok", "warn", "err", "skip"]
    why: str
    finding_ids: tuple[str, ...]
    dest: Mapping[str, Any] | None
    detail: Mapping[str, Any] | None = None  # the console family's row facts (flags, hit, action choices, line)


@dataclass(frozen=True)
class TieOut:
    rows_read: int
    rows_out: int
    rows_skipped: int
    ties: bool
    debits: Decimal | None
    credits: Decimal | None
    net: Decimal | None
    journals: int | None

    @classmethod
    def of(cls, t: Mapping[str, Any]) -> TieOut:
        """The tie-out as the sealed check and write payloads keep it (amounts as text)."""
        def amount(k: str) -> Decimal | None:
            return Decimal(t[k]) if t[k] is not None else None

        return cls(int(t["rows_read"]), int(t["rows_out"]), int(t["rows_skipped"]), bool(t["ties"]),
                   amount("debits"), amount("credits"), amount("net"), t["journals"])


@dataclass(frozen=True)
class Manifest:
    eval_key: str
    pins: PinSet
    sources: tuple[SourceRef, ...]
    recipe_checksum: str | None
    choices_checksum: str
    model: str | None
    spool_checksum: str
    stage_keys: Mapping[str, str] = field(default_factory=dict)
    prompts: Mapping[str, str] = field(default_factory=dict)  # role -> prompt checksum, for the roles used
    replay_record: Mapping[str, str] = field(default_factory=dict)  # {ref, record_sha256} once the record is written
    field_recipes: Mapping[str, str] = field(default_factory=dict)  # record type -> pin of the sponsor's recipe applied
    derived_sources: tuple[Mapping[str, Any], ...] = ()  # approved layout programs behind `derived` sources

    def as_record(self) -> dict[str, Any]:
        """What rebuilds this job and what was in force, as plain JSON: the pins (skills, target, controls hash,
        Golden chart, engine version, schema versions, model and prompt checksum per role), the sources, the recipe,
        the choices and, after a write, the replay record (its reference and checksum)."""
        pins = self.pins
        out = {"eval_key": self.eval_key, "engine": pins.engine, "controls": pins.controls,
               "schema_versions": dict(pins.schema_versions), "models": dict(pins.models),
               "prompts": dict(self.prompts),
               "pins": plain(pins), "sources": [{"kind": s.kind, "ref": s.ref, "sheet": s.sheet,
                                                  "header_row": s.header_row, "fingerprint": s.fingerprint}
                                                 for s in self.sources],
               "recipe_checksum": self.recipe_checksum, "choices_checksum": self.choices_checksum,
               "model": self.model, "spool_checksum": self.spool_checksum, "stage_keys": dict(self.stage_keys),
               "replay_record": dict(self.replay_record)}
        if self.field_recipes:  # only when the sponsor has a recipe in force: a job without one is as it was
            out["field_recipes"] = dict(self.field_recipes)
        if self.derived_sources:  # only when a source came from a layout program (flex, ticket 11)
            out["derived_sources"] = [dict(d) for d in self.derived_sources]
        return out


@dataclass(frozen=True)
class Produced:
    path: Path
    sha256: str
    rows: int
    ids_issued: tuple[str, ...]
    approval_id: str


def findings_digest(findings: Iterable[Finding]) -> str:
    """The hash of the findings a person saw: what the gate puts in `Approval.findings_digest`."""
    return sha("findings", sorted((f.id, f.severity, f.count) for f in findings))


@dataclass(frozen=True)
class Evaluation:
    outcome: Literal["complete", "needs_human", "stopped"]
    reached: str
    tally: Mapping[str, int]
    findings: tuple[Finding, ...]
    missing: tuple[Missing, ...]
    tie_out: TieOut | None
    proposals: tuple[Mapping[str, Any], ...]
    manifest: Manifest
    produced: Produced | None
    audit_seqs: tuple[int, ...]
    profile: Any = None  # contracts.SourceProfile, filled when stop_after="read"
    header: tuple[str, ...] = ()  # the template's fields, in upload order
    source_facts: tuple[Mapping[str, Any], ...] = ()  # what list_sheets and look show of each source
    reconcile: Mapping[str, Any] | None = None  # the reconcile step's result, when the recipe has one
    reconciliation: Mapping[str, Any] | None = None  # input = output + left out + dropped (`review.reconciliation`)
    reused: tuple[str, ...] = ()
    facts: Mapping[str, Any] = field(default_factory=dict)  # the console family's aggregates (journals, IDs, sheet)
    _store: Any = field(default=None, repr=False, compare=False)
    _table_keys: tuple[str, ...] = field(default=(), repr=False, compare=False)
    _outcome_key: str = field(default="", repr=False, compare=False)

    @property
    def findings_digest(self) -> str:
        return findings_digest(self.findings)

    def rows(self, status: str | None = None, offset: int = 0, limit: int = 200, *,
             written: bool = True) -> Iterator[RowOutcome]:
        """Row outcomes a page at a time from the spool; no caller holds the table. `written=False` leaves out the
        template row each outcome points at (a caller that reads only the detail does not decode the table)."""
        if self._store is None or not self._outcome_key:
            return
        want = (lambda r: r["status"] == status) if status else None
        dest = _DestReader(self._store, self._table_keys)
        for rec in page(self._store, self._outcome_key, offset, limit, want):
            t = rec.get("t")
            row = dest.at(t) if (written and t is not None and rec["status"] in ("ok", "warn")) else None
            yield RowOutcome(rec["i"], rec["src_row"], rec["status"], rec["why"], tuple(rec["finding_ids"]), row,
                             rec.get("detail"))

    def staged(self, offset: int = 0, limit: int = 200) -> Iterator[dict[str, Any]]:
        """The staged rows as they would be written, whatever their status, a page at a time from the spool."""
        if self._store is None or limit <= 0:
            return
        from itertools import islice

        yield from islice(_table_rows(self._store, self._table_keys), offset, offset + limit)

    def digest(self, max_chars: int = 6000) -> str:
        """The capped text an agent tool returns: outcome, tally, tie-out, one line per cause, what is MISSING."""
        lines = [f"outcome: {self.outcome}; reached: {self.reached}; eval {self.manifest.eval_key[:12]}",
                 "rows: " + ", ".join(f"{k} {v}" for k, v in self.tally.items())]
        if self.tie_out is not None:
            t = self.tie_out
            lines.append(f"tie-out: read {t.rows_read}, out {t.rows_out}, skipped {t.rows_skipped}, "
                         f"ties {'yes' if t.ties else 'NO'}"
                         + (f", debits {t.debits}, credits {t.credits}, net {t.net}, journals {t.journals}"
                            if t.debits is not None else ""))
        for f in self.findings:
            where = f" at {f.ranges_text}" if f.ranges_text else ""
            lines.append(f"{f.severity} {f.rule.rule_id} [{f.field}] x{f.count}{where}: {f.message[:160]}")
        gaps = [m for m in self.missing if m.reason != "blank_in_source"]
        if gaps:
            lines.append("MISSING: " + ", ".join(m.field for m in gaps))
        if self.produced is not None:
            lines.append(f"written: {self.produced.path.name} ({self.produced.rows} rows) "
                         f"sha256 {self.produced.sha256[:12]}")
        text = "\n".join(lines)
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 45] + "\n...[truncated to fit the response cap]"


def _table_rows(store: Any, keys: Sequence[str]) -> Iterator[Any]:
    for k in keys:
        yield from read(store, k)


class _DestReader:
    """Reads staged rows from the table spool in step with the outcomes (both are in table order)."""

    def __init__(self, store: Any, keys: Sequence[str]) -> None:
        self.store = store
        self.keys = list(keys)
        self._it: Iterator[Any] | None = None
        self._next = 0

    def at(self, t: int) -> dict[str, Any] | None:
        if self._it is None or t < self._next:
            self._it = _table_rows(self.store, self.keys)
            self._next = 0
        row = None
        while self._next <= t:
            row = next(self._it, None)
            self._next += 1
            if row is None:
                return None
        return row


def src_row(src: str) -> int:
    """The row number in a source address such as `Sheet!14`; 0 when there is none."""
    m = re.search(r"!(\d+)", str(src))
    return int(m.group(1)) if m else 0


def is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


class Totals:
    """What passes through a file: its rows, the debits, credits and net of its amounts, the journals it names."""

    def __init__(self) -> None:
        self.rows = 0
        self.debits = self.credits = self.net = ZERO
        self.journals: set[str] = set()

    def add(self, amount: Any) -> None:
        a = to_decimal(amount)
        self.net += a
        if a > 0:
            self.debits += a
        else:
            self.credits += -a

    def tie(self, rows_read: int, rows_out: int, skipped: int, ties: bool, gl: bool) -> dict[str, Any]:
        """The tie-out as the sealed check keeps it (amounts as text; the money is a GL's only)."""
        return {"rows_read": rows_read, "rows_out": rows_out, "rows_skipped": skipped, "ties": ties,
                "debits": str(cents(self.debits)) if gl else None, "credits": str(cents(self.credits)) if gl else None,
                "net": str(cents(self.net)) if gl else None, "journals": len(self.journals) if gl else None}
