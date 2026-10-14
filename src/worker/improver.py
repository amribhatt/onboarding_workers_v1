"""The improver: corrections improve the harness (rules, synonyms, know-how), never the model.

signal -> diagnose -> propose -> evaluate -> gate -> promote. The value is in the gate:

  * a write guard limits what a proposal may touch (table rows, golden mappings, synonyms, skill text); scripts,
    the scorer, answer keys, the replay set, the controls (thresholds, the severity floor), the never-invent rule,
    the audit and the gates are refused and audited;
  * an external verifier replays recorded jobs under the candidate and compares with the baseline: no regression
    on any split, a real gain on at least one, no change to a signed-off output, no gain bought with more
    questions or model calls; the private holdout contributes counts only;
  * the slow loop (know-how, prompts) must beat run-to-run noise over at least 3 runs;
  * promotion writes a new version pinned to future jobs, never changes finished work, and rolls back in one step;
  * the improver never approves its own change.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from worker.accounts import Coa, TypeMap, Versions, key_of, map_keys
from worker.audit import AuditError, AuditLog
from worker.decide import Table
from worker.fsutil import atomic_write_text
from worker.hashing import sha256_json

# ---- the write guard -------------------------------------------------------------------------------------
# `threshold` is not here: a threshold that decides what passes unseen is a control, changed only by a controller
ALLOWED_KINDS = ("table_row", "golden_mapping", "synonym", "know_how", "description", "example",
                 "standard")  # standard: an uploaded replacement of a template, the chart or the type mapping
SLOW_KINDS = ("know_how", "description", "example")  # model-dependent: weekly loop, noise band, human approval
FORBIDDEN = (
    re.compile(r"(^|[\\/])score\.py$"),
    re.compile(r"answer_key|\.answer\.json$"),
    re.compile(r"(^|[\\/])audit\.py$"),
    re.compile(r"(^|[\\/])(agent|review|checks)\.py$"),  # the gates, the review and the severity floor
    re.compile(r"(^|[\\/])prompts\.py$"),  # carries the never-invent rule
    re.compile(r"(^|[\\/])scripts([\\/]|$)"),
    re.compile(r"pyproject\.toml$|uv\.lock$|requirements"),
    re.compile(r"(^|[\\/])(cases|replay|holdout)([\\/]|$)"),
    re.compile(r"(^|[\\/])controls\.yaml$"),  # tolerances, severity floors, balance rule, required fields
)


class WriteRefused(PermissionError):
    """A proposal that touches something the improver may not edit."""


# What learning from field decisions may propose (ticket 05 D2c): one header to a synonyms file, one non-control
# table row, worked prose. A revert card removes exactly one header a promoted learning card added.
LEARNED_KINDS = ("synonym", "table_row", "example")
LEARNED_REVERT = "synonym_revert"
FORBIDDEN_LEARNED = (
    re.compile(r"(^|[\\/])recipes([\\/]|$)"),  # recipes are a sponsor's own; never copied as recipes
    re.compile(r"\.target\.yaml$"),  # a target's controls: block and its contract
    re.compile(r"(^|[\\/])transforms\.py$|\.py$"),  # a new transform op is an engineer's ticket
)


def guard(kind: str, target: str, *, learned: bool = False, change: Mapping[str, Any] | None = None,
          table: Table | None = None, base: Mapping[str, Any] | None = None) -> None:
    """Raise WriteRefused unless `kind` is an allowed surface and `target` is not a protected path.

    `learned=True` is the stricter guard for proposals drafted from field decisions: only `synonym` (add one header
    to a `synonyms.yaml`), `table_row` (one row of a `tables/` file that is not a control row, never sets or removes
    an `unseen` output, never narrows a one-way table; `table` must be given so code can tell) and `example` (prose
    in a `.md` file); plus `synonym_revert`. Never controls, a target's `controls:`, `cases/`, `threshold`, recipes,
    transform ops or code."""
    if learned:
        _guard_learned(kind, target, change or {}, table, base)
        return
    if kind not in ALLOWED_KINDS:
        raise WriteRefused(f"the improver may not make a {kind!r} change; allowed: {ALLOWED_KINDS}")
    for pattern in FORBIDDEN:
        if pattern.search(target):
            raise WriteRefused(f"{target!r} is protected: the improver may not edit it")


def _guard_learned(kind: str, target: str, change: Mapping[str, Any], table: Table | None,
                   base: Mapping[str, Any] | None) -> None:
    if kind not in (*LEARNED_KINDS, LEARNED_REVERT):
        raise WriteRefused(f"learning may propose only {LEARNED_KINDS} (and a synonym revert), never {kind!r}")
    for pattern in (*FORBIDDEN, *FORBIDDEN_LEARNED):
        if pattern.search(target):
            raise WriteRefused(f"{target!r} is protected: learning may not propose a change to it")
    path = str(change.get("path", ""))
    if re.match(r"^controls(\.|\[|$)", path):
        raise WriteRefused("learning never touches a control")
    name = Path(target).name
    if kind in ("synonym", LEARNED_REVERT):
        if name != "synonyms.yaml":
            raise WriteRefused(f"a learned synonym goes into a synonyms.yaml, not {target!r}")
        op = change.get("op")
        if kind == "synonym" and op != "add":
            raise WriteRefused("a learned synonym only adds a header; a removal is a synonym revert card")
        if kind == LEARNED_REVERT and op != "remove":
            raise WriteRefused("a synonym revert only removes the header a promoted card added")
        if not isinstance(change.get("value"), str) or not change["value"].strip():
            raise WriteRefused("a synonym change names exactly one header")
        return
    if kind == "example":
        if not name.endswith(".md"):
            raise WriteRefused("a learned example is prose in a skill's .md file")
        return
    # table_row
    if not re.search(r"(^|[\\/])tables[\\/][^\\/]+\.yaml$", target):
        raise WriteRefused("a learned table row goes into one file under a skill's tables/")
    row = change.get("row")
    if not isinstance(row, Mapping) or not row.get("id"):
        raise WriteRefused("a learned table row carries one row with its id")
    if table is None:
        raise WriteRefused("the table is needed to tell a control row from a learnable one; refused without it")
    old = base if base is not None else next((r for r in table.rows if r["id"] == row["id"]), None)
    hits = control_row_hits(table, str(row["id"]), row, old)
    if hits:
        raise WriteRefused("learning never touches a control row: " + "; ".join(hits))
    if Path(target).stem in ONE_WAY_TABLES and narrowing(old, row):
        raise WriteRefused("learning never narrows a one-way table: " + "; ".join(narrowing(old, row)))


# ---- controllers (identity is self-declared: demo-grade, not production authentication) ----------------------
CONTROL_TARGET = re.compile(r"(^|[\\/])controls\.yaml$")
CONTROL_KINDS = ("control", "threshold")
# Names that are never a person: they can never approve a change or a control (ticket 07 s1, kept regardless of auth)
RESERVED = ("improver", "agent", "console", "system", "local")
_MODEL_ID = re.compile(r"^(gpt|claude|gemini|gemma|llama|mistral|mixtral|qwen|deepseek|phi|o[1-9])-")  # matched lowercased
# One-way tables: a proposal may widen them (add an account), narrowing needs a controller. Table id -> skill.
ONE_WAY_TABLES = {"account-series": "reconcile-gl", "deal-required": "map-accounts"}


def reserved_name(name: str, model_ids: Iterable[str] | None = None) -> bool:
    """True for a non-human name: the improver, an agent, the console, the system, a policy, or a model id."""
    import os

    who = name.strip().lower()
    if who in RESERVED or who.startswith("policy:") or "/" in who or _MODEL_ID.match(who):
        return True
    known = set(model_ids or ())
    for key, value in os.environ.items():
        if key.startswith("WORKER_MODEL") and key != "WORKER_MODEL_PROVIDER":
            known.update(v.strip() for v in value.split(","))
    return who in {m.strip().lower() for m in known if m.strip()}


def is_control_proposal(proposal: Mapping[str, Any]) -> bool:
    """A proposal that touches a control: it targets controls.yaml or its kind is a control or a threshold."""
    return proposal.get("kind") in CONTROL_KINDS or bool(CONTROL_TARGET.search(str(proposal.get("target", ""))))


def _one_way_table(proposal: Mapping[str, Any]) -> str:
    if proposal.get("kind") != "table_row":
        return ""
    stem = Path(str(proposal.get("target", ""))).stem
    return stem if stem in ONE_WAY_TABLES else ""


def control_row_hits(table: Table, row_id: str, row: Mapping[str, Any],
                     base: Mapping[str, Any] | None = None) -> list[str]:
    """Why a proposed row touches a control row, or [] when it does not. A control row is one with an output value
    the table declares `unseen` (it passes something without a person looking, as the cascade rows L0 and L1 do):
    the row it would replace, or the proposed row itself, gives such a value. `base` is the row being replaced when
    the caller knows a newer one than the organisation's table."""
    unseen = {k: list(v.get("unseen") or ()) for k, v in table.outputs.items()
              if isinstance(v, Mapping) and v.get("unseen")}
    if not unseen:
        return []
    old = base if base is not None else next((r for r in table.rows if r["id"] == row_id), None)
    out = []
    for label, r in (("the row it replaces", old), ("the proposed row", row)):
        for name, values in unseen.items():
            then = (r or {}).get("then") or {}
            if name in then and any(then[name] == v and type(then[name]) is type(v) for v in values):
                out.append(f"{label} gives {name} = {then[name]!r}, which passes unseen")
    return out


