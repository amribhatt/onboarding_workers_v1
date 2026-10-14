"""The chat about an open issue group understands the person's own words: the Interpreter reads a sentence into
decisions (option, rows, value, note), code validates every part, the preview shows what would change and one Apply
decides it, audited with the sentence. The model never applies anything and never retypes a value. Scripted models only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from chat_model import interpreter, issue_reading, reading
from worker import console_issues as CI
from worker.console import Console, ConsoleError

WHO = "Priya Shah"


@pytest.fixture
def c(tmp_path: Path) -> Console:
    return Console(tmp_path / "inbox")


def job5(c: Console) -> dict[str, Any]:
    c.act("start|Sponsor A|Investors")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    c.act("ex|yes")
    c.act("ex|file")
    c.act("go")
    j = c.job()
    assert j is not None
    return j


def group(c: Console, j: dict[str, Any], cause: str = "idlong") -> str:
    return next(g for g in CI.model(c, j)["groups"] if g["cause"] == cause and g["state"] == "open")["id"]


def say(c: Console, gid: str, text: str) -> None:
    c.act("say", text, WHO, {"group": gid})


def last(j: dict[str, Any]) -> dict[str, Any]:
    return j["chat"][-1]


def audit(c: Console, j: dict[str, Any]) -> list[dict[str, Any]]:
    path = c.job_dir(j) / "audit.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


SENTENCE = "row 9 should be INV-A because the client confirmed it, and leave row 10 out"


def test_one_sentence_becomes_two_decisions_previewed_then_applied_with_one_button(c: Console) -> None:
    j = job5(c)
    gid = group(c, j)
    interpreter(c, issue_reading("decide", "Row 9 gets INV-A; row 10 is left out.",
                                 {"option": "edit", "rows": [9], "value": "INV-A", "note": "the client confirmed it"},
                                 {"option": "out", "rows": [10]}))
    say(c, gid, SENTENCE)
    msg = last(j)
    assert not j["dec"], "nothing changes before Apply"
    assert "INV-A" in msg["text"] and "row 9" in msg["text"] and "row 10" in msg["text"]
    assert [o["label"] for o in msg["options"]] == ["Apply"]
    c.act(msg["options"][0]["action"], "", WHO)
    assert j["dec"]["8"].endswith("INV-A") and j["dec"]["9"].startswith("exclude")
    done = [a for a in audit(c, j) if a["action"] == "issues.decided"]
    assert len(done) == 2 and {d["inputs"]["reviewer"] for d in done} == {WHO}
    assert done[0]["inputs"]["changes"][0] == {"row": 9, "before": "HS-REF-0000000000000042", "after": "INV-A"}
    assert SENTENCE in done[0]["rationale"]  # the person's own words are the note, under their name
    undo = next(o for o in last(j)["options"] if o["label"] == "Undo")
    c.act(undo["action"], "", WHO)
    assert not j["dec"]  # one Undo takes back the whole sentence


def test_a_value_the_person_did_not_type_is_refused_and_nothing_is_proposed(c: Console) -> None:
    j = job5(c)
    gid = group(c, j)
    bad = issue_reading("decide", "Row 9 gets a tidier ID.", {"option": "edit", "rows": [9], "value": "INV-9"})
    model = interpreter(c, bad, bad)
    say(c, gid, "row 9 should be INV-A")
    assert j.get("issue_prop") is None and not j["dec"] and len(model.seen) == 2  # one repair, then refused
    assert "Nothing changed" in last(j)["text"]


@pytest.mark.parametrize("decision", [
    {"option": "out", "rows": [1]},                        # a row that is not in this group
    {"option": "delete", "rows": [9]},                     # an option the group does not offer
    {"option": "park", "rows": [9], "value": "waiting"},   # a reason the person never gave
    {"option": "fix", "rows": [9]},                        # a suggestion is for the whole group
])
def test_every_part_of_a_reading_is_checked_by_code(c: Console, decision: dict[str, Any]) -> None:
    j = job5(c)
    gid = group(c, j)
    bad = issue_reading("decide", "x", decision)
    interpreter(c, bad, bad)
    say(c, gid, "do something with row 9")
    assert j.get("issue_prop") is None and not j["dec"]


def test_a_hold_needs_the_persons_own_reason_and_the_worker_asks_when_there_is_none(c: Console) -> None:
    j = job5(c)
    gid = group(c, j)
    interpreter(c, issue_reading("ask", "Why is row 9 on hold?"))
    say(c, gid, "hold row 9")
    assert last(j)["text"] == "Why is row 9 on hold?" and not j["dec"] and j.get("issue_prop") is None
    interpreter(c, issue_reading("decide", "Hold row 9.", {"option": "park", "rows": [9], "value": "waiting on the client"}))
    say(c, gid, "hold row 9, waiting on the client")
    c.act(last(j)["options"][0]["action"], "", WHO)
    assert j["dec"]["8"].startswith("exclude: on hold: waiting on the client")


def test_a_sentence_about_something_else_goes_on_to_the_ordinary_chat(c: Console) -> None:
    j = job5(c)
    gid = group(c, j)
    interpreter(c, issue_reading("other", "That is a rule."),
                reading("propose", "Treat Ltd and Limited as the same word.", rule="ltd_limited", value="on"))
    say(c, gid, "treat Limited and Ltd as one company")
    assert j["draft"]["rule"] == ("suffix", True) and not j["dec"]
    assert [m["text"] for m in j["chat"] if m["who"] == "you"].count("treat Limited and Ltd as one company") == 1


def test_an_answer_is_shown_and_changes_nothing(c: Console) -> None:
    j = job5(c)
    gid = group(c, j)
    interpreter(c, issue_reading("answer", "Both are longer than Ledgerline allows."))
    say(c, gid, "what is wrong with these?")
    assert last(j)["text"] == "Both are longer than Ledgerline allows." and not j["dec"] and j.get("issue_prop") is None


def test_an_old_proposal_cannot_be_applied_after_a_newer_one(c: Console) -> None:
    j = job5(c)
    gid = group(c, j)
    interpreter(c, issue_reading("decide", "Skip row 10.", {"option": "out", "rows": [10]}))
    say(c, gid, "skip row 10")
    first = last(j)["options"][0]["action"]
    interpreter(c, issue_reading("decide", "Skip row 9.", {"option": "out", "rows": [9]}))  # the model is scripted per sentence
    say(c, gid, "skip row 9")
    with pytest.raises(ConsoleError, match="not current"):
        c.act(first, "", WHO)
    assert not j["dec"]
    c.act(last(j)["options"][0]["action"], "", WHO)
    assert list(j["dec"]) == ["8"]


def test_the_label_chat_still_works_without_a_model(c: Console) -> None:
    j = job5(c)
    gid = group(c, j)
    say(c, gid, "Leave these out")  # a suggestion's own words: no model is asked
    assert "Apply" in [o["label"] for o in last(j)["options"]]
