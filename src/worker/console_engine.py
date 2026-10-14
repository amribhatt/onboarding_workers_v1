"""The console's adapter to the one engine (ticket 03 D9): translate, call `evaluate`, keep a handle.

The console decides no row. `to_spec(c, j)` copies a console job (its plan, rules, the person's decisions, fills and
corrections) into a `JobSpec`; `evaluation(c, j)` calls `engine.evaluate` and keeps the result with its `eval_key`
(on the job as `j["eval_key"]`); the views page `Evaluation.rows()` through `static_rows` and `gl_eval`, which give
the rows and lines the screens have always read. Approval builds an `Approval` and calls
`evaluate(stop_after="write")`, so the engine writes the file: `.part`, the audit record, the rename.

Three small things live here and nowhere else: `ConsoleWorld` (the engine's `World` over the console's state, the
sponsor's records in the one registry, the Required? choices, the ID counters), `ConsoleAudit` (the engine's
records go to `console_out/engine.audit.jsonl`; the one `write_upload` record the job's own `audit.jsonl` has always
held is written, in its own shape, before the file is let through) and `CachedSkills` (the pinned rule files are
loaded again only when one of them changes).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from worker import console_demo as D
from worker import engine as E
from worker import engine_console as EC
from worker import persist
from worker import record_facts as RF
from worker import skills as K
from worker import templates as T
from worker.audit import AuditError, AuditLog
from worker.glio import GlRows, golden_account
from worker.ports import FileReader, LoadedRules, MemoryStore, Ports, ProductionSkills

WHO = "analyst"  # the console's one named person (the audit's `who`); never an agent role
REASON = "human approval; the AI prepared it"
CHOICE_REASON = "decision made in the console, recorded in the job's audit"
EC_CORRECT = re.compile(r"^corrected ID to (.+)$")  # the decision text that corrects an ID
ENGINE_LOG = "engine.audit.jsonl"


# ---- the job as a spec ------------------------------------------------------------------------------------
def _plan_columns(plan: Sequence[Sequence[Any]], record_type: str) -> dict[str, str]:
    """Which source columns hold the key, the name, the entity and the commitment's amount and date: the plan's
    own sources (the column of the key field, of the `_Name` field, ...)."""
    real = [f for f in plan if f[1] and f[1] not in ("generated",) and not str(f[1]).startswith(("constant", "("))]
    key = next((f[1] for f in real if str(f[0]).endswith(("_ID", "_Key"))), "")
    name = next((f[1] for f in plan if str(f[0]).endswith("_Name")), "")
    by = {f[0]: f[1] for f in plan}
    out = {"key_col": key, "name_col": name, "ent_col": D.ENTITY_COLUMN.get(record_type, "")}
    if record_type == "Commitments":
        out["ent_col"] = by.get("Fund_ID") or out["ent_col"]
        out["amount_col"] = by.get("Commitment_Amount", "")
        out["date_col"] = by.get("Commitment_Date", "")
    return out


def source_id_column(fields: Sequence[Sequence[Any]], record_type: str) -> str:
    """The file's column that holds the ID the source gave the record ("" when there is none): the plan's
    `Source_*_Key` field, else the field of the target's key. A generated, constant or classified field is not one."""
    key = RF.target(record_type).key[-1]
    f = next((f for f in fields if str(f[0]).startswith("Source_") and str(f[0]).endswith("_Key")), None) \
        or next((f for f in fields if f[0] == key), None)
    src = str(f[1]) if f else ""
    return "" if src in ("generated",) or src.startswith(("constant", "(")) else src


