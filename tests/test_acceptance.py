"""Section 10 of docs/design-onboarding-assistant.md, one test per scenario id, on the sample world of the mockup.

The console is driven through `Console.act` (the same calls the page makes) and every number is read from the
state view the page renders. No model is called anywhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chat_model import interpreter, reading
from id_decision import decide_ids
from worker import console_engine as CE
from worker.console import Console, ConsoleError
from worker.console_screens import state_view


@pytest.fixture
def c(tmp_path: Path) -> Console:
    return Console(tmp_path / "inbox")


def start(c: Console, sponsor: str, typ: str, cov: str = "all", fund: str | None = None) -> dict[str, Any]:
    c.act(f"start|{sponsor}|{typ}")
    if typ.startswith("GL"):
        if fund:
            c.act(f"ask_set|fund|{fund}")
    else:
        c.act(f"ask_set|cov|{cov}")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    return j


def need(c: Console) -> int:
    return CE.tally(c, c.job())["err"]  # type: ignore[arg-type]


def answer_no(c: Console) -> None:
    c.act("ex|no")


def approve_investors(c: Console) -> None:
    j = start(c, "Sponsor A", "Investors")
    c.act("ex|no")
    c.act("go")
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    assert j["approved"]


# ---- A. Starting a job in the chat ----------------------------------------------------------------------
def test_A1_start_opens_the_chat_not_a_dialog(c: Console) -> None:
    st = c.act("start|Sponsor A")
    assert st["view"] == "job" and st["job"] is None and st["ask"] is not None
    assert st["ask"]["next"]["text"].startswith("What do you need?")
    assert st["ask"]["ready"] is False and st["ask"]["need"] == ["what to build", "the source file"]


def test_A2_a_sentence_sets_the_record_type(c: Console) -> None:
    c.act("start|Sponsor A")
    st = c.act("say", "build the investor upload")
    assert [m["text"] for m in st["ask"]["messages"] if m["who"] == "w"] == ["Got it: Investors."]
    assert st["ask"]["type"] == "Investors"
    assert st["ask"]["next"]["text"] == "Drop the investors source file here."


def test_A3_the_sample_file_asks_which_entities_it_covers(c: Console) -> None:
    c.act("start|Sponsor A")
    c.act("say", "build the investor upload")
    st = c.act("ask_sample")
    assert st["ask"]["file"] == "Sponsor_A_Common_Investors.xlsx"
    nxt = st["ask"]["next"]
    assert nxt["text"] == "Which entities does Sponsor_A_Common_Investors.xlsx cover?"
    assert [o["label"] for o in nxt["options"]] == ["All 3", "Fund A", "Fund B", "Fund C", "Some of them"]


def test_A4_one_sentence_carries_everything(c: Console) -> None:
    c.act("start|Sponsor A")
    st = c.act("say", "build the investor upload for all entities from the sample")
    assert st["ask"]["ready"] and st["ask"]["need"] == []
    st = c.act("say", "go")
    assert st["job"] is not None and st["job"]["type"] == "Investors" and st["ask"] is None


def test_A5_clicking_and_chatting_change_the_same_state(c: Console) -> None:
    c.act("start|Sponsor A")
    c.act("ask_set|type|Investors")
    c.act("ask_set|cov|all")
    clicked = c.act("ask_sample")["ask"]
    c.act("start|Sponsor A")
    spoken = c.act("say", "build the investor upload for all entities from the sample")["ask"]
    for k in ("type", "cov", "file", "ready", "route", "template"):
        assert clicked[k] == spoken[k], k


# ---- B. Records already in Ledgerline -------------------------------------------------------------------------
def test_B1_the_question_comes_before_the_plan(c: Console) -> None:
    start(c, "Sponsor A", "Investors")
    st = state_view(c)
    job = st["job"]
    assert job["ex_pending"] and "issues" not in job and not job["ran"]
    assert job["timeline"][1] == {"stage": "Map", "state": 2, "fact": "a question for you"}
    asked = [m for m in job["chat"] if "loaded into Ledgerline outside this app" in m["text"]]
    assert asked and [o["label"] for o in asked[0]["options"]][:2] == ["Yes, I'll upload the export", "No, go ahead"]
    with pytest.raises(ConsoleError):
        decide_ids(c)
        c.act("approve", who="reviewer-a")


def test_B2_the_export_leaves_three_out_and_brings_two_clashes(c: Console) -> None:
    start(c, "Sponsor A", "Investors")
    c.act("ex|yes")
    c.act("ex|file")
    st = c.act("go")["job"]
    assert st["tally"] == {"err": 5, "warn": 3, "out": 7, "skip": 3, "read": 15}
    rows = {r["i"]: r for r in CE.static_rows(c, c.job())}  # type: ignore[arg-type]
    assert [rows[i]["st"] for i in (3, 4, 5)] == ["skip", "skip", "skip"]
    assert rows[6]["why"] == "Same key on file under another name"
    assert rows[7]["why"] == "Same name on file under another key"
    assert rows[12]["st"] == "warn"  # Northgate Resources Ltd vs Limited: only a clash once the rule exists
    read = [m for m in st["chat"] if m["text"].startswith("Read 6 investors from Ledgerline_Investors_Export.xlsx")]
    assert read
    interpreter(c, reading("propose", "Treat Ltd and Limited as the same word.", rule="ltd_limited", value="on"))
    c.act("say", "Ltd = Limited")
    c.act("save|sponsor")
    rows = {r["i"]: r for r in CE.static_rows(c, c.job())}  # type: ignore[arg-type]
    assert rows[12]["st"] == "err" and rows[12]["why"] == "Same name if Ltd = Limited, under another key"


def test_B3_no_writes_them_as_new_and_it_is_not_asked_again(c: Console) -> None:
    j = start(c, "Sponsor A", "Investors")
    c.act("ex|no")
    t = c.act("go")["job"]["tally"]
    assert t["skip"] == 0 and t["err"] == 3 and t["read"] == 15  # rows 3 to 7 are written as new
    c.act("start|Sponsor A|Investors")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    assert c.job()["exAsk"] is False and c.job() is not j  # type: ignore[index]


def test_B4_go_ahead_without_it_marks_the_run_and_asks_again(c: Console) -> None:
    start(c, "Sponsor A", "Investors")
    c.act("ex|unsure")
    c.act("ex|skip")
    job = c.act("go")["job"]
    assert "not checked against records loaded outside the app" in job["store_text"]
    start(c, "Sponsor A", "Investors")
    assert c.job()["exAsk"] and c.ex_pending(c.job())  # type: ignore[arg-type]


def test_B5_the_store_says_where_its_count_comes_from(c: Console) -> None:
    assert c.store_text("Investors", "Sponsor B") == (
        "5 investors already in Ledgerline: 5 from the approved B Fund I investors job · none loaded outside the "
        "app, you said"
    )
    assert c.store_text("Investors", "Sponsor A").startswith("0 investors already in Ledgerline")


# ---- D. Counts always agree ----------------------------------------------------------------------------------
def test_D1_plan_rows_equal_read_rows(c: Console) -> None:
    start(c, "Sponsor A", "Investors")
    c.act("ex|no")
    st = c.act("go")["job"]
    assert st["plan"]["rows"] == 15 == st["tally"]["read"]


def test_D2_read_equals_written_plus_left_out_plus_need_you(c: Console) -> None:
    start(c, "Sponsor A", "Investors")
    c.act("ex|yes")
    c.act("ex|file")
    t = c.act("go")["job"]["tally"]
    assert (t["read"], t["out"], t["skip"], t["err"]) == (15, 7, 3, 5) and t["read"] == t["out"] + t["skip"] + t["err"]
    st = c.act("acceptall")["job"]
    t = st["tally"]
    assert t["err"] == 0 and t["read"] == t["out"] + t["skip"]
    assert st["can_approve"] and st["output"]["ties"]


def _need_you_everywhere(c: Console, expected: int) -> None:
    st = state_view(c)["job"]
    assert st["tally"]["err"] == expected
    assert st["timeline"][3]["fact"] == f"{expected} need you"
    assert any(f"{expected} " in m["text"] and "need your call" in m["text"] for m in st["chat"])


def test_D3_need_you_agrees_on_the_timeline_the_issues_and_the_chat(c: Console) -> None:
    # Investors (with the export)
    start(c, "Sponsor A", "Investors")
    c.act("ex|yes")
    c.act("ex|file")
    c.act("go")
    _need_you_everywhere(c, 5)
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    # Commitments, Vendors, Investments, Affiliates: "no" to records loaded outside the app
    for typ, n in (("Commitments", 4), ("Vendors", 3), ("Investments", 2), ("Affiliates", 3)):
        start(c, "Sponsor A", typ)
        if c.ex_pending(c.job()):  # type: ignore[arg-type]
            c.act("ex|no")
        c.act("go")
        _need_you_everywhere(c, n)
        c.act("acceptall")
        decide_ids(c)
        c.act("approve", who="reviewer-a")


def test_D3_gl_fund_after_resuming(c: Console) -> None:
    c.act("prov|free")
    start(c, "Sponsor B", "GL Fund", fund="B Fund I")
    st = c.act("go")["job"]
    assert st["failed"]["stage"] == "Check" and st["timeline"][3]["state"] == 3
    st = c.act("resume")["job"]
    assert st["failed"] is None and st["tally"]["err"] == 11 and st["tally"]["read"] == 18
    _need_you_everywhere(c, 11)


def test_D4_gl_lp_says_lines_written_plus_offsets(c: Console) -> None:
    c.act("prov|paid")
    j = start(c, "Sponsor B", "GL Fund", fund="B Fund I")
    c.act("go")
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    assert j["approved"]
    start(c, "Sponsor B", "GL LP", fund="B Fund I")
    st = c.act("go")["job"]
    t = st["tally"]
    assert (t["read"], t["out"], t["err"]) == (5, 8, 1)
    c.act("acceptall")
    st = state_view(c)["job"]
    ready = [m["text"] for m in st["chat"] if m["text"].startswith("Ready for your approval")]
    assert ready and "5 written plus 5 offsets" in ready[0] and "read =" not in ready[0]


def test_D5_a_journal_corrected_in_the_source_is_not_counted_as_balanced(c: Console) -> None:
    c.act("prov|paid")
    start(c, "Sponsor B", "GL Fund", fund="B Fund I")
    c.act("go")
    j = c.job()
    for key in ("J-1003", "J-1005"):
        c.decide(c.job(), key, "corrected")
    for key in ("J-1006", "J-1007", "J-1008", "J-1009"):
        c.decide(c.job(), key, "exclude as incorrect (deal name blank)")
    c.decide(c.job(), "J-1002", "exclude as incorrect")
    c.decide(c.job(), "J-1004", "exclude as incorrect")
    facts = state_view(c)["job"]["output"]["facts"]
    assert "journals balanced" in facts and "marked corrected in the source" in facts
    assert j["dec"]["J-1003"] == "corrected"
    assert "1/3 journals balanced" in facts or "1/2 journals balanced" in facts


# ---- J. Runs, failures and approval ----------------------------------------------------------------------------
def test_J1_looks_right_runs_and_notifies_when_check_is_done(c: Console) -> None:
    start(c, "Sponsor A", "Investors")
    c.act("ex|no")
    st = c.act("go")
    assert st["toast"]["text"] == "Sponsor A · Investors: Check done · 3 need you"
    assert st["job"]["timeline"][2] == {"stage": "Convert", "state": 1, "fact": "run 1"}


def test_J2_resume_from_check_repeats_nothing_and_keeps_decisions(c: Console) -> None:
    c.act("prov|free")
    start(c, "Sponsor B", "GL Fund", fund="B Fund I")
    st = c.act("go")["job"]
    assert st["failed"]["stage"] == "Check" and st["ran"] is False
    c.decide(c.job(), "J-1003", "exclude as incorrect (out of balance)")
    j = c.job()
    assert j["dec"] and j["retries"] == 1  # type: ignore[index]
    st = c.act("resume")["job"]
    assert st["failed"] is None and len(c.job()["runs"]) == 1  # type: ignore[index]
    assert c.job()["dec"]["J-1003"].startswith("exclude")  # type: ignore[index]
    assert any("Read, Map and Convert were not repeated" in m["text"] for m in st["chat"])


def test_J3_cannot_approve_before_convert_after_failure_or_while_anything_needs_you(c: Console) -> None:
    c.act("prov|free")
    start(c, "Sponsor B", "GL Fund", fund="B Fund I")
    with pytest.raises(ConsoleError, match="Convert the file first"):
        decide_ids(c)
        c.act("approve", who="reviewer-a")
    c.act("go")
    with pytest.raises(ConsoleError, match="stopped at Check"):
        decide_ids(c)
        c.act("approve", who="reviewer-a")
    c.act("resume")
    with pytest.raises(ConsoleError, match="still need you"):
        decide_ids(c)
        c.act("approve", who="reviewer-a")


def test_J4_approval_writes_saves_advances_the_counter_and_offers_the_next_type(c: Console) -> None:
    start(c, "Sponsor A", "Investors")
    c.act("ex|yes")
    c.act("ex|file")
    c.act("go")
    c.act("acceptall")
    decide_ids(c)
    st = c.act("approve", who="reviewer-a")["job"]
    assert st["approved"] and st["written"] == 9 and st["next"] == "Commitments"
    assert c.S["wf"]["Sponsor A|Investors"] is True
    assert c.counter_of("Sponsor A", "Investors") == 10  # GINV-0001 to GINV-0009 were issued
    store = c.reg_of("Sponsor A")["Investors"]
    assert len(store) == 6 + 9 and {"id": "GINV-0001", "key": "HS-9901"}.items() <= {
        k: v for k, v in store[6].items() if k in ("id", "key")
    }.items()
    nxt = [m for m in st["chat"] if m["text"].startswith("Approved and written")]
    assert nxt and nxt[0]["options"][0]["action"] == "next|Commitments"
    c.act("next|Commitments")
    assert state_view(c)["ask"]["type"] == "Commitments"


def test_J4b_an_approved_job_keeps_its_tally_after_its_records_are_in_the_store(c: Console) -> None:
    start(c, "Sponsor A", "Investors")
    c.act("ex|no")
    c.act("go")
    c.act("acceptall")
    decide_ids(c)
    before = c.act("approve", who="reviewer-a")["job"]["output"]
    after = state_view(c)["job"]["output"]
    assert before["facts"] == after["facts"] and "0 written" not in after["facts"]
    assert state_view(c)["job"]["tally"]["out"] == c.S["jobs"][-1]["written"] > 0


# ---- K. Seed and sponsors -----------------------------------------------------------------------------------
def test_K1_sponsor_a_starts_with_nothing_approved(c: Console) -> None:
    st = state_view(c)
    a = next(s for s in st["sponsors"] if s["name"] == "Sponsor A")
    assert a["approved_jobs"] == [] and a["store"] == {"Investors": 0, "Investments": 0, "Vendors": 0, "Affiliates": 0}
    assert a["counters"] == {}
    assert c.route({"type": "Investors", "sp": "Sponsor A", "cov": "all", "fund": "Fund A"})[0].startswith("Path 1")
    assert c.route({"type": "Investors", "sp": "Sponsor A", "cov": "one", "fund": "Fund A"})[0].startswith("Path 2")


def test_K2_sponsor_b_has_five_static_jobs_approved_for_b_fund_i(c: Console) -> None:
    b = next(s for s in state_view(c)["sponsors"] if s["name"] == "Sponsor B")
    assert sorted(j["type"] for j in b["approved_jobs"]) == sorted(["Investors", "Commitments", "Investments",
                                                                    "Vendors", "Affiliates"])
    assert {j["fund"] for j in b["approved_jobs"]} == {"B Fund I"}
    assert b["counters"] == {"Investors": 6, "Commitments": 6}
    assert b["store"] == {"Investors": 5, "Investments": 1, "Vendors": 1, "Affiliates": 1}
    assert c.static_done("B Fund I", "Sponsor B") == 5 and c.static_done("B Fund II", "Sponsor B") == 3  # sponsor-level types count


def test_K3_gl_names_follow_the_jobs_sponsor_fund_and_entity(c: Console) -> None:
    c.act("start|Sponsor B|GL Fund")
    st = c.act("ask_sample")["ask"]
    assert st["file"] == "Sponsor_B_GL_BFundI_2025Q4.xlsx"
    assert "Entity ID 6100101 from B Fund I setup" in st["route"]
    assert c.S["EM"]["B Fund I"]["id"] == "6100101"


def test_K4_investors_vendors_affiliates_count_for_the_whole_sponsor(c: Console) -> None:
    start(c, "Sponsor A", "Investors", cov="one", fund="Fund A")
    c.act("ex|no")
    c.act("go")
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    assert c.static_ok("Investors", "Sponsor A", "Fund C")  # a sponsor-level type, whichever entity it came with
    assert not c.static_ok("Commitments", "Sponsor A", "Fund A")  # commitments count per fund
