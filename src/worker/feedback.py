"""Feedback: every human correction is data, attached to what produced the choice.

A record says what was shown, what the human did (accept, override, reject, flag, comment), why (optional), how far
it should reach (row, job, sponsor, organisation), and what made the choice (skill@version, decision-table row,
model, prompt version) with the input fingerprint and the source cell. It goes into the job's audit (hash-chained)
and into the sponsor's feedback log. Code routes it from its attachment; a model may only draft the wording.

A human's answer to an `unknown_field` or `unknown_format` question is a `field_decision` (ticket 05 D1): built by
`make_field_decision`, written to the job's audit first and then to the sponsor's own log, never to a shared file.
Sample values are referenced, never copied; the value shape, the recommended option and accept/override are code's.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from worker.audit import AuditLog
from worker.hashing import sha256_json

ACTIONS = ("accept", "override", "reject", "flag", "comment")
SCOPES = ("row", "job", "sponsor", "organisation")
KINDS = ("mapping", "suggestion", "row", "cell", "job", "outcome", "chat", "field_decision")
_FIELDS = (
    "kind", "action", "shown", "chosen", "reason", "scope", "job", "sponsor", "record_type", "skill", "table_row",
    "model", "prompt_version", "input_sha256", "lineage", "check", "concept", "outcome",
)

# ---- field decisions (ticket 05 D1, learning-loop.md section 1) -------------------------------------------------
FIELD_DECISION = "field_decision"
DECISION_QUESTIONS = ("unknown_field", "unknown_format")
DECISION_SCOPES = ("job", "sponsor")  # "every sponsor" is a request (suggest_org), never a scope
PIN_KEYS = ("skills", "controls", "target", "recipe_before", "model", "prompt", "mapper_handoff")
SOURCE_KEYS = ("file_fp", "sheet", "header", "col_id")
SAMPLE_REF_KEYS = ("kind", "id", "checksum")
AUTH = "self-declared-demo"  # identity is self-declared: demo-grade, not production authentication
_DECISION_ARGS = (
    "job", "unit_id", "sponsor", "record_type", "target_field", "question", "source", "values", "sample_ref",
    "options_shown", "chosen", "scope", "principal", "pins", "conformance", "reason", "mapper_confidence",
    "suggest_org", "supersedes", "recipe_after", "field_required", "action", "recommended",
)


class FeedbackError(ValueError):
    """A feedback record that does not say what it needs to."""


def _need_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FeedbackError(f"a field decision names its {name}")
    return value.strip()


def _option(o: Any, where: str) -> dict[str, Any]:
    from worker.contracts import ContractError, Option

    try:
        opt = o if isinstance(o, Option) else Option.from_dict(o)
    except (ContractError, TypeError, KeyError) as exc:
        raise FeedbackError(f"{where} is not a valid option: {exc}") from exc
    return opt.to_dict()


def _principal(p: Any) -> dict[str, str]:
    from worker.contracts import is_agent_principal
    from worker.improver import reserved_name

    name = p.get("name") if isinstance(p, Mapping) else p
    if not isinstance(name, str) or not name.strip():
        raise FeedbackError("a field decision names its principal: only a human answers")
    if is_agent_principal(name) or reserved_name(name):
        raise FeedbackError(f"{name.strip()!r} is not a person; only a human answers a question")
    return {"name": name.strip(), "auth": AUTH}


def _counts(name: str, c: Any) -> dict[str, Any]:
    if not isinstance(c, Mapping):
        raise FeedbackError(f"conformance.{name} is {{bound, conform, total, findings}}")
    total, conform = c.get("total"), c.get("conform")
    if not (isinstance(total, int) and isinstance(conform, int) and 0 <= conform <= total):
        raise FeedbackError(f"conformance.{name} needs whole numbers with 0 <= conform <= total")
    return {"bound": bool(c.get("bound")), "conform": conform, "total": total,
            "findings": dict(c.get("findings") or {})}


def make_field_decision(**given: Any) -> dict[str, Any]:
    """Validate and complete one `field_decision` record. Code fills what code decides: `header_norm`, `shape_sig`
    (from `values`, which are never stored), the recommended option (`contracts.choose_recommended`), `action`
    (accept when the recommended option was chosen, override otherwise), `via_suggestion` and `feedback_id`."""
    from worker import learnindex as L
    from worker.contracts import Option as _Opt
    from worker.contracts import choose_recommended

    unknown = sorted(k for k in given if k not in _DECISION_ARGS)
    if unknown:
        raise FeedbackError(f"unknown field-decision fields: {unknown}")
    g = dict(given)
    job, sponsor = _need_text("job", g.get("job")), _need_text("sponsor", g.get("sponsor"))
    unit_id = _need_text("unit (unit_id)", g.get("unit_id"))
    record_type = _need_text("record type", g.get("record_type"))
    target_field = _need_text("target field", g.get("target_field"))
    principal = _principal(g.get("principal"))
    q = g.get("question") or {}
    if not isinstance(q, Mapping) or q.get("kind") not in DECISION_QUESTIONS:
        raise FeedbackError(f"question is {{question_id, kind}} with kind one of {DECISION_QUESTIONS}")
    question = {"question_id": _need_text("question id", q.get("question_id")), "kind": q["kind"]}
    src = g.get("source") or {}
    if not isinstance(src, Mapping):
        raise FeedbackError("source is a mapping")
    for k in SOURCE_KEYS:
        _need_text(f"source {k}", src.get(k))
    norm = L.header_norm(src["header"])
    if "header_norm" in src and src["header_norm"] != norm:
        raise FeedbackError("header_norm is computed by code; the one given does not match the header")
    source = {"file_fp": src["file_fp"], "sheet": src["sheet"], "header": src["header"], "header_norm": norm,
              "col_id": src["col_id"], "profile_id": src.get("profile_id")}
    values = g.get("values")
    if values is None or isinstance(values, (str, bytes, Mapping)):
        raise FeedbackError("values (every value of the column) are needed: code computes the shape from them")
    values = list(values)
    ref = g.get("sample_ref") or {}
    if not isinstance(ref, Mapping) or any(not ref.get(k) for k in SAMPLE_REF_KEYS):
        raise FeedbackError(f"sample_ref is a reference {SAMPLE_REF_KEYS}; values are never copied")
    samples = {"sample_ref": {k: ref[k] for k in SAMPLE_REF_KEYS},
               "rows_seen": sum(1 for v in values if L.value_class(v) is not None),
               "shape_sig": L.shape_sig(values), "shape_version": L.SHAPE_VERSION}
    shown = g.get("options_shown") or ()
    opts = [_option(o, f"options_shown[{i}]") for i, o in enumerate(shown)]
    if not opts:
        raise FeedbackError("a field decision records the options shown")
    ids = [o["option_id"] for o in opts]
    if len(set(ids)) != len(ids):
        raise FeedbackError("options_shown holds the same option id twice")
    recommended = choose_recommended([_Opt.from_dict(o) for o in opts])
    if "recommended" in g and g["recommended"] != recommended:
        raise FeedbackError("the recommended option is chosen by code; the one given does not match")
    reason = " ".join(str(g.get("reason") or "").split())
    chosen_in = g.get("chosen") or {}
    if not isinstance(chosen_in, Mapping) or len(chosen_in) != 1 or not ({"option_id", "custom"} & set(chosen_in)):
        raise FeedbackError("chosen is {option_id} or {custom: an option with a closed action}")
    if "option_id" in chosen_in:
        if chosen_in["option_id"] not in ids:
            raise FeedbackError(f"chosen option {chosen_in['option_id']!r} was not shown")
        chosen: dict[str, Any] = {"option_id": chosen_in["option_id"]}
        picked = opts[ids.index(chosen_in["option_id"])]
    else:
        picked = _option(chosen_in["custom"], "chosen.custom")
        if picked["proposed_by"] != "human":
            raise FeedbackError("a custom option is proposed by the human")
        if not reason:
            raise FeedbackError("a custom option needs a reason")
        chosen = {"custom": picked}
    if picked["action"] == "declare_missing" and g.get("field_required") and not reason:
        raise FeedbackError("declaring a required field missing needs a reason")
    action = "accept" if recommended is not None and chosen.get("option_id") == recommended else "override"
    if g.get("action") not in (None, action):
        raise FeedbackError(f"the action is decided by code: this is {action!r}")
    scope = g.get("scope")
    if scope not in DECISION_SCOPES:
        raise FeedbackError(f"scope must be one of {DECISION_SCOPES}")
    pins = g.get("pins") or {}
    missing = [k for k in PIN_KEYS if not isinstance(pins, Mapping) or k not in pins]
    if missing:
        raise FeedbackError(f"pins in force are recorded; missing {missing}")
    conf = g.get("conformance") or {}
    if not isinstance(conf, Mapping):
        raise FeedbackError("conformance is {before, after}")
    conformance = {"before": _counts("before", conf.get("before")), "after": _counts("after", conf.get("after"))}
    confidence = g.get("mapper_confidence")
    if confidence is not None and not isinstance(confidence, (int, float)):
        raise FeedbackError("mapper_confidence is a number (evidence only, never a gate)")
    via = picked.get("suggestion_id") if picked.get("proposed_by") == "learned" else None
    rec = {
        "schema_version": 1, "kind": FIELD_DECISION, "action": action, "job": job, "unit_id": unit_id,
        "sponsor": sponsor, "record_type": record_type, "target_field": target_field, "question": question,
        "source": source, "samples": samples, "options_shown": opts, "recommended": recommended,
        "mapper_confidence": confidence, "chosen": chosen, "scope": scope, "suggest_org": bool(g.get("suggest_org")),
        "reason": reason, "principal": principal, "pins": {k: pins[k] for k in PIN_KEYS},
        "conformance": conformance, "recipe_after": g.get("recipe_after") if scope == "sponsor" else None,
        "supersedes": g.get("supersedes"), "via_suggestion": via,
    }
    rec["feedback_id"] = "fb-" + sha256_json(rec)[:12]
    return rec


def make_record(**given: Any) -> dict[str, Any]:
    """Validate and complete one feedback record; the scope defaults to the job."""
    if given.get("kind") == FIELD_DECISION:
        return make_field_decision(**{k: v for k, v in given.items() if k != "kind"})
    unknown = [k for k in given if k not in _FIELDS]
    if unknown:
        raise FeedbackError(f"unknown feedback fields: {sorted(unknown)}")
    rec = {k: given.get(k) for k in _FIELDS}
    if rec["kind"] not in KINDS:
        raise FeedbackError(f"kind must be one of {KINDS}")
    if rec["action"] not in ACTIONS:
        raise FeedbackError(f"action must be one of {ACTIONS}")
    rec["scope"] = rec["scope"] or "job"
    if rec["scope"] not in SCOPES:
        raise FeedbackError(f"scope must be one of {SCOPES}")
    rec["reason"] = " ".join(str(rec["reason"] or "").split())
    if rec["action"] == "comment" and not rec["reason"]:
        raise FeedbackError("a comment needs its text in `reason`")
    if not (rec["sponsor"] and rec["job"]):
        raise FeedbackError("feedback names its sponsor and job")
    return rec


def summary_line(rec: Mapping[str, Any]) -> str:
    return rec["reason"] or f"{rec['action']} on {rec['kind']} (no comment given)"


class FeedbackStore:
    """The sponsor's feedback log: one hash-chained file per sponsor, read-only to everyone but `record`."""

    def __init__(self, root: str | Path, sponsor: str) -> None:
        self.log = AuditLog(Path(root) / sponsor / "feedback.jsonl")
        self.sponsor = sponsor

    def add(self, rec: Mapping[str, Any]) -> dict[str, Any]:
        return self.log.record("feedback", summary_line(rec), dict(rec), {"target": route(rec)["target"]})

    def add_undo(self, feedback_id: str, principal: Any, reason: str) -> dict[str, Any]:
        """Append an `undo` record for one of this sponsor's decisions. Nothing is deleted: the decision stays in the
        log and stops counting as evidence."""
        if not any(r.get("feedback_id") == feedback_id for r in self.list(kind=FIELD_DECISION)):
            raise FeedbackError(f"{feedback_id} is not a field decision in this sponsor's log")
        who = _principal(principal)
        if not str(reason or "").strip():
            raise FeedbackError("an undo needs a reason")
        return self.log.record("feedback.undo", str(reason).strip(),
                               {"feedback_id": feedback_id, "principal": who, "sponsor": self.sponsor})

    def undone(self) -> set[str]:
        return {e["inputs"]["feedback_id"] for e in self.log.read() if e["action"] == "feedback.undo"}

    def list(self, *, skill: str | None = None, action: str | None = None, scope: str | None = None,
             kind: str | None = None) -> list[dict[str, Any]]:
        out = []
        for e in self.log.read():
            if e["action"] != "feedback":  # undo records are read through `undone`
                continue
            r = e["inputs"]
            if kind and r.get("kind") != kind:
                continue
            if skill and (r.get("skill") or "").split("@")[0] != skill:
                continue
            if action and r.get("action") != action:
                continue
            if scope and r.get("scope") != scope:
                continue
            out.append({"seq": e["seq"], "ts": e["ts"], **r, "target": (e.get("outputs") or {}).get("target")})
        return out