def _choices(j: Mapping[str, Any]) -> tuple[E.Choice, ...]:
    gl = RF.is_gl(j["type"])
    unit = "journal" if gl else "row"
    out: list[E.Choice] = []

    def add(kind: str, subject: str, value: Any) -> None:
        out.append(E.Choice(kind, subject, value, "job", WHO, CHOICE_REASON))  # type: ignore[arg-type]

    for key, text in j.get("dec", {}).items():
        m = EC_CORRECT.match(str(text))
        if str(text).startswith("exclude"):
            add("exclude", f"{unit}:{key}", str(text))
        elif m:
            add("correct_id", f"{unit}:{key}", m.group(1))
        else:
            add("accept_flag", f"decision:{key}", str(text))
    for key in j.get("qa", {}):
        add("accept_flag", f"qa:{key}", 1)
    for key, value in j.get("fill", {}).items():
        row, _, name = str(key).partition("|")
        add("supply", f"row:{row}|{name}", str(value))
    for row, value in j.get("dates", {}).items():
        add("supply", f"date:{row}", str(value))
    for acct, value in j.get("dealfill", {}).items():
        add("supply", f"deal:{acct}", str(value))
    for acct, golden in j.get("acct", {}).items():
        add("map_account", f"source:{acct}", str(golden))
    for name, header in j.get("map", {}).items():
        add("map_column", f"field:{name}", str(header))
    for name, step in (j.get("steps") or {}).items():  # a field step decided for this job only (M24a)
        add("transform", f"field:{name}", step)
    return tuple(sorted(out, key=lambda c: (c.kind, c.subject)))


# ---- the sponsor's field recipe ---------------------------------------------------------------------------
def recipe_doc(c: Any, j: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """(the sponsor's recipe for this job's record type, its pin), or (None, None). A static type only. A recipe
    file that no longer validates raises `EvaluationError`: a damaged recipe is loud, never skipped."""
    from worker.recipes import RecipeChangeRefused

    if RF.is_gl(str(j["type"])) or not hasattr(c, "field_recipes"):
        return None, None
    fr = c.field_recipes(j["sp"])
    rt = str(j["type"])
    pin = fr.pin(rt)
    if pin is None:
        return None, None
    hit = _DOCS.get((str(fr.dir), rt))
    if hit is not None and hit[0] == pin:
        return copy.deepcopy(hit[1]), pin
    try:
        doc = fr.load(rt)
    except RecipeChangeRefused as exc:
        raise EvaluationError(f"the {j['sp']} {j['type']} recipe cannot be read: {exc}") from exc
    if doc is None:
        return None, None
    _DOCS[(str(fr.dir), rt)] = (pin, doc)
    return copy.deepcopy(doc), pin


_DOCS: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}  # (recipe folder, record type) -> (pin, document)


def refresh_recipe(c: Any, j: dict[str, Any]) -> None:
    """Re-read what the sponsor's recipe and this job's own steps decide, so the plan shows it before any run."""
    j["fieldrecipe"] = fieldrecipe_view(c, j)


