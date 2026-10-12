"""The Explainer (the likely cause of one non-PASS break, citing engine rows only), its per-cause
fan-out and the offline Drafter (words a proposal from a feedback cluster; code chose the file)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from worker import role_runner as rr
from worker.context import BudgetExceeded
from worker.contracts import Explanation, Result

EXPLAINER_INSTRUCTIONS = """You are the Explainer. Say in plain words the likely cause of this break and what to ask
the client for. Cite the engine rows you rely on by their ids. Do not restate a figure unless it is in those rows;
never say the check passes; never change a severity.
JSON: {"summary": text, "confidence": number, "output": {"likely_cause": text, "cites": [row id, ...]}}"""

PASS_WORD = re.compile(r"\bPASS(ES|ED)?\b")


def explain(
    runner: rr.RoleRunner,
    *,
    unit_id: str,
    cause_id: str,
    rows: Mapping[str, Mapping[str, Any]],
    check_no: int | None = None,
    know_how: str = "",
) -> rr.RoleOutcome:
    payload = {"cause_id": cause_id, "check_no": check_no, "rows": {k: dict(v) for k, v in rows.items()},
               "know_how": know_how}
    brief = rr.make_brief("explainer", unit_id, f"Explain break {cause_id}", payload)

    def build(out: dict[str, Any]) -> Explanation:
        return Explanation.from_dict({**out, "cause_id": cause_id, "check_no": check_no})

    def verify(result: Result, env: Any) -> list[str]:
        exp: Explanation = result.output
        problems = [f"cite {c!r} is not an engine row" for c in exp.cites if c not in rows]
        bad = rr.unsupported_numbers([exp.likely_cause], engine_facts)
        if bad:
            problems.append(f"the explanation states numbers that are not in the engine result: {', '.join(bad[:5])}")
        if PASS_WORD.search(exp.likely_cause) or PASS_WORD.search(result.summary):
            problems.append("the Explainer never says a check passes; code sets the status")
        if exp.ask is not None:
            problems.append("only code asks the human; leave ask empty")
        return problems

    # The numeric-claim lint: a number the Explainer states must be one of the engine rows it was given. The
    # know-how text and the ids of the brief are not facts (M21), so a figure only they hold is refused.
    engine_facts = {"rows": payload["rows"], "check_no": check_no}
    attempt = runner.call(brief, payload, instructions=EXPLAINER_INSTRUCTIONS, build=build, verify=verify,
                          facts=engine_facts, envelope=rr.Envelope[dict[str, Any]])
    if attempt.result is None:
        q = rr.no_answer_question(unit_id, cause_id, rr.sha(payload), "explainer",
                                "No explanation could be verified; the break rows are shown as they are.")
        return rr.outcome(brief, attempt, None, [q])
    exp = attempt.result.output
    return rr.outcome(brief, attempt, exp, output=exp)


NON_PASS = ("ERR", "WARN")
EXPLAIN_LIMIT = 6  # causes one evaluation hands to the Explainer; the rest are reported as not explained
EXPLAIN_WORKERS = 4  # calls in flight at once (they share the job's budget and audit)


@dataclass(frozen=True)
class CauseExplanation:
    """What the person sees beside a finding. `status` is `explained` (verified), `unverified` (no answer passed
    the lint after one repair: the engine's own message stands alone) or `skipped` (cap or spend cap)."""

    cause_id: str
    status: str
    text: str
    cites: tuple[str, ...] = ()
    attempts: int = 0


def cause_rows(finding: Any, tie_out: Mapping[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """The engine rows one cause is explained from, keyed by the id an explanation may cite: the finding itself
    (rule, severity, field, count, where, the engine's message) and the run's tie-out when it has one."""
    row: dict[str, Any] = {
        "rule": finding.rule.rule_id, "severity": finding.severity, "field": finding.field,
        "count": finding.count, "where": finding.ranges_text, "message": finding.message,
        "sample_rows": [int(s) for s in finding.sample],
    }
    rows: dict[str, dict[str, Any]] = {str(finding.id): row}
    if tie_out:
        rows["tie_out"] = {k: str(v) if isinstance(v, Decimal) else v for k, v in tie_out.items()}
    return rows


def explain_causes(
    runner: rr.RoleRunner,
    findings: Sequence[Any],
    *,
    unit_id: str,
    tie_out: Mapping[str, Any] | None = None,
    know_how: str = "",
    limit: int = EXPLAIN_LIMIT,
    workers: int = EXPLAIN_WORKERS,
) -> dict[str, CauseExplanation]:
    """One Explainer hand-off per non-PASS cause (a finding is a cause: one rule on one field, with its count and
    rows), run in parallel under the runner's budget and hand-off audit. Each answer passed the numeric-claim
    lint in `explain`: every number it states is in the engine rows of its cause. A cause beyond `limit` is not
    sent and says so. Returns {cause_id: CauseExplanation} in the findings' order."""
    causes: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for f in findings:
        key = (f.rule.rule_id, f.field)
        if f.severity in NON_PASS and key not in seen:
            seen.add(key)
            causes.append(f)
    out: dict[str, CauseExplanation] = {}
    sent = causes[: max(0, limit)]
    for f in causes[len(sent):]:
        out[str(f.id)] = CauseExplanation(str(f.id), "skipped", f"not explained: only {limit} causes are sent")

    def one(f: Any) -> CauseExplanation:
        cid = str(f.id)
        try:
            res = explain(runner, unit_id=unit_id, cause_id=cid, rows=cause_rows(f, tie_out), know_how=know_how)
        except BudgetExceeded:
            return CauseExplanation(cid, "skipped", "not explained: the job's model budget is used up")
        if res.decided is None:
            return CauseExplanation(cid, "unverified", "no explanation passed the checks; the finding stands as is",
                                    (), res.attempts)
        return CauseExplanation(cid, "explained", res.decided.likely_cause, res.decided.cites, res.attempts)

    if len(sent) > 1 and workers > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(workers, len(sent))) as pool:
            done = list(pool.map(one, sent))
    else:
        done = [one(f) for f in sent]
    for item in done:
        out[item.cause_id] = item
    return {str(f.id): out[str(f.id)] for f in causes}


DRAFTER_INSTRUCTIONS = """You are the Drafter. Word the smallest edit to the given skill file that the feedback
examples ask for, in one short paragraph in "summary". Do not choose another file. Never touch a control, a script,
a prompt or a test case. Output is null.
JSON: {"summary": text, "confidence": number, "output": null}"""

_FORBIDDEN_WORDS = re.compile(r"controls\.yaml|prompts\.py|\bscripts/|\bcases/|\.py\b", re.IGNORECASE)


def draft(
    runner: rr.RoleRunner,
    *,
    cluster_id: str,
    target_file: str,
    proposal_kind: str,
    examples: Sequence[Mapping[str, Any]],
) -> rr.RoleOutcome:
    payload = {"cluster_id": cluster_id, "target_file": target_file, "proposal_kind": proposal_kind,
               "examples": [dict(e) for e in examples]}
    brief = rr.make_brief("drafter", cluster_id, f"Word a {proposal_kind} proposal for {target_file}", payload)

    def verify(result: Result, env: Any) -> list[str]:
        if _FORBIDDEN_WORDS.search(result.summary):
            return ["the wording names a control, a script, a prompt or a case; those are never edited here"]
        return []

    attempt = runner.call(brief, payload, instructions=DRAFTER_INSTRUCTIONS, build=lambda out: None, verify=verify,
                          facts=payload, envelope=rr.Envelope[None])
    if attempt.result is None:
        q = rr.no_answer_question(cluster_id, cluster_id, rr.sha(payload), "drafter",
                                "No wording could be verified; write the proposal by hand.")
        return rr.outcome(brief, attempt, None, [q])
    return rr.outcome(brief, attempt, attempt.result.summary)
