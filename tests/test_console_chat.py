"""The job chat reads plain language: the model proposes one rule or one question, code builds the draft, a person saves."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chat_model import interpreter, reading
from worker import console_chat as CC
from worker.console import Console
from worker.console_screens import state_view


@pytest.fixture
def c(tmp_path: Path) -> Console:
    return Console(tmp_path / "inbox")


def job(c: Console) -> dict[str, Any]:
    c.act("start|Sponsor A|Investors")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    return j


def last(j: dict[str, Any]) -> dict[str, Any]:
    return j["chat"][-1]


def test_a_natural_sentence_becomes_a_draft_the_person_saves(c: Console) -> None:
    j = job(c)
    interpreter(c, reading("propose", "Treat Ltd and Limited as the same word.", rule="ltd_limited", value="on"))
    c.act("say", "if a company says Limited in one file and Ltd in another it is one company")
    assert j["draft"]["rule"] == ("suffix", True) and j["rules"]["suffix"] is False
    c.act("save|job")
    assert j["rules"]["suffix"] is True


def test_a_length_in_words_is_a_number_by_code(c: Console) -> None:
    j = job(c)
    interpreter(c, reading("propose", "Keep names to sixty characters.", rule="name_length", value=60))
    c.act("say", "names can be up to sixty characters")
    assert j["draft"]["rule"] == ("nameMax", 60) and j["draft"]["name"] == "Name length"


def test_an_unclear_sentence_asks_one_question_and_changes_nothing(c: Console) -> None:
    j = job(c)
    before = dict(j["rules"])
    interpreter(c, reading("ask", "Do you mean the investor name or the investor ID?"))
    c.act("say", "make it shorter")
    assert j["draft"] is None and j["rules"] == before
    assert last(j)["text"] == "Do you mean the investor name or the investor ID?"


def test_something_outside_the_rules_points_at_the_screen(c: Console) -> None:
    j = job(c)
    interpreter(c, reading("cannot", "I can't change the file's columns here; the plan has them.", where="Plan"))
    c.act("say", "move the amount column")
    assert j["draft"] is None
    assert last(j)["options"][0] == {"label": "Open Plan", "action": "goto|plan"}


def test_a_rule_that_is_not_in_the_set_is_refused_after_one_repair(c: Console) -> None:
    j = job(c)
    bad = reading("propose", "Delete the file.", rule="delete_everything", value="on")
    model = interpreter(c, bad, bad)
    c.act("say", "get rid of it")
    assert j["draft"] is None and len(model.seen) == 2
    assert "Nothing changed" in last(j)["text"]


def test_an_id_decision_needs_the_persons_reason_and_their_name(c: Console) -> None:
    j = job(c)
    interpreter(c, reading("propose", "Number them in sequence.", rule="id_decision", value="sequential"))
    c.act("say", "just number them")
    assert c.id_decision("Sponsor A", "Investors") is None
    assert last(j)["options"] == [] and "Give a reason" in last(j)["text"]
    interpreter(c, reading("propose", "Number them in sequence.", rule="id_decision", value="sequential",
                           reason="the client has no numbering of its own"))
    c.act("say", "number them, the client has no numbering of its own")
    apply = last(j)["options"][0]
    assert apply["label"] == "Apply" and c.id_decision("Sponsor A", "Investors") is None
    c.act(apply["action"], who="reviewer-a")
    held = c.id_decision("Sponsor A", "Investors")
    assert held and held["by"] == "reviewer-a" and held["reason"] == "the client has no numbering of its own"
    assert state_view(c)["job"]["chat"]


def test_the_agent_cannot_make_the_id_decision(c: Console) -> None:
    job(c)
    with pytest.raises(Exception, match="not a person|person's name|reviewer|approver"):
        c.act("id_decide|sequential|because", who="agent")
    assert c.id_decision("Sponsor A", "Investors") is None


def test_a_model_that_cannot_be_reached_is_said_plainly(c: Console) -> None:
    j = job(c)
    interpreter(c)  # an empty script: the call fails
    c.act("say", "names up to 40")
    assert j["draft"] is None and "couldn't reach a model" in last(j)["text"]


def test_each_message_leaves_one_trace_without_the_sentence_or_the_answer(c: Console) -> None:
    job(c)
    interpreter(c, reading("propose", "Keep names to sixty characters.", rule="name_length", value=60))
    c.act("say", "names can be up to sixty characters, said Priya")
    bad = reading("propose", "Delete the file.", rule="delete_everything", value="on")
    interpreter(c, bad, bad)
    c.act("say", "get rid of it")
    got = CC.traces(c)
    assert [t["outcome"] for t in got] == ["refused", "read"] and [t["attempts"] for t in got] == [2, 1]
    assert got[0]["problems"] and len({t["trace"] for t in got}) == 2
    raw = (Path(c.root) / "console_chat.audit.jsonl").read_text(encoding="utf-8")
    assert "Priya" not in raw and "sixty characters" not in raw


def test_the_chat_model_is_small_fast_to_fail_and_built_once() -> None:
    from worker.agent import model_for

    env = {"WORKER_MODEL_PROVIDER": "openai", "OPENAI_API_KEY": "k", "WORKER_MODEL": "big", "WORKER_MODEL_SMALL": "tiny"}
    chat = CC._model(env)
    assert CC._model(env) is chat and chat.model_name == "tiny"
    assert (chat.request_timeout, chat.max_retries) == (CC.CHAT_TIMEOUT, CC.CHAT_RETRIES)
    shared = model_for("small", env)  # the roles' own client keeps the long timeout and the retries
    assert shared is not chat and (shared.request_timeout, shared.max_retries) == (90, 4)


def _fb(c: Console, j: dict[str, Any]) -> list[dict[str, Any]]:
    from worker import persist
    from worker import templates as T

    return persist.feedback(Path(c.root) / "feedback", T.sponsor_slug(j["sp"])).list(kind="chat")


def test_every_reading_can_be_rated_and_only_the_latest(c: Console) -> None:
    j = job(c)
    interpreter(c, reading("ask", "Names or IDs?"))
    c.act("say", "make it shorter")
    assert [o["action"] for o in last(j)["options"]] == ["chat_fb|up", "chat_fb|down"]
    interpreter(c, reading("propose", "Names to sixty.", rule="name_length", value=60))
    c.act("say", "names up to sixty")
    rated = [m for m in j["chat"] if any(o["action"].startswith("chat_fb|") for o in m.get("options", []))]
    assert rated == [last(j)]


def test_a_thumbs_up_teaches_the_next_reading_for_that_sponsor_only(c: Console) -> None:
    j = job(c)
    interpreter(c, reading("propose", "Names to sixty.", rule="name_length", value=60))
    c.act("say", "keep names to sixty")
    c.act("chat_fb|up", who="reviewer-a")
    assert [r["action"] for r in _fb(c, j)] == ["accept"]
    model = interpreter(c, reading("ask", "Which field?"))
    c.act("say", "something else")
    sent = str(model.seen[0][-1].content)
    assert "keep names to sixty" in sent and "name_length" in sent
    other = job_for(c, "Sponsor B")
    model_b = interpreter(c, reading("ask", "Which field?"))
    c.act("say", "something else")
    assert other and "keep names to sixty" not in str(model_b.seen[0][-1].content)


def job_for(c: Console, sponsor: str) -> dict[str, Any] | None:
    c.act(f"start|{sponsor}|Investors")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    return c.job()


def test_thumbs_down_then_the_saved_correction_becomes_the_example(c: Console) -> None:
    j = job(c)
    interpreter(c, reading("propose", "Cut IDs.", rule="id_length", value="truncate"))
    c.act("say", "make the long ones fit")
    c.act("chat_fb|down", who="reviewer-a")
    assert "what you meant" in last(j)["text"] and _fb(c, j)[-1]["action"] == "reject"
    interpreter(c, reading("propose", "Names to forty.", rule="name_length", value=40))
    c.act("say", "I mean the names, forty characters")
    c.act("save|sponsor", who="reviewer-a")
    got = _fb(c, j)
    assert [r["action"] for r in got] == ["reject", "override"]
    assert got[1]["shown"]["sentence"] == "make the long ones fit" and got[1]["chosen"] == {"rule": "name_length", "value": 40}
    assert CC.learned(c, j) == [{"sentence": "make the long ones fit", "rule": "name_length", "value": 40}]


def test_a_thumbs_down_alone_is_never_an_example_and_nothing_to_rate_is_refused(c: Console) -> None:
    j = job(c)
    with pytest.raises(Exception, match="nothing to rate"):
        c.act("chat_fb|up", who="reviewer-a")
    interpreter(c, reading("propose", "Names to sixty.", rule="name_length", value=60))
    c.act("say", "names to sixty")
    c.act("chat_fb|down", who="reviewer-a")
    assert CC.learned(c, j) == []


def test_a_weak_cheap_reading_goes_once_to_the_stronger_model(c: Console) -> None:
    j = job(c)
    small = interpreter(c, reading("ask", "Which one?", confidence=0.3),
                        stronger=(reading("propose", "Names to sixty.", rule="name_length", value=60),))
    c.act("say", "names up to sixty")
    assert j["draft"]["rule"] == ("nameMax", 60) and len(small.seen) == 1
    got = CC.traces(c)
    assert len(got) == 1 and got[0]["escalated"] is True and got[0]["outcome"] == "read"


def test_a_confident_cheap_reading_never_calls_the_stronger_model(c: Console) -> None:
    j = job(c)
    interpreter(c, reading("propose", "Names to sixty.", rule="name_length", value=60),
                stronger=(reading("ask", "should not be used"),))
    c.act("say", "names up to sixty")
    assert j["draft"]["rule"] == ("nameMax", 60) and CC.traces(c)[0]["escalated"] is False