def narrowing(base: Mapping[str, Any] | None, row: Mapping[str, Any]) -> list[str]:
    """What a replacement row takes away from a one-way row: dropped results, dropped list items, changed
    conditions. Empty when the row only widens (or is new)."""
    if not base:
        return []
    out = []
    if (base.get("when") or {}) != (row.get("when") or {}):
        out.append("its conditions change")
    new_then = row.get("then") or {}
    for k, v in (base.get("then") or {}).items():
        if k not in new_then:
            out.append(f"{k} is dropped")
            continue
        old_items = {x.strip() for x in str(v).split(",") if x.strip()}
        new_items = {x.strip() for x in str(new_then[k]).split(",") if x.strip()}
        lost = sorted(old_items - new_items)
        if lost:
            out.append(f"{k} loses {', '.join(lost)}")
    return out


def _names(controllers: Iterable[str] | None) -> list[str]:
    return sorted({c.strip().lower() for c in controllers or () if c and c.strip()})


def controllers_sha256(controllers: Iterable[str] | None) -> str:
    """Identity of the controllers list as recorded on every control change and at start."""
    return hashlib.sha256("\n".join(_names(controllers)).encode("utf-8")).hexdigest()


def controller_refusal(approver: str, proposer: str, controllers: Iterable[str] | None, *,
                       rollback: bool = False) -> str:
    """Why `approver` may not change a control, or "" when they may. Only a named controller who is not the
    proposer approves a change; any controller may roll back the latest change."""
    who = approver.strip().lower()
    names = _names(controllers)
    if not who:
        return "a controller (WORKER_ADMINS) must approve a change to the controls"
    if reserved_name(approver):
        return f"{approver.strip()} is not a person; a reserved non-human name can never approve a control"
    if not names:
        return "no controllers are configured (WORKER_ADMINS); a control cannot change"
    if who not in names:
        return f"{approver.strip()} is not a controller; only a controller may change a control"
    if not rollback and who == proposer.strip().lower():
        if names == [who]:  # no cooling-off, no non-controller second approval: the card waits
            return "you are the only controller; another controller must approve"
        return "the proposer cannot approve their own change to the controls"
    return ""


def proposal_sha256(proposal: Mapping[str, Any]) -> str:
    """Hash of the proposal exactly as filed and shown to the approver."""
    return sha256_json(proposal)


