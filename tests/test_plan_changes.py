"""A change in an earlier phase does not leave later work silently standing: a decision remembers the plan it was made
under, and when the plan changes after it (a column choice, Required?, the ID decision, a rule) the decision is flagged,
Approve waits, and you say whether it still applies: keep it (audited) or re-open it (undone, audited). Fictitious data only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from worker import console_issues as CI
from worker.console import Console, ConsoleError
from worker.console_screens import state_view

WHO = "Priya Shah"


@pytest.fixture
def c(tmp_path: Path) -> Console:
    return Console(tmp_path / "inbox")


def job5(c: Console) -> dict[str, Any]:
    c.act("start|Sponsor A|Investors")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    c.act("ex|no")
    c.act("go")
    j = c.job()
    assert j is not None
    return j


def decide(c: Console, j: dict[str, Any], cause: str = "idlong") -> None:
    g = next(g for g in CI.model(c, j)["groups"] if g["cause"] == cause and g["state"] == "open")
    CI.apply(c, j, [{"id": g["id"], "option": "out", "rows": [CI.rows_page(c, j, g["id"])["rows"][0]["row"]]}], WHO)


def audit(c: Console, j: dict[str, Any]) -> list[dict[str, Any]]:
    path = c.job_dir(j) / "audit.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def view(c: Console) -> dict[str, Any]:
    return state_view(c)["job"]


def test_a_decision_is_not_stale_while_the_plan_is_as_it_was(c: Console) -> None:
    j = job5(c)
    decide(c, j)
    assert view(c)["stale"] is None


@pytest.mark.parametrize("cause, action, said", [
    ("idlong", "col|Country|Type", "the column choices"),
    ("blankreq", "req|Investors|Country|1", "Required?"),
    ("idlong", "id_decide|sequential|number them in sequence", "the ID decision"),
])
def test_changing_the_plan_after_deciding_flags_the_decision_and_blocks_approval(c: Console, cause: str, action: str, said: str) -> None:
    j = job5(c)
    decide(c, j, cause)
    c.act(action, "", WHO)
    s = view(c)["stale"]
    assert s and s["n"] == 1 and s["changed"] == [said] and view(c)["can_approve"] is False
    with pytest.raises(ConsoleError, match=f"You changed {said.replace('?', '.')} after you decided 1 thing"):
        c.act("approve", "", WHO)


def test_two_changes_are_both_named(c: Console) -> None:
    j = job5(c)
    decide(c, j, "blankreq")
    c.act("col|Country|Type", "", WHO)
    c.act("req|Investors|Country|1", "", WHO)
    assert view(c)["stale"]["changed"] == ["the column choices", "Required?"]


def test_a_change_that_cannot_touch_what_was_decided_does_not_make_it_stale(c: Console) -> None:
    j = job5(c)
    decide(c, j, "blankreq")  # a blank name does not depend on how IDs are made
    c.act("id_decide|sequential|number them in sequence", "", WHO)
    assert view(c)["stale"] is None
    decide(c, j, "idlong")  # a long ID does not depend on Required?
    c.act("req|Investors|Country|1", "", WHO)
    assert [h["cause"] for h in CI.stale(c, j)[0]] == ["blankreq"]  # only the blank-name decision depends on it


def test_keeping_the_decision_is_audited_and_clears_the_flag(c: Console) -> None:
    j = job5(c)
    decide(c, j)
    c.act("col|Country|Type", "", WHO)
    c.act("issue_keep_stale", "", WHO)
    assert view(c)["stale"] is None and j["dec"]
    kept = [a for a in audit(c, j) if a["action"] == "issues.kept"]
    assert kept and kept[-1]["inputs"]["changed"] == ["the column choices"] and kept[-1]["inputs"]["reviewer"] == WHO
    assert kept[-1]["inputs"]["decisions"] == [CI.history(j)[0]["id"]]
    c.act("col|Country|Country", "", WHO)  # a later change flags it again
    assert view(c)["stale"]


def test_reopening_undoes_the_decision_and_says_why(c: Console) -> None:
    j = job5(c)
    decide(c, j, "blankreq")
    c.act("req|Investors|Country|1", "", WHO)
    c.act("issue_reopen_stale", "", WHO)
    assert not j["dec"] and view(c)["stale"] is None and CI.history(j)[0]["reverted"]
    rev = [a for a in audit(c, j) if a["action"] == "issues.reverted"][-1]
    assert "the plan changed after it was decided" in rev["rationale"] and "Required?" in rev["rationale"]


def test_a_decision_made_after_the_change_is_not_stale_and_an_irreversible_one_can_only_be_kept(c: Console) -> None:
    j = job5(c)
    c.act("col|Country|Type", "", WHO)
    decide(c, j, "blankreq")  # made under the plan as it is now
    assert view(c)["stale"] is None
    c.act("req|Investors|Country|1", "", WHO)
    CI._hist(j)[-1]["irreversible"] = "added to the sponsor's existing records, which stay"
    assert view(c)["stale"]["can_reopen"] is False
    with pytest.raises(ConsoleError, match="nothing I can re-open"):
        c.act("issue_reopen_stale", "", WHO)
    c.act("issue_keep_stale", "", WHO)
    assert view(c)["stale"] is None


def test_a_decision_from_before_plans_were_recorded_is_never_called_stale(c: Console) -> None:
    j = job5(c)
    decide(c, j)
    CI._hist(j)[-1].pop("plan")
    c.act("col|Country|Type", "", WHO)
    assert view(c)["stale"] is None


def test_nothing_to_keep_is_said_plainly(c: Console) -> None:
    j = job5(c)
    decide(c, j)
    with pytest.raises(ConsoleError, match="nothing to keep"):
        c.act("issue_keep_stale", "", WHO)