def fieldrecipe_view(c: Any, j: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The fields a recipe decides for this job, as the plan shows them: {field: {source, header, value, ops,
    scope}}. The sponsor's recipe first, then this job's own steps over it."""
    doc, _pin = recipe_doc(c, j)
    out: dict[str, dict[str, Any]] = {}
    for scope, fields in (("sponsor", (doc or {}).get("fields") or {}), ("job", j.get("steps") or {})):
        for name, step in fields.items():
            ref = step.get("from")
            header = ref.get("header") if isinstance(ref, Mapping) else ref
            kind = "from" if "from" in step else "supply" if "supply" in step else "missing"
            out[str(name)] = {"source": kind, "header": str(header or ""), "scope": scope,
                              "value": str((step.get("supply") or {}).get("value", "")) if kind == "supply" else "",
                              "ops": [str(o.get("op")) for o in step.get("ops") or [] if isinstance(o, Mapping)]}
    return out


def to_spec(c: Any, j: Mapping[str, Any]) -> E.JobSpec:
    """The console job as the engine's input. Copies fields; decides nothing."""
    typ = str(j["type"])
    slug = T.sponsor_slug(j["sp"])
    gl = RF.is_gl(typ)
    ents = list(j.get("ents") or [j["fund"]])
    read = {"step": "console_read", "kind": "gl" if gl else "static", "source": 0, "sponsor": slug}
    check: dict[str, Any] = {"step": "console_check", "kind": "gl" if gl else "static", "sponsor": slug,
                             "rules": dict(j["rules"]), "entities": ents, "fund": j["fund"],
                             "entity_ids": {e: c.entity_id(e) for e in ents if c.entity_id(e)},
                             "fixed_ids": {str(k): v for k, v in (j.get("ids") or {}).items()}}
    if gl:
        check.update(fund_id=c.entity_id(j["fund"]) or j["fund"], fund_name=j["fund"],
                     chart_accounts=[a[0] for a in D.ACCTS], txn_types=list(D.TXN))
    else:
        plan = D.plan(typ)
        check.update(plan=plan, **_plan_columns(plan, typ))
        if c.id_decision(j["sp"], typ):  # the sponsor's decision; none: the target file's format
            check["id_scheme"] = {**c.id_scheme_of(j["sp"], typ), "year": int(str(j.get("d") or "")[:4] or 0),
                                  "source_ok": bool(source_id_column(fields_of(j), typ))}
    steps = [read, check, {"step": "write", "stem": T.file_stem(j)}]
    doc, pin = recipe_doc(c, j)
    if doc is not None:  # the sponsor's approved recipe, applied in convert before any rule or check
        from worker.recipes import engine_step

        steps.insert(2, engine_step(doc, pin, typ))
    derived = (j["up"].get("derived") or {}).get("table_sha256")  # a reshaped sheet (ticket 11): read from its blob
    src = E.SourceRef("derived", derived, None, 1, derived) if derived else E.SourceRef(
        "file", str(j["up"]["path"]), None, 1, "")
    return E.JobSpec(str(j["id"]), slug, typ, str(j["fund"]), (src,), E.PinSet(), E.Recipe.of(*steps), _choices(j),
                     {"gp_id": slug, "fund": str(j["fund"]), "scope": str(j.get("scope", ""))})



# ---- the ports --------------------------------------------------------------------------------------------
class ConsoleWorld:
    """The engine's `World` over the console's state: the sponsor's existing records (the one registry), the
    Required? choices, the account mappings and the ID counters. `commit` moves nothing: `Console.write_output`
    moves the one counter once the file is written."""

    def __init__(self, c: Any, sponsor: str) -> None:
        self.c = c
        self.sp = sponsor
        self.sponsor = T.sponsor_slug(sponsor)

    def store(self, record_type: str) -> list[dict[str, Any]]:
        return list(self.c.registry(self.sp).entries(record_type))

    def required(self, record_type: str, field_name: str, default: bool) -> bool:
        return bool(self.c.required_value(f"{self.sp}|{record_type}|{field_name}", default))

    def known_account(self, source_account: str, transaction_type: str) -> str:
        return str(self.c.known_account(self.sp, source_account, transaction_type))

    def account_entries(self) -> dict[str, dict[str, Any]]:
        return {}

    def golden_accounts(self) -> dict[str, dict[str, Any]]:
        return {}

    def id_scheme(self, record_type: str) -> dict[str, Any] | None:
        return {**self.c.id_scheme_of(self.sp, record_type), "start": self.counter(record_type)}

    def counter(self, record_type: str) -> int:
        return int(self.c.counter_of(self.sp, record_type))

    def reserve_ids(self, record_type: str, n: int, key: str, taken: Sequence[str] = ()) -> list[str]:
        raise ValueError("a console job takes its IDs from the sponsor's counter in its own check")

    def fingerprint(self, record_types: Sequence[str]) -> str:
        s = self.c.S
        facts = {
            "records": {t: [[e.get("key"), e.get("name"), e.get("source_key"), e.get("label"), e.get("job"),
                             e.get("source"), e.get("batch")] for e in self.store(t)] for t in RF.stored_types()},
            "counters": {f"{self.sp}|{t}": n for t, n in self.c.counters_of(self.sp).items()},
            "accounts": (s.get("accounts") or {}).get(self.sp, {}), "golden": s.get("golden", {}),
            "req": {k: v for k, v in s["req"].items() if k.startswith(self.sp + "|")},
            "req_ctl": {k: v for k, v in (s.get("req_ctl") or {}).items() if k.startswith(self.sp + "|")},
        }
        mine = {f"{self.sp}|{t}": self.c.id_scheme_of(self.sp, t) for t in RF.id_decision_types()
                if self.c.id_decision(self.sp, t)}
        if mine:  # only a sponsor that decided has this fact, so every other evaluation key stays as it was
            facts["idgen"] = mine
        return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()

    def commit(self, job_id: str, issued: Sequence[str], records: Sequence[Mapping[str, Any]], *,
               record_type: str = "", key: str = "", accounts: Mapping[str, str] | None = None,
               batch: str = "", reason: str = "") -> None:
        return None


_RULES: dict[str, tuple[Any, LoadedRules]] = {}


_SIG: dict[str, Any] = {"dirs": None, "at": 0.0, "sig": ()}
_RECHECK_SECONDS = 0.5  # the rule files are looked at again at most this often


def _rules_signature() -> tuple[tuple[str, int, int], ...]:
    """(path, modification time, size) of every file under the skills and the templates folders."""
    roots = (K.skills_dir(), T.templates_dir())
    now = time.monotonic()
    if _SIG["dirs"] == roots and now - float(_SIG["at"]) < _RECHECK_SECONDS:
        return _SIG["sig"]  # type: ignore[no-any-return]
    sig = []
    for root in roots:
        for p in sorted(root.rglob("*")):
            try:
                if p.is_file():
                    st = p.stat()
                    sig.append((str(p), st.st_mtime_ns, st.st_size))
            except OSError:
                continue
    _SIG.update(dirs=roots, at=now, sig=tuple(sig))
    return tuple(sig)


class CachedSkills:
    """`ProductionSkills`, loaded again only when a rule or target file changes (size, modification time)."""

    def __init__(self, sponsor: str) -> None:
        self.sponsor = sponsor

    def __call__(self, pins: Any = None) -> LoadedRules:
        sig = _rules_signature()
        hit = _RULES.get(self.sponsor)
        if hit is None or hit[0] != sig:
            hit = (sig, ProductionSkills(self.sponsor)(pins))
            _RULES[self.sponsor] = hit
        return hit[1]


class ConsoleAudit:
    """The engine's `AuditSink` for a console job. Every record goes to the engine's own log. The `write_upload`
    record goes first to the job's own `audit.jsonl` in the shape the console has always written it (so the file
    is let through only when that record exists), then to the engine's log with the manifest."""

    def __init__(self, engine_log: AuditLog, job_log: AuditLog | None = None,
                 legacy: Callable[[dict[str, Any], dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]] | None = None
                 ) -> None:
        self.engine_log = engine_log
        self.job_log = job_log
        self.legacy = legacy

    def record(self, action: str, rationale: str, inputs: dict | None = None, outputs: dict | None = None) -> dict:
        if action == "write_upload" and self.job_log is not None and self.legacy is not None:
            li, lo = self.legacy(inputs or {}, outputs or {})
            self.job_log.record("write_upload", REASON, li, lo)
            try:
                return self.engine_log.record(action, rationale, inputs, outputs)
            except AuditError:
                try:
                    self.job_log.record("write_upload.failed", "the engine's own record could not be written; the "
                                        "file was not let through", li, {"refused": True})
                except AuditError:
                    pass
                raise
        if action == "write_upload.failed" and self.job_log is not None:
            try:
                self.job_log.record(action, rationale, inputs, outputs)
            except AuditError:
                pass
        return self.engine_log.record(action, rationale, inputs, outputs)

    def record_many(self, action: str, items: list[tuple[str, dict | None, dict | None]]) -> list[dict]:
        return self.engine_log.record_many(action, items)


# ---- an evaluation, kept ----------------------------------------------------------------------------------
@dataclass
class _Held:
    key: tuple[Any, ...]
    spec: E.JobSpec
    ev: E.Evaluation
    store: MemoryStore
    rows: list[dict[str, Any]] | None = None
    gl: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _held(c: Any) -> dict[str, _Held]:
    h = getattr(c, "_engine_held", None)
    if h is None:
        h = {}
        c._engine_held = h
    return h  # type: ignore[no-any-return]


def _ports(c: Any, j: Mapping[str, Any], store: MemoryStore, audit: Any) -> Ports:
    path = Path(str(j["up"]["path"]))
    slug = T.sponsor_slug(j["sp"])
    reader: Any = FileReader([path.parent])
    if (j["up"].get("derived") or {}).get("table_sha256"):  # the approved table is played back, no program runs
        from worker.flex.service import DerivedReader
        from worker.replay import BlobStore

        reader = DerivedReader(reader, BlobStore(Path(c.root) / "flex" / "blobs"))
    return Ports(world=ConsoleWorld(c, j["sp"]), reader=reader, audit=audit, store=store,
                 out_dir=c.job_dir(j), skills=CachedSkills(slug))


def _engine_log(c: Any) -> AuditLog:
    return persist.audit(Path(c.out_dir) / ENGINE_LOG)


class EvaluationError(ValueError):
    """The engine refused to evaluate the job, and says why in plain words."""


_EVALUATING = threading.RLock()


def evaluation(c: Any, j: dict[str, Any]) -> E.Evaluation:
    """The job evaluated now: read, map, convert, check. Kept while nothing it depends on changes (its spec, the
    source file, the sponsor's records and choices, the rule files); the same call returns the same result."""
    held = _held(c)
    world = ConsoleWorld(c, j["sp"])
    path = Path(str(j["up"]["path"]))
    try:
        st = path.stat()
    except OSError as exc:
        raise EvaluationError(f"the source file cannot be read: {exc}") from exc
    key = (_job_signature(c, j), world.fingerprint(()), st.st_mtime_ns, st.st_size, _rules_signature())
    got = held.get(str(j["id"]))
    refresh_recipe(c, j)  # what the plan shows as decided by a recipe
    if got is not None and got.key == key:
        return got.ev
    with _EVALUATING:  # a view that asks while the evaluation runs waits for it: one evaluation, not one per ask
        got = held.get(str(j["id"]))
        if got is not None and got.key == key:
            return got.ev
        spec = to_spec(c, j)
        store = MemoryStore()  # a new evaluation drops what the job's earlier ones kept
        try:
            ev = E.evaluate(spec, _ports(c, j, store, ConsoleAudit(_engine_log(c))))
        except E.Refused as exc:
            raise EvaluationError(exc.sentence) from exc
        except E.StageFailed as exc:
            raise EvaluationError(str(exc)) from exc
        held[str(j["id"])] = _Held(key, spec, ev, store)
        j["eval_key"] = ev.manifest.eval_key
        if ev.facts.get("sheet"):
            j["up"]["sheet"] = ev.facts["sheet"]
        return ev


def _job_signature(c: Any, j: Mapping[str, Any]) -> str:
    """What `to_spec` reads of the job, as one string: cheap to make, so a view that asks again does not rebuild
    the spec and hash it."""
    parts = {k: j.get(k) for k in ("id", "type", "sp", "fund", "scope", "ents", "rules", "map", "dec", "qa", "fill",
                                   "dates", "acct", "dealfill", "ids", "steps")}
    parts["path"] = j["up"]["path"]
    parts["recipe"] = recipe_doc(c, j)[1]  # a changed sponsor recipe is a new evaluation
    parts["entity_ids"] = {e: c.entity_id(e) for e in (j.get("ents") or [j["fund"]])}
    return json.dumps(parts, sort_keys=True, default=str)


def _entry(c: Any, j: dict[str, Any]) -> _Held:
    evaluation(c, j)
    return _held(c)[str(j["id"])]


def fields_of(j: Mapping[str, Any]) -> list[list[Any]]:
    """The type's plan with this job's column choices applied: [field, source, required by default, confidence,
    set by you]."""
    out = EC.plan_with(D.plan(str(j["type"])), j.get("map", {}))
    for f in out:  # a field a recipe decides shows where it comes from, as set by the person
        got = (j.get("fieldrecipe") or {}).get(f[0])
        if got is not None:
            src = {"from": got["header"], "supply": f"constant: {got['value']}", "missing": ""}[got["source"]]
            f[1], f[3], f[4] = src, (100 if src else 0), 1
    slug = T.sponsor_slug(str(j["sp"]))
    for f in out:  # the demo plan carries one sponsor's name as the GP_ID constant; the file carries this sponsor's
        if f[0] == "GP_ID" and str(f[1]).startswith("constant"):
            f[1] = f"constant: {slug}"
    return out


# ---- what the views read ----------------------------------------------------------------------------------
def tally(c: Any, j: dict[str, Any]) -> dict[str, int]:
    t = evaluation(c, j).tally
    return {"err": int(t["err"]), "warn": int(t["warn"]), "out": int(t["out"]), "skip": int(t["skip"]),
            "read": int(t["read"])}


def static_rows(c: Any, j: dict[str, Any]) -> list[dict[str, Any]]:
    """The rows of a static job, each with its status and why: `i`, `row`, `id`, `nm`, `x`, `fl`, `flags`, `act`,
    `en`, `hit`, `st`, `why`, `nid`, `blank`, `res`."""
    e = _entry(c, j)
    if e.rows is None:
        e.rows = [dict(o.detail or {}) for o in e.ev.rows(limit=10**9, written=False)]
    return e.rows


def gl_eval(c: Any, j: dict[str, Any]) -> dict[str, Any]:
    """The lines of a GL job with their errors and messages, its LP offsets and its journals: `lines`, `offsets`
    and `js`."""
    e = _entry(c, j)
    if e.gl is None:
        lines: list[dict[str, Any]] = []
        offsets: list[dict[str, Any]] = []
        for o in e.ev.rows(limit=10**9):
            d = o.detail or {}
            ln = EC.line_of(d["l"])
            if d.get("off"):
                offsets.append({"l": ln, "st": o.status, "off": True, "row": o.src_row})
            else:
                lines.append({"l": ln, "st": o.status, "msgs": list(d.get("msgs", [])),
                              "errs": list(d.get("errs", [])), "row": o.src_row})
        e.gl = {"lines": lines, "offsets": offsets, "js": [dict(x) for x in e.ev.facts.get("journals", [])]}
    return e.gl


def out_ids(c: Any, j: dict[str, Any]) -> tuple[dict[int, str], int]:
    """The ID each written row gets and how many of them are new from the sponsor's counter."""
    f = evaluation(c, j).facts
    return {int(k): v for k, v in (f.get("ids") or {}).items()}, int(f.get("gen", 0))


def gl_mapper(c: Any, j: dict[str, Any]) -> GlRows:
    """Shapes a GL job's lines into upload rows: the Golden account is the person's mapping for this job, else what
    the sponsor or the organisation already knows; an investor is the sponsor's record the reference names."""
    w = c.world(j["sp"])
    investors = w.store("Investors")

    def investor(ref: str) -> tuple[str, str]:
        hit = next((e for e in investors if ref in (e.get("id"), e.get("key"), e.get("name"))), None)
        return (hit["id"], hit["name"]) if hit else ("", "")

    return GlRows(T.spec_of(j["sp"], j["type"]), T.sponsor_slug(j["sp"]), c.entity_id(j["fund"]) or j["fund"],
                  j["fund"], lambda acct, tt: golden_account(j["acct"], w.known_account, acct, tt), investor)


# ---- the source file the person gave ------------------------------------------------------------------------
def source_table(path: str | Path) -> tuple[str, list[str], list[dict[str, Any]]]:
    """(sheet, header, [{"i", "row", "v"}]) of a static source's first sheet, kept while the file does not change
    (the engine's read of the same file shares it)."""
    return EC.read_static_records(path)


def source_columns(j: Mapping[str, Any]) -> list[str]:
    """The real headers of a job's own source file (never the demo's sample headers); [] for a sample or a GL file."""
    up = j.get("up") or {}
    if not up.get("own") or RF.is_gl(str(j["type"])) or not up.get("path"):
        return []
    try:
        return [h for h in source_table(up["path"])[1] if h]
    except (OSError, ValueError, KeyError):
        return []


def sheet_facts(j: Mapping[str, Any]) -> tuple[str, int, int]:
    """(sheet, rows, width) the screens show for the source: the file's own when the person gave one, else the demo's."""
    up = j.get("up") or {}
    if up.get("own") and not RF.is_gl(str(j["type"])):
        try:
            name, header, recs = source_table(up["path"])
            return name, len(recs), len(header)
        except (OSError, ValueError, KeyError):
            pass
    return D.sheet(str(j["type"]))


# ---- approval: the engine writes the file -----------------------------------------------------------------
def write_upload(c: Any, j: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the person's `Approval` for what they saw and call `evaluate(stop_after="write")`. Returns the facts
    of the written file (`file`, `path`, `rows`, `fields`) and the job's manifest. Nothing exists unless the audit
    record was written first; on any refusal nothing is left and `ConsoleError` says why."""
    from worker.console import ConsoleError

    held = _entry(c, j)
    ev = held.ev
    spec_t = T.spec_of(j["sp"], j["type"])
    if not j.get("approver"):  # the approval is a named person's: there is no default
        raise ConsoleError("I didn't write the file: the approval has no name on it.")
    appr = E.Approval(f"appr-{j['id']}", str(j["approver"]), ev.manifest.eval_key, ev.findings_digest,
                      tuple(f.id for f in ev.findings if f.severity == "WARN"),
                      time.strftime("%Y-%m-%dT%H:%M:%S"))
    n_dec = len(j.get("dec", {}))

    def legacy(inputs: dict[str, Any], outputs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        t = outputs["tie_out"]
        tie: dict[str, Any] = {"rows": int(outputs["rows"]), "engine_written": int(t["rows_out"]),
                               "ties": int(outputs["rows"]) == int(t["rows_out"])}
        if RF.is_gl(j["type"]):
            tie.update({"debits": float(t["debits"]), "credits": float(t["credits"]), "net": float(t["net"]),
                        "journals": int(t["journals"])})
        return ({"job": j["id"], "sponsor": j["sp"], "record_type": j["type"], "template": spec_t.name},
                {"file": outputs["file"], "tie_out": tie, "ids_issued": int(outputs.get("ids_issued_count", 0)),
                 "decisions": n_dec, "manifest": outputs["manifest"]})  # M19: the manifest on every write

    audit = ConsoleAudit(_engine_log(c), persist.audit(c.job_dir(j) / "audit.jsonl"), legacy)
    try:
        done = E.evaluate(held.spec, _ports(c, j, held.store, audit), approval=appr, stop_after="write")
    except E.Refused as exc:
        raise ConsoleError(f"I didn't write the file, so the job is not approved: {exc.sentence}") from exc
    except (E.StageFailed, AuditError, OSError, ValueError) as exc:
        raise ConsoleError(f"I didn't write the file, so the job is not approved: {exc}") from exc
    p = done.produced
    if p is None:  # pragma: no cover  the engine returns Produced or raises
        raise ConsoleError("I didn't write the file, so the job is not approved: nothing was produced")
    out = {"file": p.path.name, "path": str(p.path), "rows": p.rows, "fields": list(spec_t.fields)}
    return out, done.manifest.as_record()
