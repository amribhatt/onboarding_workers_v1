"""The model-change smoke test (ticket 04 D6, decision Q9): warn when a newly configured model is clearly unsuitable.

A model swap should not matter: the knowledge lives in the skills and the engine, the model only proposes. So a
change of model is checked lightly, once, and the result is a WARNING only. It never blocks a job, never changes or
re-approves a skill, and never writes anything outside `<state>/models/`.

* **Trigger.** At start-up the configured model id of each model role (`agent.ROLES`: plan, exec, search, small, as
  `agent.model_for` / `models.model_for_role` resolve it from the settings) is compared with the last one recorded
  in `<state>/models/seen.json`. A difference (a first sight counts) is audited as `model.changed` with the old and
  the new id, before anything runs.
* **Case set.** Fixed: the non-held-out skill `cases/` files (map-accounts cascade -> Key Resolver, build-static-
  records headers -> Mapper, reconcile-gl known GL -> Explainer) plus the recorded jobs named in the `smoke` control
  (one tape entry per family and agent role). A case runs only when the model role its agent role uses has changed.
* **Run.** Each case is asked once (pass@1) through an injected `ask`, with the role's real instructions and brief
  layout (`role_runner.RoleRunner.call`). No repair attempt.
* **Pass.** The answer is schema-valid (the role's contract builds), passes code verification, and its decision
  equals the recorded one.
* **Clearly unsuitable** is either: the pass rate of a role is below `pass_floor` (a control; unset means only hard
  zeros warn), or any hard-zero violation: a pick outside the candidates, an invented binding (a key, field or column
  that was not given), or a number that is not in the engine result the model was given.
* **Effect.** `model.smoke_warn` (or `model.smoke_passed`) is audited in `<state>/models/audit.jsonl`; the last
  outcome is kept in `<state>/models/smoke.json` for the health view and the run view.

NOT VERIFIED: the behaviour against a live model. Tests drive this module with scripted answers only; in production
it is wired to `models.model_for_role` but runs only when `WORKER_SMOKE_ON_START=1` (default off), and that path has
never been exercised here.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from worker import persist
from worker.audit import AuditLog
from worker.contracts import ContractError, Explanation, KeyDecisions, MappingPlan, Result
from worker.fsutil import atomic_write_text
from worker.hashing import sha256_json as _sha
from worker.prompts import as_data

SMOKE_ON_START = "WORKER_SMOKE_ON_START"
PASS_FLOOR_BOUNDS = (Decimal(0), Decimal(1))  # code bounds of the `pass_floor` control
CONTROL_KEYS = frozenset({"pass_floor", "cases", "jobs"})
SMOKE_ROLES = ("key_resolver", "mapper", "explainer")  # the agent roles the smoke cases exercise
_SAFE_ACTIONS = ("apply", "suggest", "needs_you")  # cascade outcomes a Key Resolver decides; `blocked` is code's

Ask = Callable[["SmokeCase", list[BaseMessage]], str]


class SmokeControlError(ValueError):
    """The `smoke` control is malformed or outside its code bounds; refused, never clamped."""


# ---- cases ------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SmokeCase:
    """One question with its recorded decision. `payload` is all the model sees; `expected` is the normalised
    decision (see `decision`)."""

    case_id: str
    role: str  # agent role: key_resolver | mapper | explainer
    objective: str
    payload: Mapping[str, Any]
    expected: Any
    source: str  # "skill:<skill>/<file>" or "job:<sponsor>/<job_id>"


def _read_cases(path: Path) -> list[dict[str, Any]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return [c for c in doc.get("cases", []) if isinstance(c, dict) and not c.get("holdout")]


def _cascade_cases(path: Path) -> list[SmokeCase]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    store = {k: v["account"] for k, v in (doc.get("sponsor_store") or {}).items()}
    usable = [c for c in _read_cases(path) if c["expect"].get("action") in _SAFE_ACTIONS]
    candidates = sorted({c["expect"]["account"] for c in usable if c["expect"].get("account")} | set(store.values()))
    out = []
    for c in usable:
        key = f"{c.get('account_type', '')} | {c.get('account', '')} | {c.get('transaction_type', '')}"
        batch_id = f"k-{_sha(['smoke', c['name']])[:16]}"
        payload = {"batch_id": batch_id, "approved_earlier": store, "items": [
            {"key": key, "candidates": candidates, "account_type": c.get("account_type", ""),
             "account": c.get("account", ""), "transaction_type": c.get("transaction_type", "")}]}
        out.append(SmokeCase(f"map-accounts/cascade#{c['name']}", "key_resolver",
                             "Pick a candidate for each key (voter 1 of 1)", payload,
                             {key: c["expect"].get("account") or None}, "skill:map-accounts/cases/cascade.json"))
    return out


def _header_cases(path: Path) -> list[SmokeCase]:
    out = []
    for c in _read_cases(path):
        headers = list(c.get("headers") or [])
        column = (c.get("expect") or {}).get("column")
        if column not in headers:  # not a column pick (a value read from the sheet name, say): code decides it
            continue
        columns = [{"col_id": f"c{i + 1}", "header": h, "samples": []} for i, h in enumerate(headers)]
        payload = {"record_type": c["record_type"], "fields": [{"field": c["field"]}], "columns": columns,
                   "transform_ops": [], "setup_fields": []}
        out.append(SmokeCase(f"build-static-records/headers#{c['name']}", "mapper",
                             f"Bind the open fields of {c['record_type']}", payload,
                             {c["field"]: f"c{headers.index(column) + 1}"},
                             "skill:build-static-records/cases/headers.json"))
    return out


def _recon_cases(path: Path) -> list[SmokeCase]:
    out = []
    for i, c in enumerate(_read_cases(path), start=1):
        worst = (c.get("expect") or {}).get("worst")
        figures = (c.get("expect") or {}).get("figures") or {}
        if worst not in ("WARN", "ERROR", "REVIEW") or not figures:  # the Explainer runs per non-PASS break
            continue
        rows = {f"r{j}": {"check": c["check"], "figure": label, "amount": value, "severity": worst}
                for j, (label, value) in enumerate(figures.items(), start=1)}
        cause_id = f"{c['check']}-{i}"
        payload = {"cause_id": cause_id, "check_no": None, "rows": rows, "know_how": ""}
        out.append(SmokeCase(f"reconcile-gl/known-gl#{c['name']}", "explainer", f"Explain break {cause_id}",
                             payload, sorted(rows), "skill:reconcile-gl/cases/known-gl.json"))
    return out


_SKILL_CASES: tuple[tuple[str, str, Callable[[Path], list[SmokeCase]]], ...] = (
    ("map-accounts", "cascade.json", _cascade_cases),
    ("build-static-records", "headers.json", _header_cases),
    ("reconcile-gl", "known-gl.json", _recon_cases),
)


def skill_cases(skills_root: Path | None = None) -> list[SmokeCase]:
    """The non-held-out skill cases, in a fixed order. A missing case file is an error: the set is fixed."""
    from worker.skills import skills_dir

    root = Path(skills_root) if skills_root is not None else skills_dir()
    out: list[SmokeCase] = []
    for skill, name, reader in _SKILL_CASES:
        out.extend(reader(root / skill / "cases" / name))
    return out


def job_cases(replay_root: Path, jobs: Sequence[str]) -> tuple[list[SmokeCase], list[str]]:
    """Cases from the named recorded jobs (`sponsor/job_id`): the first results-tape entry per family and agent role
    that carries its payload. Returns (cases, why each named job gave nothing): never silently skipped."""
    from worker.replay import BlobStore, ReplayError, ReplayRecord

    blobs = BlobStore(Path(replay_root) / "blobs")
    cases: list[SmokeCase] = []
    unavailable: list[str] = []
    taken: set[tuple[str, str]] = set()
    for name in jobs:
        sponsor, _, job_id = str(name).partition("/")
        try:
            record = ReplayRecord.load(Path(replay_root) / sponsor / f"{job_id}.json")
            tape_ref = (record.blobs or {}).get("results_tape")
            tape = blobs.get_json(tape_ref) if isinstance(tape_ref, str) else None
        except ReplayError as exc:
            unavailable.append(f"{name}: {exc}")
            continue
        if not isinstance(tape, list):
            unavailable.append(f"{name}: the record has no results tape")
            continue
        found = 0
        for i, entry in enumerate(tape):
            role = entry.get("role") if isinstance(entry, Mapping) else None
            if role not in SMOKE_ROLES or (record.family, role) in taken:
                continue
            payload, output = entry.get("payload"), entry.get("output")
            if not isinstance(payload, Mapping) or output is None:
                continue
            try:
                expected = decision(role, _build(role, payload, output))
            except (ContractError, TypeError, ValueError, KeyError) as exc:
                unavailable.append(f"{name}#{i}: the recorded output does not build: {exc}")
                continue
            taken.add((record.family, role))
            found += 1
            cases.append(SmokeCase(f"job:{name}#{i}", role, str(entry.get("objective") or f"recorded {role} step"),
                                   dict(payload), expected, f"job:{name}"))
        if not found:
            unavailable.append(f"{name}: no tape entry with its payload for a role not yet covered")
    return cases, unavailable


# ---- the control ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SmokeControl:
    pass_floor: Decimal | None = None  # unset: only a hard zero warns
    cases: tuple[str, ...] | None = None  # case ids to keep; None = every non-held-out skill case
    jobs: tuple[str, ...] = ()  # recorded jobs, "sponsor/job_id"


def smoke_control(data: Mapping[str, Any] | None) -> SmokeControl:
    """The `smoke:` section of a controls file, checked against its code bounds."""
    if not data:
        return SmokeControl()
    if not isinstance(data, Mapping):
        raise SmokeControlError("the smoke control is a mapping")
    unknown = sorted(set(data) - CONTROL_KEYS)
    if unknown:
        raise SmokeControlError(f"unknown smoke controls {unknown}; known: {sorted(CONTROL_KEYS)}")
    floor = data.get("pass_floor")
    if floor is not None:
        if isinstance(floor, bool):
            raise SmokeControlError("smoke.pass_floor is a number from 0 to 1")
        try:
            floor = Decimal(str(floor))
        except InvalidOperation:
            raise SmokeControlError("smoke.pass_floor is a number from 0 to 1") from None
        if not PASS_FLOOR_BOUNDS[0] <= floor <= PASS_FLOOR_BOUNDS[1]:
            raise SmokeControlError(f"smoke.pass_floor = {floor} is outside the code bounds 0 to 1")
    cases = data.get("cases")
    if cases is not None and (not isinstance(cases, list) or not all(isinstance(c, str) for c in cases)):
        raise SmokeControlError("smoke.cases is a list of case ids")
    jobs = data.get("jobs") or []
    if not isinstance(jobs, list) or not all(isinstance(j, str) and j.count("/") == 1 for j in jobs):
        raise SmokeControlError("smoke.jobs is a list of 'sponsor/job_id'")
    return SmokeControl(floor, tuple(cases) if cases is not None else None, tuple(jobs))


def control_from_skills(skills: Iterable[Any] | None = None) -> SmokeControl:
    """The `smoke` section of the organisation controls files (first one that has it); unset today."""
    if skills is None:
        from worker.skills import load_skills

        skills = load_skills().values()
    for skill in skills:
        for loaded in skill.pin.files.values():
            if loaded.kind == "controls" and not loaded.overlay and isinstance(loaded.data, Mapping) \
                    and loaded.data.get("smoke") is not None:
                return smoke_control(loaded.data["smoke"])
    return SmokeControl()


# ---- one case ---------------------------------------------------------------------------------------------------
@dataclass
class CaseOutcome:
    case_id: str
    role: str
    passed: bool = False
    schema_valid: bool = False
    verified: bool = False
    equal: bool = False
    hard_zeros: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _instructions(role: str) -> str:
    from worker import role_explain, role_mapper

    return {"key_resolver": role_mapper.KEY_INSTRUCTIONS, "mapper": role_mapper.MAPPER_INSTRUCTIONS,
            "explainer": role_explain.EXPLAINER_INSTRUCTIONS}[role]


def messages_for(case: SmokeCase) -> list[BaseMessage]:
    """The role's real instructions and brief layout, exactly as `role_runner.RoleRunner.call` sends them."""
    from worker import role_runner

    brief = _brief(case)
    system = f"{_instructions(case.role)}\n\n{role_runner.COMMON_RULES}"
    body = as_data(f"brief:{case.role}", role_runner.canon({
        "brief_id": brief.brief_id, "objective": brief.objective, "stop_when": brief.stop_when,
        "output_schema": brief.output_schema, "skills": list(brief.skill_slice), "data": dict(case.payload),
    }))
    return [SystemMessage(system), HumanMessage(body)]