def record(audit: AuditLog, store: FeedbackStore | None, **given: Any) -> dict[str, Any]:
    """Write a feedback record to the job's audit, then to the sponsor's log. Returns the record."""
    rec = make_record(**given)
    entry = audit.record("feedback", summary_line(rec), rec, {"target": route(rec)["target"]})
    if rec["kind"] == FIELD_DECISION:  # the time of the decision is the time of its audit entry
        rec = {**rec, "decided_at": entry["ts"], "audit_seq": entry["seq"]}
    if store is not None:
        store.add(rec)
    return rec


def route(rec: Mapping[str, Any]) -> dict[str, Any]:
    """Where this feedback should go, decided from its attachment alone (no model).

    data rule      -> a proposed decision-table row (`table_row`)
    concept        -> a proposed synonym
    skill behaviour-> a proposed know-how edit or example (`skill_edit`)
    tool/check gap -> a ticket with the failing case attached
    """
    kind, action = rec["kind"], rec["action"]
    if kind == FIELD_DECISION:  # the sponsor's recipe first, then a suggestion elsewhere, then a shared synonym
        if rec.get("scope") != "sponsor":
            return {"target": "feedback_only", "why": "a job-scope answer is recorded, never learned", "then": []}
        return {"target": "recipe", "why": "a field decided for this sponsor", "then": ["suggestion", "synonym"]}
    skill = (rec.get("skill") or "").split("@")[0]
    if kind == "outcome":
        return {"target": "ticket", "why": "an upload outcome is a tool or check gap", "skill": skill or None}
    if kind == "mapping" and rec.get("concept"):
        return {"target": "synonym", "why": "a header or field meaning", "concept": rec["concept"]}
    if kind in ("mapping", "suggestion") and skill and rec.get("table_row"):
        return {"target": "table_row", "why": "a data rule fired this row", "skill": skill, "row": rec["table_row"]}
    if kind in ("row", "cell"):
        if rec.get("check"):
            return {"target": "table_row", "why": f"check {rec['check']} flagged it", "skill": skill or None,
                    "check": rec["check"]}
        return {"target": "ticket", "why": "a row problem no check names is a check gap"}
    if skill:
        return {"target": "skill_edit", "why": "skill behaviour", "skill": skill}
    if action in ("override", "reject", "flag"):
        return {"target": "ticket", "why": "no skill or rule produced it"}
    return {"target": "skill_edit", "why": "general note", "skill": None}


def draft(rec: Mapping[str, Any], writer: Callable[[str], str] | None = None) -> dict[str, Any]:
    """A proposal for the routed target. Routing is code; `writer` (a model) only words the proposal."""
    target = route(rec)
    facts = (
        f"{rec['action']} on {rec['kind']}: shown {rec.get('shown')!r}, chosen {rec.get('chosen')!r}, "
        f"reason {rec['reason'] or '(none)'}; scope {rec['scope']}; sponsor {rec['sponsor']}"
    )
    text = writer(f"Draft a one-sentence change proposal for a {target['target']} from: {facts}") if writer else facts
    return {**target, "wording": text, "evidence": {k: rec.get(k) for k in ("input_sha256", "lineage", "job")}}
