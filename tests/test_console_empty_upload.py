"""Approving a job whose rows were all left out used to write an upload with no records. Now it is refused, on the
record with its reason, unless a named person decided that an empty file is what is meant. Fictitious data only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from id_decision import decide_ids
from worker import console_engine as CE
from worker.console import Console, ConsoleError
from worker.console_screens import state_view


@pytest.fixture
def c(tmp_path: Path) -> Console:
    return Console(tmp_path / "inbox")


def ready_job(c: Console) -> dict[str, Any]:
    c.act("start|Sponsor A|Investors")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    c.act("ex|no")
    c.act("go")
    c.act("acceptall")
    decide_ids(c)
    j = c.job()
    assert j is not None
    return j


def leave_all_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every row of the job is left out: the tally says nothing is to be written."""
    real = CE.tally

    def tally(c: Any, j: dict[str, Any]) -> dict[str, int]:
        t = dict(real(c, j))
        t.update(out=0, skip=t["read"], err=0, warn=0)
        return t

    monkeypatch.setattr(CE, "tally", tally)


def engine_actions(c: Console) -> list[str]:
    return [e["action"] for e in CE._engine_log(c).read()]


def test_an_empty_approval_is_refused_and_nothing_is_written(c: Console, monkeypatch: pytest.MonkeyPatch) -> None:
    j = ready_job(c)
    leave_all_out(monkeypatch)
    with pytest.raises(ConsoleError, match="no records"):
        c.act("approve", "", "Priya Shah")
    assert not j["approved"] and not j.get("out")
    assert not list(c.job_dir(j).glob("*.xlsx")) and not list(c.job_dir(j).glob("*.csv"))
    refusals = [e for e in CE._engine_log(c).read() if e["action"] == "approve.refused_empty"]
    assert len(refusals) == 1 and "left out" in refusals[0]["rationale"]
    assert refusals[0]["inputs"]["by"] == "Priya Shah" and refusals[0]["inputs"]["job"] == j["id"]
    assert "write_upload" not in engine_actions(c)


def test_the_view_says_it_is_empty_and_the_approve_button_is_off(c: Console, monkeypatch: pytest.MonkeyPatch) -> None:
    ready_job(c)
    leave_all_out(monkeypatch)
    v = state_view(c, {})["job"]
    assert v["empty"] is True and v["empty_allowed"] is False and v["can_approve"] is False


def test_a_named_person_can_decide_that_an_empty_file_is_meant(c: Console, monkeypatch: pytest.MonkeyPatch) -> None:
    j = ready_job(c)
    leave_all_out(monkeypatch)
    with pytest.raises(ConsoleError, match="Add a reason"):
        c.act("allow_empty", "  ", "Priya Shah")
    with pytest.raises(ConsoleError):  # a model or a role is not a person
        c.act("allow_empty", "the sponsor has no investors yet", "agent")
    c.act("allow_empty", "the sponsor has no investors yet", "Priya Shah")
    assert j["empty_ok"]["by"] == "Priya Shah"
    assert "approve.empty_upload_allowed" in engine_actions(c)
    v = state_view(c, {})["job"]
    assert v["empty"] and v["empty_allowed"] and v["can_approve"]
    c.act("approve", "empty on purpose", "Priya Shah")
    assert j["approved"]


def test_the_decision_belongs_to_the_job_as_it_was_and_is_not_kept_after_it_changes(
        c: Console, monkeypatch: pytest.MonkeyPatch) -> None:
    j = ready_job(c)
    leave_all_out(monkeypatch)
    c.act("allow_empty", "no investors yet", "Priya Shah")
    j["empty_ok"]["eval_key"] = "a-different-evaluation"  # the job was evaluated differently when it was decided
    with pytest.raises(ConsoleError, match="no records"):
        c.act("approve", "", "Priya Shah")


def test_a_job_with_rows_is_not_asked(c: Console) -> None:
    j = ready_job(c)
    v = state_view(c, {})["job"]
    assert v["empty"] is False
    with pytest.raises(ConsoleError, match="rows to write"):
        c.act("allow_empty", "why", "Priya Shah")
    c.act("approve", "", "Priya Shah")
    assert j["approved"] and j["written"] > 0


def test_a_file_with_no_rows_is_not_called_ready_for_approval(c: Console, monkeypatch: pytest.MonkeyPatch) -> None:
    """A header-only sheet read 0 rows and the console said "ready for your approval" (browser pass 3)."""
    real = CE.tally

    def tally(cc: Any, j: dict[str, Any]) -> dict[str, int]:
        t = dict(real(cc, j))
        t.update(read=0, out=0, skip=0, err=0, warn=0)
        return t

    monkeypatch.setattr(CE, "tally", tally)
    c.act("start|Sponsor A|Investors")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    c.act("ex|no")
    c.act("go")
    assert "ready for your approval" not in c.S["toast"]["text"] and "no rows were read" in c.S["toast"]["text"]
    chat = state_view(c, {})["job"]["chat"]
    assert "Ready for your approval" not in chat[-1]["text"] and "no rows" in chat[-1]["text"]