def _brief(case: SmokeCase) -> Any:
    from worker.role_runner import make_brief

    return make_brief(case.role, f"smoke-{_sha(case.case_id)[:16]}", case.objective, dict(case.payload))


def _build(role: str, payload: Mapping[str, Any], out: Any) -> Any:
    if role == "key_resolver":
        if not isinstance(out, Mapping) or set(out) != {"picks"}:
            raise ContractError("output is {picks: {key: candidate id or null}}")
        return KeyDecisions(batch_id=str(payload.get("batch_id") or "smoke"), picks=out["picks"], voter=1)
    if role == "mapper":
        if not isinstance(out, Mapping) or set(out) - {"bindings"}:
            raise ContractError("output is {bindings: [...]}")
        return MappingPlan.from_dict({"plan_id": "plan-smoke", "record_type": str(payload.get("record_type")),
                                      "bindings": list(out.get("bindings") or [])})
    if role == "explainer":
        if not isinstance(out, Mapping):
            raise ContractError("output must be an object")
        return Explanation.from_dict({**out, "cause_id": payload.get("cause_id"), "check_no": payload.get("check_no")})
    raise ContractError(f"the smoke test has no cases for role {role}")


def decision(role: str, output: Any) -> Any:
    """The part of a role's output that is the decision, in a form that compares by value."""
    if role == "key_resolver":
        return dict(output.picks)
    if role == "mapper":
        return {b.field: b.col_id if b.action in ("map", "transform") else b.action for b in output.bindings}
    return sorted(output.cites)