def _controls_sha256(skill_dir: Path) -> str:
    return hashlib.sha256((skill_dir / "controls.yaml").read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def start_checks(audit: AuditLog, controllers: Iterable[str] | None, skill_dir: str | Path | None = None) -> list[str]:
    """At start: `controllers.changed` when the controllers list differs from the last one recorded, and
    `control.unaudited_change` when controls.yaml differs from what the last audited change left (a direct edit).
    The first start records `control.baseline`. Returns the actions written."""
    written: list[str] = []
    log = audit.read()
    names = _names(controllers)
    last = next((e for e in reversed(log) if e["action"] == "controllers.changed"), None)
    before = last["inputs"]["controllers_list_sha256"] if last else controllers_sha256([])
    if controllers_sha256(names) != before:
        audit.record("controllers.changed", "the controllers list differs from the last one recorded",
                     {"controllers": names, "controllers_list_sha256": controllers_sha256(names),
                      "previous_sha256": before, "previous": last["inputs"]["controllers"] if last else []})
        written.append("controllers.changed")
    try:
        sd = Path(skill_dir) if skill_dir is not None else _control_skill_dir()
        found = _controls_sha256(sd)
    except (OSError, ValueError):
        return written  # no reconcile-gl skill installed: nothing to compare
    by_seq = {e["seq"]: e for e in log}
    expected = None
    for e in reversed(log):
        i = e.get("inputs") or {}
        if e["action"] in ("control.changed", "control.rolled_back") and "controls_sha256_after" in i:
            expected = i["controls_sha256_after"]
        elif e["action"] == "control.write_failed" and i.get("reverses_seq") in by_seq:
            expected = (by_seq[i["reverses_seq"]].get("inputs") or {}).get("controls_sha256_before")
        elif e["action"] in ("control.baseline", "control.unaudited_change"):
            expected = i.get("controls_sha256")
        else:
            continue
        break
    if expected is None:
        audit.record("control.baseline", "first start: the controls in force are recorded",
                     {"skill": "reconcile-gl", "controls_sha256": found})
        written.append("control.baseline")
    elif expected != found:
        audit.record("control.unaudited_change", "controls.yaml differs from what the last audited change left: it "
                     "was edited outside the system", {"skill": "reconcile-gl", "expected": expected,
                                                       "controls_sha256": found})
        written.append("control.unaudited_change")
    return written


# ---- replay ---------------------------------------------------------------------------------------------
@dataclass
class ReplayCase:
    case_id: str
    sponsor: str
    record_type: str
    key: str
    info: dict[str, Any]
    expected: str
    signed_off: bool = False
    holdout: bool = False

    @property
    def split(self) -> str:
        return f"{self.sponsor}/{self.record_type}"


@dataclass
class Outcome:
    action: str
    account: str
    model_calls: int = 0

    @property
    def applied(self) -> str:
        return self.account if self.action == "apply" else ""


class AccountReplay:
    """Evaluator for map-accounts: what the cascade decides cold (no sponsor memory) for each recorded key."""

    def __init__(self, coa: Coa, types: TypeMap, table: Table) -> None:
        self.coa, self.types, self.table = coa, types, table

    def run(
        self, cases: Sequence[ReplayCase], golden: Mapping[str, Any], table: Table | None = None
    ) -> dict[str, Outcome]:
        out: dict[str, Outcome] = {}
        for c in cases:
            result = map_keys({c.key: c.info}, self.coa, self.types, {}, golden, table or self.table)[0]
            out[c.case_id] = Outcome(result.action, result.account)
        return out


@dataclass
class GateResult:
    fixes: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    signed_off_changed: list[str] = field(default_factory=list)
    by_split: dict[str, dict[str, int]] = field(default_factory=dict)
    asked_before: int = 0
    asked_after: int = 0
    model_calls_before: int = 0
    model_calls_after: int = 0
    holdout: dict[str, int] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.reasons

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "passed": self.passed}


def evaluate(
    cases: Sequence[ReplayCase], baseline: Mapping[str, Outcome], candidate: Mapping[str, Outcome]
) -> GateResult:
    """Compare candidate with baseline over the replay cases. The holdout is reported as counts only."""
    g = GateResult()
    split_stats: dict[str, Counter[str]] = defaultdict(Counter)
    hold = Counter()
    for c in cases:
        b, n = baseline[c.case_id], candidate[c.case_id]
        was_right = b.applied == c.expected
        now_right = n.applied == c.expected
        bucket = hold if c.holdout else None
        fix = now_right and not was_right
        regress = was_right and not now_right
        if bucket is not None:
            bucket["fixes"] += fix
            bucket["regressions"] += regress
            bucket["signed_off_changed"] += c.signed_off and b.applied != "" and b.applied != n.applied
        else:
            if fix:
                g.fixes.append(c.case_id)
            if regress:
                g.regressions.append(c.case_id)
        split_stats[c.split]["fixes"] += fix
        split_stats[c.split]["regressions"] += regress
        if c.signed_off and b.applied and b.applied != n.applied and not c.holdout:
            g.signed_off_changed.append(c.case_id)
        g.asked_before += b.action != "apply"
        g.asked_after += n.action != "apply"
        g.model_calls_before += b.model_calls
        g.model_calls_after += n.model_calls
    g.by_split = {k: dict(v) for k, v in sorted(split_stats.items())}
    g.holdout = dict(hold)
    if g.signed_off_changed or hold["signed_off_changed"]:
        g.reasons.append("changes a signed-off output")
    if g.regressions or hold["regressions"] or any(v["regressions"] for v in split_stats.values()):
        g.reasons.append("regression on a split")
    if not (g.fixes or hold["fixes"]):
        g.reasons.append("no gain on any case")
    if g.asked_after > g.asked_before:
        g.reasons.append("asks the analyst more often")
    if g.model_calls_after > g.model_calls_before:
        g.reasons.append("uses more model calls")
    return g


def noise_gate(baseline: Sequence[float], candidate: Sequence[float], min_runs: int = 3) -> dict[str, Any]:
    """Slow-loop gate: at least `min_runs` runs each, and the mean gain must exceed the run-to-run spread."""
    if len(baseline) < min_runs or len(candidate) < min_runs:
        return {"passed": False, "reason": f"needs at least {min_runs} runs each", "gain": None, "noise": None}
    gain = statistics.mean(candidate) - statistics.mean(baseline)
    noise = max(statistics.stdev(baseline), statistics.stdev(candidate))
    ok = gain > noise
    return {"passed": ok, "gain": round(gain, 4), "noise": round(noise, 4),
            "reason": "" if ok else "the gain is within run-to-run noise"}