def _hard_zeros(case: SmokeCase, result: Result) -> list[str]:
    from worker.role_runner import unsupported_numbers

    out: list[str] = []
    texts = [result.summary]
    payload = case.payload
    if case.role == "key_resolver":
        items = {i["key"]: i for i in payload.get("items", [])}
        for key, pick in result.output.picks.items():
            if key not in items:
                out.append(f"invented binding: key {key!r} was not asked")
            elif pick is not None and pick not in items[key]["candidates"]:
                out.append(f"pick outside the candidates for {key!r}: {pick!r}")
    elif case.role == "mapper":
        cols = {c["col_id"] for c in payload.get("columns", [])}
        fields = {f["field"] for f in payload.get("fields", [])} | set(payload.get("setup_fields") or [])
        for b in result.output.bindings:
            if b.field not in fields:
                out.append(f"invented binding: field {b.field!r} was not asked")
            if b.col_id is not None and b.col_id not in cols:
                out.append(f"invented binding: column {b.col_id!r} does not exist")
            texts.append(b.reason)
    else:
        texts.append(result.output.likely_cause)
    bad = unsupported_numbers(texts, payload)
    if bad:
        out.append(f"numbers not in the engine result: {', '.join(bad[:5])}")
    return out


def _other_problems(case: SmokeCase, result: Result, env: Any) -> list[str]:
    from worker.role_explain import PASS_WORD
    from worker.role_runner import touches_control

    problems = []
    if touches_control(result.to_dict()) or touches_control(env.alternatives):
        problems.append("the answer touches a control")
    if case.role == "explainer":
        exp = result.output
        problems += [f"cite {c!r} is not an engine row" for c in exp.cites if c not in case.payload.get("rows", {})]
        if PASS_WORD.search(exp.likely_cause) or PASS_WORD.search(result.summary):
            problems.append("the Explainer never says a check passes")
        if exp.ask is not None:
            problems.append("only code asks the human")
    return problems


def run_case(case: SmokeCase, ask: Ask) -> CaseOutcome:
    """Ask once and judge the answer. A failing model call fails the case; it never raises."""
    from worker.role_runner import Envelope, parse_answer  # lazy: roles pulls in the model stack

    class Answer(Envelope[Any]):  # any role's envelope; the output is judged here (`_build`, `_hard_zeros`)
        alternatives: Any = None

    outcome = CaseOutcome(case.case_id, case.role)
    try:
        text = str(ask(case, messages_for(case)))
    except Exception as exc:  # noqa: BLE001  the smoke test reports, it never stops start-up
        outcome.problems.append(f"the model call failed: {type(exc).__name__}: {exc}"[:300])
        return outcome
    result, env, problems = parse_answer(_brief(case), text, Answer, lambda out: _build(case.role, case.payload, out))
    if result is None:
        outcome.problems.extend(problems)
        return outcome
    outcome.schema_valid = True
    outcome.hard_zeros = _hard_zeros(case, result)
    outcome.problems = _other_problems(case, result, env)
    outcome.verified = not outcome.hard_zeros and not outcome.problems
    outcome.equal = decision(case.role, result.output) == case.expected
    if not outcome.equal:
        outcome.problems.append("the decision differs from the recorded one")
    outcome.passed = outcome.verified and outcome.equal
    return outcome