# ---- feedback -> proposals -------------------------------------------------------------------------------
def cluster(feedback: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Group feedback by cause: same skill, table row and the same shown -> chosen change. Most frequent first.
    Field decisions are not clustered here: they count sponsors, not records (`cluster_decisions`)."""
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for f in feedback:
        if f.get("kind") == "field_decision" or f.get("action") not in ("override", "reject", "flag"):
            continue
        groups[(f.get("skill"), f.get("table_row"), f.get("lineage"), f.get("shown"), f.get("chosen"))].append(f)
    out = []
    for (skill, row, lineage, shown, chosen), items in groups.items():
        out.append({
            "skill": skill, "table_row": row, "key": lineage, "shown": shown, "chosen": chosen,
            "count": len(items), "sponsors": sorted({str(i.get("sponsor")) for i in items}),
            "feedback_jobs": sorted({str(i.get("job")) for i in items}),
        })
    return sorted(out, key=lambda c: (-c["count"], str(c["key"])))


# ---- learning from field decisions: clusters count sponsors, never records (ticket 05 D3-D5) ----------------------
LEARNING_SKILL = "build-static-records"  # owns the header synonyms a learning card changes
SYNONYMS_TARGET = f"skills/{LEARNING_SKILL}/synonyms.yaml"
# The thresholds are controls: `learning:` in the owning skill's controls.yaml (absent: these proposed values).
LEARNING_DEFAULTS = {"min_sponsors": 3, "min_independent": 2, "min_principals": 2, "min_cases_per_split": 3,
                     "revert_override_sponsors": 2}
# Code bounds: a controls.yaml can never go below these (two sponsors can be one analyst's habit; one is never enough)
LEARNING_FLOORS = {"min_sponsors": 2, "min_independent": 1, "min_principals": 1, "min_cases_per_split": 1,
                   "revert_override_sponsors": 1}


def _learning_skill_dir() -> Path:
    from worker.skills import load_skills

    found = load_skills().get(LEARNING_SKILL)
    if found is None:
        raise ValueError(f"the {LEARNING_SKILL} skill is not installed")
    return found.path


def learning_controls(skill_dir: str | Path | None = None, *, data: Mapping[str, Any] | None = None) -> dict[str, int]:
    """The learning thresholds in force: the `learning:` section of the owning skill's controls.yaml over the
    defaults. A value below its code bound, an unknown key or a non-whole number is refused, never clamped."""
    if data is None:
        import yaml

        p = (Path(skill_dir) if skill_dir is not None else _learning_skill_dir()) / "controls.yaml"
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
        data = (doc or {}).get("learning") or {}
    if not isinstance(data, Mapping):
        raise TypeError("controls `learning:` is a mapping of threshold name to a whole number")
    unknown = sorted(set(data) - set(LEARNING_DEFAULTS))
    if unknown:
        raise ValueError(f"unknown learning controls {unknown}; known: {sorted(LEARNING_DEFAULTS)}")
    out = dict(LEARNING_DEFAULTS)
    for name, value in data.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"learning.{name} is a whole number")
        if value < LEARNING_FLOORS[name]:
            raise ValueError(f"learning.{name} = {value} is below the code bound {LEARNING_FLOORS[name]}")
        out[name] = value
    for name in ("min_independent", "min_principals"):
        if out[name] > out["min_sponsors"]:
            raise ValueError(f"learning.{name} cannot exceed min_sponsors: each sponsor counts once")
    return out


def cluster_decisions(entries: Sequence[Mapping[str, Any]],
                      controls: Mapping[str, int] | None = None) -> list[dict[str, Any]]:
    """Clusters of field decisions from the suggestion index (key plus canonical step; each sponsor once), with
    whether a card may be drafted: enough sponsors, enough of them independent of the suggestion, enough distinct
    principals, no contest, and a step that travels. Most sponsors first."""
    c = dict(controls or LEARNING_DEFAULTS)
    out = []
    for e in entries:
        n = len(e["sponsors"])
        why = []
        if n < c["min_sponsors"]:
            why.append(f"{n} sponsor(s); {c['min_sponsors']} needed")
        if e["independent"] < c["min_independent"]:
            why.append(f"{e['independent']} decided without the suggestion; {c['min_independent']} needed")
        if e["principals"] < c["min_principals"]:
            why.append(f"{e['principals']} distinct people; {c['min_principals']} needed")
        if e.get("contested"):
            why.append("contested: the same header and shape were decided another way elsewhere")
        if not e.get("eligible"):
            why.append("the step carries a sponsor's data and never travels")
        out.append({"suggestion_id": e["suggestion_id"], "key": dict(e["key"]), "step": dict(e["step"]),
                    "sponsors": n, "independent": e["independent"], "principals": e["principals"],
                    "decisions": e["decisions"], "ready": not why, "reasons": why})
    return sorted(out, key=lambda x: (-x["sponsors"], x["suggestion_id"]))


def _pseudonyms(card_id: str, hashes: Iterable[str]) -> list[str]:
    """Per-card names S1..Sn: the order is shuffled by the card id, so a sponsor's number differs between cards."""
    order = sorted(hashes, key=lambda h: hashlib.sha256(f"{card_id}:{h}".encode()).hexdigest())
    return [f"S{i + 1}" for i in range(len(order))]


def draft_learning_card(entry: Mapping[str, Any], *, controls: Mapping[str, int] | None = None,
                        synonyms: Mapping[str, Any] | None = None, target: str = SYNONYMS_TARGET,
                        base_pin: str | None = None) -> dict[str, Any] | None:
    """A `synonym` card for one ready cluster: add the header to `types.<record type>.<field>`, at the lowest
    priority. None when the cluster is not ready or the header is already there. Sponsors are per-card pseudonyms
    and no sample value appears; the header text does (it is the diff), so admins only see it until promotion.
    Replay (`min_cases_per_split`) runs before it can be promoted."""
    from worker.learnindex import header_norm

    c = dict(controls or LEARNING_DEFAULTS)
    facts = cluster_decisions([entry], c)[0]
    if not facts["ready"]:
        return None
    key, header = entry["key"], str(entry["header"])
    rt, fld = key["record_type"], key["target_field"]
    have = (((synonyms or {}).get("types") or {}).get(rt) or {}).get(fld) or []
    if any(header_norm(h) == key["header_norm"] for h in have):
        return None
    diff = {"op": "add", "path": f"types.{rt}.{fld}[-1]", "value": header}
    guard("synonym", target, learned=True, change=diff)
    card_id = "lrn-" + hashlib.sha256(json.dumps([key, entry["step"]], sort_keys=True).encode()).hexdigest()[:12]
    evidence = {"cluster_key": dict(key), "suggestion_id": entry["suggestion_id"], "feedback": list(entry["feedback"]),
                "sponsors": _pseudonyms(card_id, entry["sponsors"]), "independent_sponsors": entry["independent"],
                "distinct_principals": entry["principals"], "contradictions": 0,
                "first_decided": entry.get("first_decided"), "last_decided": entry.get("last_decided"),
                "sample_values": "none"}
    summary = f"Add '{header}' as a header for {fld}"
    return {
        "id": card_id, "status": "ready", "kind": "synonym", "loop": "fast", "scope": "organisation",
        "target": target, "base_pin": base_pin, "diff": diff, "evidence": evidence,
        "predicted": {"fields_detected_without_question": entry["decisions"], "jobs": entry["decisions"],
                      "fixes": entry["decisions"]},
        "replay": {"pending": True, "min_cases_per_split": c["min_cases_per_split"]},
        "proposal": {"kind": "synonym", "target": target, "scope": "organisation", "change": diff,
                     "evidence": evidence, "proposed_by": "improver", "speed": "fast", "summary": summary},
        "summary": summary, "proposed_by": "improver", "history": [],
    }


def file_learning_card(store: ImprovementStore, card: dict[str, Any]) -> dict[str, Any]:
    """File a learning or revert card: guarded, audited first, then saved. It waits as `awaiting_replay`, which
    `promote` refuses, until the synonym replay has run; it changes nothing live."""
    guard(card["kind"], card["target"], learned=True, change=card["diff"])
    audit = store._require_audit("a learning card")
    try:
        return store.load(card["id"])  # already filed: filing is idempotent
    except OSError:
        pass
    filed = {**card, "status": "awaiting_replay", "filed": time.strftime("%Y-%m-%dT%H:%M:%S")}
    audit.record("improver.filed", card["summary"], {"card": card["id"], "kind": card["kind"], "learned": True},
                 {"status": filed["status"]})
    store.save(filed)
    return filed


def overriding_sponsors(card_id: str, records_by_sponsor: Mapping[str, Iterable[Mapping[str, Any]]], *,
                        key: bytes | str | None = None) -> tuple[list[str], list[str]]:
    """(keyed sponsor hashes, opaque feedback ids) of overrides of a binding this card's synonym made. A binding is
    attributed through the recommended option's payload `via_card`."""
    from worker.learnindex import keyed, learning_key

    k = learning_key(key)
    sponsors, ids = set(), []
    for sponsor, records in records_by_sponsor.items():
        for r in records:
            if r.get("kind") != "field_decision" or r.get("action") != "override":
                continue
            rec = next((o for o in r.get("options_shown") or () if o.get("option_id") == r.get("recommended")), None)
            if rec is not None and (rec.get("payload") or {}).get("via_card") == card_id:
                sponsors.add(keyed("sponsor", sponsor, k))
                ids.append(f"fb:{str(r['feedback_id']).removeprefix('fb-')}")
    return sorted(sponsors), sorted(ids)


def synonym_revert_card(card: Mapping[str, Any], trigger: Mapping[str, Any]) -> dict[str, Any]:
    """A card that removes exactly the one header a promoted learning card added. Filed by code; applied only when an
    admin approves, with the audit written first."""
    added = card["diff"]
    path = re.sub(r"\[-?\d+\]$", "", str(added["path"]))
    diff = {"op": "remove", "path": path, "value": added["value"]}
    guard(LEARNED_REVERT, card["target"], learned=True, change=diff)
    summary = f"Remove '{added['value']}' from {path.rsplit('.', 1)[-1]} (learned by {card['id']})"
    return {"id": f"rev-{card['id']}", "kind": LEARNED_REVERT, "reverts": card["id"], "status": "ready",
            "scope": "organisation", "target": card["target"], "diff": diff, "trigger": dict(trigger),
            "note": "Filed by code, applied only when an admin approves; audit written first",
            "proposal": {"kind": LEARNED_REVERT, "target": card["target"], "scope": "organisation", "change": diff,
                         "proposed_by": "improver", "summary": summary},
            "summary": summary, "proposed_by": "improver", "history": []}


def revert_cards(promoted: Iterable[Mapping[str, Any]], entries: Sequence[Mapping[str, Any]],
                 records_by_sponsor: Mapping[str, Iterable[Mapping[str, Any]]], *, key: bytes | str | None = None,
                 controls: Mapping[str, int] | None = None,
                 prediction_alerts: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Revert cards for promoted learning cards whose evidence fell below N (an undo), whose bindings were
    overridden at `revert_override_sponsors` or more sponsors, or whose prediction alerted."""
    c = dict(controls or LEARNING_DEFAULTS)
    alerts = set(prediction_alerts)
    records = {s: list(r) for s, r in records_by_sponsor.items()}
    out = []
    for card in promoted:
        if card.get("kind") != "synonym" or card.get("status") != "promoted":
            continue
        sid = card["evidence"]["suggestion_id"]
        entry = next((e for e in entries if e["suggestion_id"] == sid), None)
        n = len(entry["sponsors"]) if entry is not None and not entry.get("contested") else 0
        who, ids = overriding_sponsors(card["id"], records, key=key)
        trigger = {"evidence_sponsors": n, "below_n": n < c["min_sponsors"], "override_sponsors": len(who),
                   "overrides_after_promotion": len(ids), "prediction_alert": card["id"] in alerts, "feedback": ids}
        if trigger["below_n"] or len(who) >= c["revert_override_sponsors"] or trigger["prediction_alert"]:
            out.append(synonym_revert_card(card, trigger))
    return out


def propose_golden_mapping(
    cause: Mapping[str, Any], *, proposed_by: str = "improver", scope: str = "organisation"
) -> dict[str, Any]:
    """The smallest change for a repeated override: one Golden mapping row (key -> the account people chose)."""
    if not cause.get("key") or not cause.get("chosen"):
        raise ValueError("a golden mapping needs the key and the chosen account")
    return {
        "kind": "golden_mapping", "target": "templates/standards/golden-mappings", "scope": scope,
        "change": {"key": cause["key"], "account": cause["chosen"]}, "evidence": cause, "proposed_by": proposed_by,
        "speed": "fast",
        "summary": f"{cause['count']} override(s) across {len(cause['sponsors'])} sponsor(s): "
                   f"{cause['key']} -> {cause['chosen']}",
    }


# ---- cards, versions, promotion --------------------------------------------------------------------------
def _control_skill_dir() -> Path:
    from worker import recon as R
    from worker.skills import load_skills

    found = load_skills().get(R.SKILL)
    if found is None:
        raise ValueError(f"the {R.SKILL} skill is not installed")
    return found.path



class ImprovementStore:
    """Improvement cards on disk: `<root>/cards/<id>.json`; golden versions live in `GoldenVersions`."""

    def __init__(self, root: str | Path, audit: AuditLog | None = None) -> None:
        self.root = Path(root)
        self.audit = audit

    def _path(self, card_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", card_id):
            raise ValueError(f"bad card id {card_id!r}")
        return self.root / "cards" / f"{card_id}.json"

    def _record(self, action: str, why: str, inputs: dict[str, Any], outputs: dict[str, Any] | None = None) -> None:
        if self.audit is not None:
            self.audit.record(action, why, inputs, outputs or {})

    def _require_audit(self, what: str) -> AuditLog:
        """A change that takes effect needs an audit log: the record is written first, or nothing happens."""
        if self.audit is None:
            raise AuditError(f"{what} needs an audit log; without one the change does not happen")
        return self.audit

    def save(self, card: dict[str, Any]) -> None:
        atomic_write_text(self._path(card["id"]), json.dumps(card, indent=2, ensure_ascii=False))

    def load(self, card_id: str) -> dict[str, Any]:
        return json.loads(self._path(card_id).read_text(encoding="utf-8"))

    def cards(self, status: str | None = None) -> list[dict[str, Any]]:
        d = self.root / "cards"
        out = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))] if d.is_dir() else []
        return [c for c in out if status is None or c["status"] == status]

    def file(
        self,
        proposal: dict[str, Any],
        cases: Sequence[ReplayCase],
        evaluator: AccountReplay,
        versions: Versions,
        *,
        predicted: Mapping[str, Any] | None = None,
        golden: Versions | None = None,
    ) -> dict[str, Any]:
        """Guard the proposal, replay it, and file the card (status `ready` or `blocked`).

        `versions` is the surface the proposal edits (the Golden mappings, or a decision-table overlay). A
        table-row proposal is replayed against `golden` (the live Golden mappings) when given.
        """
        try:
            guard(proposal["kind"], proposal["target"])
        except WriteRefused as exc:
            self._record("improver.refused", str(exc), {"kind": proposal["kind"], "target": proposal["target"]})
            raise
        change = proposal["change"]
        if proposal["kind"] == "golden_mapping":
            base_golden = dict(versions.current())
            cand_golden = {**base_golden, change["key"]: {"account": change["account"], "source": "improver proposal"}}
            baseline = evaluator.run(cases, base_golden)
            candidate = evaluator.run(cases, cand_golden)
        elif proposal["kind"] == "table_row":
            live = dict(golden.current()) if golden is not None else {}
            candidate_table = evaluator.table.with_override(
                {"id": evaluator.table.id, "inputs": evaluator.table.inputs, "rows": [change["row"]]}
            )
            baseline = evaluator.run(cases, live)
            candidate = evaluator.run(cases, live, candidate_table)
        else:
            raise ValueError(f"{proposal['kind']} changes are replayed by the slow loop (model runs), not here")
        gate = evaluate(cases, baseline, candidate)
        card = {
            "id": f"imp-{int(time.time() * 1000) % 10**10}-{len(self.cards()) + 1}",
            "proposal": proposal,
            "predicted": dict(predicted or {"fixes": len(gate.fixes)}),
            "replay": gate.as_dict(),
            "status": "ready" if gate.passed else "blocked",
            "filed": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "history": [],
        }
        if proposal["kind"] == "table_row":
            control = self._row_control(proposal, getattr(evaluator, "table", None), versions)
            if control:  # a controller's decision: narrowing a one-way rule, or touching a row that passes unseen
                card.update(status="awaiting_controller", proposal_sha256=proposal_sha256(proposal), control=control)
                card["replay"]["info_only"] = True
        self.save(card)
        self._record("improver.filed", proposal["summary"], {"card": card["id"], "kind": proposal["kind"]},
                     {"status": card["status"], "fixes": len(gate.fixes), "reasons": gate.reasons})
        return card

    @staticmethod
    def _row_control(proposal: Mapping[str, Any], table: Table | None, versions: Versions) -> dict[str, Any]:
        """The control facts of a table-row proposal, or {} when it needs no controller: the one-way rule it
        narrows and the unseen outputs of the control row it touches."""
        row = proposal["change"]["row"]
        stem = Path(str(proposal.get("target", ""))).stem
        one_way = _one_way_table(proposal)
        mine = table if table is not None and table.id == stem else None
        base: dict[str, Any] = {r["id"]: r for r in (mine.rows if mine is not None else [])}
        base.update({r["id"]: r for r in versions.current().get("rows", [])})
        old = base.get(row["id"])
        lost = narrowing(old, row) if one_way else []
        hits = control_row_hits(mine, row["id"], row, old) if mine is not None else []
        if not lost and not hits:
            return {}
        return {"one_way": one_way, "narrows": lost, "unseen": hits, "table": stem, "base": old}

    # ---- control changes: a person proposes, another controller approves (never the improver's road) --------
    def file_control(self, proposal: dict[str, Any], skill_dir: str | Path | None = None) -> dict[str, Any]:
        """File a proposed change to a skill's `controls.yaml`. It waits as `awaiting_controller`: `promote` refuses
        it, the fast loop never sees it, and only `approve_control` can apply it. A replay is information only."""
        import yaml

        from worker import recon as R

        who = str(proposal.get("proposed_by", "")).strip()
        if not who or reserved_name(who):
            raise PermissionError("a control change is proposed by a named person; the improver and agents cannot")
        if not CONTROL_TARGET.search(str(proposal.get("target", ""))):
            raise ValueError("a control proposal targets a skill's controls.yaml")
        new = (proposal.get("change") or {}).get("controls")
        if not isinstance(new, Mapping):
            raise TypeError("a control proposal carries the whole new controls document under change.controls")
        sd = Path(skill_dir) if skill_dir is not None else _control_skill_dir()
        ids = [d["id"] for d in yaml.safe_load((sd / "specs.yaml").read_text(encoding="utf-8"))["checks"]]
        R.validate_controls(new, ids)  # out of the code's bounds: never filed
        proposal = {**proposal, "kind": "control", "scope": "organisation", "speed": "controller"}
        filed = time.strftime("%Y-%m-%dT%H:%M:%S")
        card = {
            "id": f"ctl-{int(time.time() * 1000) % 10**10}-{len(self.cards()) + 1}",
            "proposal": proposal, "proposal_sha256": proposal_sha256(proposal),
            "replay": {"info_only": True, "note": "a replay cannot show that a change to a control is acceptable",
                       "result": proposal.get("replay")},
            "status": "awaiting_controller", "filed": filed, "history": [],
        }
        self._require_audit("a control proposal").record(
            "improver.filed", str(proposal.get("summary") or "control change proposed"),
            {"card": card["id"], "kind": "control", "proposed_by": who, "proposal_sha256": card["proposal_sha256"]},
            {"status": card["status"]})
        self.save(card)
        return card

    def approve_control(self, card_id: str, approver: str, *, controllers: Iterable[str] | None, reason: str,
                        skill_dir: str | Path | None = None, seen_sha256: str = "",
                        versions: Versions | None = None) -> dict[str, Any]:
        """A controller who is not the proposer approves a control card; `recon.apply_controls` writes the audit
        record (bound, pins, proposal hash), then the file, then checks it read back as approved. A card that
        narrows a one-way table is published to `versions` the same way: record first, then the version moves."""
        from worker import recon as R

        audit = self._require_audit("a control change")
        card = self.load(card_id)
        if card["status"] != "awaiting_controller":
            raise ValueError(f"card {card_id} is {card['status']}; only a card awaiting a controller can be approved")
        p = card["proposal"]
        sha = proposal_sha256(p)
        if sha != card.get("proposal_sha256") or (seen_sha256 and seen_sha256 != sha):
            raise ValueError("this proposal changed since it was shown; reload and review it again")
        if p["kind"] == "table_row":
            return self._approve_one_way(card, approver, controllers, reason, versions, audit, sha)
        if p["kind"] != "control":
            raise ValueError(f"card {card_id} is not a control change")
        result = R.apply_controls(
            Path(skill_dir) if skill_dir is not None else _control_skill_dir(), p["change"]["controls"],
            str(p.get("proposed_by", "")), approver, reason, audit, controllers=controllers,
            proposal={"id": card_id, "sha256": sha, "proposed_at": card.get("filed", ""),
                      "evidence": p.get("evidence") or {}, "replay": card.get("replay")})
        card.update(status="promoted", approved_by=approver.strip(), promoted=time.strftime("%Y-%m-%dT%H:%M:%S"),
                    control_seq=result["seq"], pin_before=result["pin_before"], pin_after=result["pin_after"])
        card["history"].append({"event": "promoted", "by": approver, "reason": reason, "seq": result["seq"]})
        self.save(card)
        return card

    def rollback_control(self, controller: str, reason: str, *, controllers: Iterable[str] | None,
                         skill_dir: str | Path | None = None) -> dict[str, Any]:
        """Any controller may reverse the latest `control.changed`, and only that one, when the skill still has the
        pin it recorded (otherwise it was edited out of band). `control.rolled_back` is written before the file."""
        import yaml

        from worker import recon as R
        from worker.skills import Skill

        audit = self._require_audit("a control rollback")
        refusal = controller_refusal(controller, "", controllers, rollback=True)
        if refusal:
            raise PermissionError(refusal)
        if not reason.strip():
            raise ValueError("a reason is required to roll back a control")
        events = [e for e in audit.read() if e["action"] in ("control.changed", "control.rolled_back")
                  and "controls_text_before" in (e.get("inputs") or {})]
        if not events or events[-1]["action"] != "control.changed":
            raise ValueError("there is no control change to roll back; only the latest change can be reversed")
        last = events[-1]
        rec = last["inputs"]
        sd = Path(skill_dir) if skill_dir is not None else _control_skill_dir()
        now = Skill(R.SKILL, "", "", sd).content_hash()
        if now != rec["pin_after"]:
            raise ValueError(f"the skill is at {now}, not {rec['pin_after']} as recorded: it was edited outside the "
                             "system, so the rollback is refused")
        text = rec["controls_text_before"]
        result = R.apply_controls(
            sd, yaml.safe_load(text), rec.get("proposed_by", ""), controller, reason, audit, controllers=controllers,
            rollback_of=last, text=text,
            proposal={"id": rec.get("proposal_id", ""), "sha256": rec.get("proposal_sha256", ""),
                      "proposed_at": rec.get("proposed_at", ""), "evidence": rec.get("evidence") or {}})
        if rec.get("proposal_id"):
            try:
                card = self.load(rec["proposal_id"])
            except (OSError, ValueError):
                card = None
            if card is not None:
                card.update(status="rolled_back")
                card["history"].append({"event": "rolled_back", "by": controller, "reason": reason,
                                        "seq": result["seq"]})
                self.save(card)
        return {"rolled_back": rec.get("proposal_id", ""), "reverses_seq": last["seq"], "seq": result["seq"],
                "pin_after": result["pin_after"]}

    def _approve_one_way(self, card: dict[str, Any], approver: str, controllers: Iterable[str] | None, reason: str,
                         versions: Versions | None, audit: AuditLog, sha: str) -> dict[str, Any]:
        p = card["proposal"]
        refusal = controller_refusal(approver, str(p.get("proposed_by", "")), controllers)
        if refusal:
            raise PermissionError(refusal)
        if not reason.strip():
            raise ValueError("a reason is required to change a control")
        if versions is None:
            raise ValueError("the table's versions are needed to apply a one-way row")
        row = p["change"]["row"]
        current = dict(versions.current())
        after = {**current, "rows": [row, *[r for r in current.get("rows", []) if r["id"] != row["id"]]]}

        def pin(n: int, content: Mapping[str, Any]) -> str:
            body = json.dumps(content, sort_keys=True, default=str).encode("utf-8")
            return f"v{n}#{hashlib.sha256(body).hexdigest()[:12]}"

        version = versions.next_version()
        names = _names(controllers)
        control = card.get("control") or {}
        if control.get("narrows"):
            bound_rule, direction = "a proposal may widen this table; narrowing it needs a controller", "looser"
        else:
            bound_rule = "a row that passes something unseen is a control; only a controller changes it"
            direction = "changed"
        audit.record("control.changed", reason, {
            "proposal_id": card["id"], "proposal_sha256": sha, "proposed_by": str(p.get("proposed_by", "")),
            "proposed_at": card.get("filed", ""), "approved_by": approver.strip(), "approver_roles": ["controller"],
            "control_id": f"{Path(str(p['target'])).stem}.{row['id']}", "old": control.get("base"), "new": row,
            "bound": {"one_way": control.get("one_way"), "rule": bound_rule}, "direction": direction,
            "narrows": control.get("narrows", []), "unseen": control.get("unseen", []),
            "evidence": p.get("evidence") or {}, "replay": {"info_only": True, "result": card.get("replay")},
            "pin_before": pin(versions.version, current), "pin_after": pin(version, after),
            "controllers": names, "controllers_list_sha256": controllers_sha256(names),
        }, {"version": version})
        published = versions.publish(after, note=f"{card['id']}: {p['summary']} (controller {approver.strip()})")
        card.update(status="promoted", version=published, approved_by=approver.strip(),
                    promoted=time.strftime("%Y-%m-%dT%H:%M:%S"))
        card["history"].append({"event": "promoted", "by": approver, "reason": reason, "version": published})
        self.save(card)
        return card

    def promote(
        self, card_id: str, approver: str, versions: Versions, *, admin: bool = False, reason: str
    ) -> dict[str, Any]:
        """A human approves a `ready` card. The improver cannot; organisation scope needs an admin."""
        card = self.load(card_id)
        proposal = card["proposal"]
        if card["status"] == "awaiting_controller" or is_control_proposal(proposal):
            raise PermissionError("this changes a control: only another controller can approve it (approve_control)")
        if not approver.strip() or approver.strip().lower() == str(proposal.get("proposed_by", "improver")).lower():
            raise PermissionError("the improver never approves its own change; a named human must")
        if reserved_name(approver):
            raise PermissionError(f"{approver.strip()} is not a person; a reserved non-human name can never approve")
        if card["status"] != "ready":
            raise ValueError(f"card {card_id} is {card['status']}; only a ready card can be promoted")
        if proposal["scope"] == "organisation" and not admin:
            raise PermissionError("an organisation-scope change needs an admin")
        if not reason.strip():
            raise ValueError("a reason is required to promote")
        audit = self._require_audit("a promotion")
        change = proposal["change"]
        if proposal["kind"] == "standard":  # a replaced file: its own snapshots, never the Golden mappings
            from worker.console_standards import apply_standard

            # the record comes first; if the file then cannot be written, that is recorded too
            audit.record("improver.promoted", reason, {"card": card_id, "approver": approver, "kind": "standard"},
                         {"standard": change["file"]})
            try:
                file_version = apply_standard(self, card)
            except Exception as exc:
                audit.record("improver.promote_failed", str(exc) or type(exc).__name__,
                             {"card": card_id, "approver": approver}, {"standard": change["file"]})
                raise
            card.update(status="promoted", file_version=file_version, approved_by=approver.strip(),
                        promoted=time.strftime("%Y-%m-%dT%H:%M:%S"))
            card["history"].append({"event": "promoted", "by": approver, "reason": reason, "file_version": file_version})
            self.save(card)
            return card
        current = dict(versions.current())
        if proposal["kind"] == "golden_mapping":
            current[change["key"]] = {"account": change["account"],
                                      "source": f"improvement {card_id} approved by {approver}"}
        elif proposal["kind"] in SLOW_KINDS:  # know-how text, keyed by its target
            current.setdefault("know_how", {})[proposal["target"]] = change["text"]
        else:  # a table row replaces the row with the same id, or is added
            rows = [r for r in current.get("rows", []) if r["id"] != change["row"]["id"]]
            current = {**current, "rows": [change["row"], *rows]}
        version = versions.next_version()
        audit.record("improver.promoted", reason, {"card": card_id, "approver": approver, "kind": proposal["kind"]},
                     {"golden_version": version})  # written before the version moves; a failure stops here
        published = versions.publish(current, note=f"{card_id}: {proposal['summary']}")
        if published != version:  # another writer got in between: say so, never leave the record wrong
            audit.record("improver.version_mismatch", f"recorded {version}, published {published}",
                         {"card": card_id}, {"golden_version": published})
        card.update(status="promoted", version=published, approved_by=approver.strip(),
                    promoted=time.strftime("%Y-%m-%dT%H:%M:%S"))
        card["history"].append({"event": "promoted", "by": approver, "reason": reason, "version": published})
        self.save(card)
        return card

    def reject(self, card_id: str, who: str, reason: str) -> dict[str, Any]:
        """A named human turns a card down. Nothing live changes, and the decision is audited before it is saved."""
        if not who.strip():
            raise PermissionError("a rejection needs a named human")
        if not reason.strip():
            raise ValueError("a reason is required")
        card = self.load(card_id)
        if card["status"] not in ("ready", "blocked", "awaiting_controller"):
            raise ValueError(f"card {card_id} is {card['status']}; only an open card can be rejected")
        self._require_audit("a rejection").record(
            "improver.rejected", reason, {"card": card_id, "by": who.strip(), "kind": card["proposal"]["kind"]},
            {"status": "rejected", "was": card["status"]})
        card.update(status="rejected")
        card["history"].append({"event": "rejected", "by": who, "reason": reason})
        self.save(card)
        return card

    def rollback(
        self, versions: Versions, approver: str, reason: str, kinds: Sequence[str] | None = None
    ) -> dict[str, Any]:
        """One step back: the previous version is current again, byte for byte.

        `kinds` limits which promoted cards are marked rolled back, so two surfaces that share a version number
        (the Golden mappings and a decision-table overlay) never mark each other's cards."""
        if not approver.strip() or not reason.strip():
            raise PermissionError("a rollback needs a named human and a reason")
        audit = self._require_audit("a rollback")
        current = versions.version
        if current == 0:
            raise ValueError("there is no earlier version to go back to")
        audit.record("improver.rolled_back", reason, {"approver": approver, "from_version": current},
                     {"golden_version": current - 1})  # written before the pointer moves
        version = versions.rollback()
        for card in self.cards("promoted"):
            if card.get("version") == version + 1 and (kinds is None or card["proposal"]["kind"] in kinds):
                card.update(status="rolled_back")
                card["history"].append({"event": "rolled_back", "by": approver, "reason": reason})
                self.save(card)
        return {"golden_version": version}


def check_prediction(card: Mapping[str, Any], observed_fixes: int, tolerance: int = 1) -> dict[str, Any]:
    """Predicted vs observed after release (tracked for 2 weeks): an alert when they diverge."""
    predicted = int((card.get("predicted") or {}).get("fixes", 0))
    diff = observed_fixes - predicted
    return {"card": card["id"], "predicted": predicted, "observed": observed_fixes,
            "alert": abs(diff) > tolerance, "difference": diff}


def fast_loop(
    store: ImprovementStore,
    proposals: Sequence[dict[str, Any]],
    cases: Sequence[ReplayCase],
    evaluator: AccountReplay,
    versions: Versions,
    *,
    golden: Versions | None = None,
    auto_promote_sponsor: bool = False,
    approver: str = "",
) -> list[dict[str, Any]]:
    """The nightly loop: replay every proposal deterministically (no model calls) and file the cards.

    A passing sponsor-scope card is promoted only when `auto_promote_sponsor` is on (off by default) and a named
    human owner is configured as approver; organisation scope always waits for an admin.
    """
    cards = []
    for p in proposals:
        try:
            card = store.file(p, cases, evaluator, versions, golden=golden)
        except WriteRefused:
            continue
        if auto_promote_sponsor and card["status"] == "ready" and p["scope"] == "sponsor" and approver:
            card = store.promote(
                card["id"], approver, versions, reason="auto-promoted by the configured sponsor policy"
            )
        cards.append(card)
    return cards


def info_from_key(key: str) -> dict[str, Any]:
    """The account type, source account and transaction type a normalised key was built from."""
    parts = [p.strip() for p in re.split(r"\|", key)]
    if len(parts) != 3:
        raise ValueError(f"not an account key: {key!r}")
    return {"account_type": parts[0], "account": parts[1], "transaction_type": parts[2], "rows": 0}


def build_cases(
    store_entries: Mapping[str, Mapping[str, Mapping[str, Any]]],
    keys: Mapping[str, Mapping[str, Any]],
    *,
    signed_off: Callable[[str, str], bool] = lambda sponsor, key: True,
    holdout: Callable[[str, str], bool] = lambda sponsor, key: False,
    record_type: str = "GL",
) -> list[ReplayCase]:
    """Replay cases from approved mappings: each sponsor's stored key -> account is what a person signed off."""
    out = []
    for sponsor, entries in sorted(store_entries.items()):
        for key, entry in sorted(entries.items()):
            info = keys.get(key)
            if info is None:
                continue
            out.append(ReplayCase(f"{sponsor}:{key}", sponsor, record_type, key, dict(info), str(entry["account"]),
                                  signed_off(sponsor, key), holdout(sponsor, key)))
    return out


__all__ = [
    "ALLOWED_KINDS",
    "SLOW_KINDS",
    "AccountReplay",
    "GateResult",
    "ImprovementStore",
    "Outcome",
    "ReplayCase",
    "WriteRefused",
    "build_cases",
    "check_prediction",
    "cluster",
    "cluster_decisions",
    "draft_learning_card",
    "evaluate",
    "fast_loop",
    "file_learning_card",
    "guard",
    "info_from_key",
    "key_of",
    "learning_controls",
    "noise_gate",
    "propose_golden_mapping",
    "revert_cards",
    "synonym_revert_card",
]