# ---- the trigger and the effect ---------------------------------------------------------------------------------
def configured_models(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The model id each model role resolves to from the settings, the way `agent.model_for` reads them. No client
    is built and no network is touched."""
    from worker.agent import DEFAULT_MODEL, ROLES, role_setting

    env = os.environ if env is None else env
    provider = (env.get("WORKER_MODEL_PROVIDER") or "openai").lower()
    if provider == "demo":
        return {r: "demo" for r in ROLES}
    return {r: f"{provider}:{role_setting(env, r) or DEFAULT_MODEL}" for r in ROLES}


def _dir(state_dir: Path) -> Path:
    return Path(state_dir) / "models"


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: Mapping[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, sort_keys=True, indent=1, ensure_ascii=False, default=str) + "\n")


def audit_log(state_dir: Path) -> AuditLog:
    return persist.audit(_dir(state_dir) / "audit.jsonl")


def status(state_dir: Path) -> dict[str, Any]:
    """The last smoke outcome for the health view: {checked_at, models, roles, warnings} or {} when none ran."""
    return _read(_dir(state_dir) / "smoke.json")


def warnings(state_dir: Path) -> list[str]:
    """The standing model warnings, for the run view. Information only: a job is never held by them."""
    return list(status(state_dir).get("warnings") or [])


def _model_role(agent_role: str) -> str:
    from worker.role_runner import ROLE_MODEL

    return ROLE_MODEL[agent_role]


def check_models(
    state_dir: Path,
    ask: Ask,
    *,
    env: Mapping[str, str] | None = None,
    models: Mapping[str, str] | None = None,
    control: SmokeControl | None = None,
    cases: Sequence[SmokeCase] | None = None,
) -> dict[str, Any]:
    """Run the smoke test for every model role whose id changed since the last recorded one, and record the
    outcome. Returns the status (`ran` False when no id changed). Warn only: nothing here can refuse a job."""
    state_dir = Path(state_dir)
    current = dict(models) if models is not None else configured_models(env)
    seen_path = _dir(state_dir) / "seen.json"
    seen = dict(_read(seen_path).get("models") or {})
    changed = {r: (seen.get(r), m) for r, m in sorted(current.items()) if seen.get(r) != m}
    if not changed:
        return {**status(state_dir), "ran": False}
    log = audit_log(state_dir)
    for role, (old, new) in changed.items():
        log.record("model.changed", f"the {role} model changed from {old or 'none recorded'} to {new}",
                   {"role": role, "old": old, "new": new})
    control = control if control is not None else control_from_skills()
    unavailable: list[str] = []
    if cases is None:
        cases = skill_cases()
        if control.jobs:
            more, unavailable = job_cases(state_dir / "replay", control.jobs)
            cases = [*cases, *more]
    if control.cases is not None:
        keep = set(control.cases)
        cases = [c for c in cases if c.case_id in keep]
    roles_out: dict[str, Any] = {}
    warns: list[str] = []
    for role, (old, new) in changed.items():
        mine = [c for c in cases if _model_role(c.role) == role]
        outcomes = [run_case(c, ask) for c in mine]
        passed = sum(o.passed for o in outcomes)
        hard = [f"{o.case_id}: {h}" for o in outcomes for h in o.hard_zeros]
        rate = Decimal(passed) / Decimal(len(outcomes)) if outcomes else None
        reasons = []
        if hard:
            reasons.append(f"{len(hard)} hard-zero violation(s), first: {hard[0]}")
        if control.pass_floor is not None and rate is not None and rate < control.pass_floor:
            reasons.append(f"pass rate {passed}/{len(outcomes)} is below the floor {control.pass_floor}")
        report = {"old": old, "new": new, "cases": len(outcomes), "passed": passed,
                  "pass_rate": str(rate.quantize(Decimal("0.001"))) if rate is not None else None,
                  "pass_floor": str(control.pass_floor) if control.pass_floor is not None else None,
                  "hard_zeros": hard[:20], "warn": bool(reasons), "reasons": reasons,
                  "failed": [asdict(o) for o in outcomes if not o.passed][:20]}
        if not outcomes:
            report["note"] = "no smoke case uses this model role"
        if reasons:
            text = f"model {new} for role {role} looks unsuitable: " + "; ".join(reasons)
            log.record("model.smoke_warn", text[:300] + " (warning only: jobs are not held, skills not changed)",
                       {"role": role, "old": old, "new": new},
                       {k: report[k] for k in ("cases", "passed", "pass_rate", "pass_floor", "hard_zeros", "reasons")})
            warns.append(text)
        else:
            log.record("model.smoke_passed", f"model {new} for role {role} passed the smoke test",
                       {"role": role, "old": old, "new": new},
                       {k: report[k] for k in ("cases", "passed", "pass_rate", "pass_floor")})
        roles_out[role] = report
    out = {"checked_at": datetime.now(UTC).isoformat(), "models": current, "roles": roles_out,
           "warnings": warns, "unavailable": unavailable, "blocking": False}
    _write(_dir(state_dir) / "smoke.json", out)
    _write(seen_path, {"models": current})
    return {**out, "ran": True}


def record_error(state_dir: Path, exc: BaseException) -> dict[str, Any]:
    """The smoke test itself failed (an unreadable case file, an audit that cannot be written): say so in the
    health view. The recorded model ids are not moved on, so the next start tries again."""
    out = {**status(state_dir), "error": f"the model smoke test could not run: {type(exc).__name__}: {exc}"[:300],
           "blocking": False}
    try:
        _write(_dir(state_dir) / "smoke.json", out)
    except OSError:
        pass
    return out


# ---- production wiring (never exercised here: see the module docstring) ---------------------------------------------
def model_ask(model_for: Callable[[str], BaseChatModel]) -> Ask:
    """An `ask` over chat models: `model_for(model_role)` returns the model the role is configured with."""

    def ask(case: SmokeCase, messages: list[BaseMessage]) -> str:
        return str(model_for(_model_role(case.role)).invoke(messages).content)

    return ask


def live_ask(env: Mapping[str, str] | None = None) -> Ask:
    """The production `ask`: `models.model_for_role` with the process settings. Live behaviour is not verified."""
    from worker.models import model_for_role

    return model_ask(lambda role: model_for_role(role, env))
