from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from scripted_model import ScriptedModel, approve, call, say
from worker.accounts import key_of
from worker.agent import build_agent, decide, start
from worker.audit import AuditLog
from worker.context import RunContext
from worker.feedback import FeedbackError, FeedbackStore, draft, make_record, record, route
from worker.memory import SponsorMemory
from worker.staging import Stage


def _rec(**kw: Any) -> dict[str, Any]:
    base = {"kind": "suggestion", "action": "override", "sponsor": "sponsor-a", "job": "job1"}
    return make_record(**{**base, **kw})


def test_an_override_with_no_comment_still_creates_a_record(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    store = FeedbackStore(tmp_path / "fb", "sponsor-a")
    rec = record(audit, store, kind="suggestion", action="override", sponsor="sponsor-a", job="job1",
                 shown="42020", chosen="15910", skill="map-accounts@1.0", table_row="L3")
    entry = audit.read()[0]
    assert entry["action"] == "feedback" and "no comment given" in entry["rationale"]
    assert entry["inputs"]["chosen"] == "15910" and rec["reason"] == ""
    assert store.list()[0]["skill"] == "map-accounts@1.0"


def test_scope_defaults_to_the_job_and_bad_values_are_refused() -> None:
    assert _rec()["scope"] == "job"
    assert _rec(scope="sponsor")["scope"] == "sponsor"
    with pytest.raises(FeedbackError):
        _rec(scope="world")
    with pytest.raises(FeedbackError):
        _rec(action="love")
    with pytest.raises(FeedbackError, match="comment needs"):
        _rec(action="comment")
    with pytest.raises(FeedbackError):
        make_record(kind="row", action="flag", sponsor="", job="j")


@pytest.mark.parametrize(
    ("fields", "target"),
    [
        ({"kind": "suggestion", "skill": "map-accounts@1.0", "table_row": "L3"}, "table_row"),
        ({"kind": "mapping", "concept": "Inv ID means Investor_ID"}, "synonym"),
        ({"kind": "suggestion", "skill": "map-accounts@1.0"}, "skill_edit"),
        ({"kind": "chat", "action": "comment", "reason": "you read the wrong sheet", "skill": "map-accounts@1.0"},
         "skill_edit"),
        ({"kind": "row", "action": "flag", "check": "id_too_long"}, "table_row"),
        ({"kind": "cell", "action": "flag"}, "ticket"),
        ({"kind": "outcome", "action": "flag", "reason": "Ledgerline rejected 3 lines", "outcome": "rejected"}, "ticket"),
    ],
)
def test_routing_picks_the_target_from_the_attachment(fields: dict[str, Any], target: str) -> None:
    assert route(_rec(**fields))["target"] == target


def test_the_model_only_drafts_the_wording() -> None:
    rec = _rec(skill="map-accounts@1.0", table_row="L3", shown="42020", chosen="15910", reason="it is a fair value")
    plain = draft(rec)
    assert plain["target"] == "table_row" and "15910" in plain["wording"]
    worded = draft(rec, writer=lambda prompt: "Add a row: Valuation -> 15910.")
    assert worded["target"] == "table_row" and worded["wording"] == "Add a row: Valuation -> 15910."


def test_the_sponsor_log_is_hash_chained_and_filterable(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    store = FeedbackStore(tmp_path / "fb", "sponsor-a")
    for action, skill in [("override", "map-accounts@1.0"), ("accept", "map-accounts@1.0"), ("flag", None)]:
        record(audit, store, kind="suggestion", action=action, sponsor="sponsor-a", job="j", skill=skill)
    assert store.log.verify() == (True, 3) and audit.verify()[0]
    assert len(store.list(skill="map-accounts")) == 2 and len(store.list(action="flag")) == 1
    other = FeedbackStore(tmp_path / "fb", "sponsor-b")
    assert other.list() == []  # a sponsor never sees another's feedback


def _ctx(tmp_path: Path, memory: bool = False) -> RunContext:
    out = tmp_path / "job1" / "out"
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    book = openpyxl.Workbook()
    ws = book.active
    ws.append(["Account", "Type", "Amount"])
    ws.append(["3000 Contributions", "Capital call", "10"])
    book.save(inbox / "gl.xlsx")
    return RunContext(
        sponsor="sponsor-a", gp_id="GP-001", inbox_roots=[inbox], out_dir=out, audit=AuditLog(out / "audit.jsonl"),
        stage=Stage(out), accounts_root=tmp_path / "state" / "accounts",
        feedback=FeedbackStore(tmp_path / "state" / "feedback", "sponsor-a"),
        memory=SponsorMemory(tmp_path / "memory", "sponsor-a") if memory else None,
    )


def test_overriding_a_suggestion_in_a_job_is_recorded_with_skill_and_row_and_stored_with_the_case(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, memory=True)
    lift = call("lift", path="gl.xlsx", sheet="Sheet", template="GL",
                columns={"Account_Name": "A", "Transaction_Type": "B", "Total_Amount": "C"},
                constants={"GP_ID": "GP-001"}, header_row=1, first_row=2, why="copy")
    key = key_of("", "3000 Contributions", "Capital call")
    script = [
        say("lift", lift),
        say("map", call("map_accounts", stage_id="s1", why="map")),
        say("note", call("note_correction", text="No, contributions go to the fee-offset account", why="human corrected",
                         scope="sponsor")),
        say("confirm", call("confirm_account_mappings", stage_id="s2", picks={key: "31020"}, why="human chose 31020")),
        say("write", call("write_upload", stage_id="s3", template="GL", why="deliver")),
        say("done"),
    ]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    start(agent, ctx, "map", "t1")
    decide(agent, ctx, "t1", {"type": "approve"}, "yes", {"reviewer": "reviewer-a"})
    approve(agent, ctx, "t1")
    items = FeedbackStore(tmp_path / "state" / "feedback", "sponsor-a").list()
    by_kind = {i["kind"]: i for i in items}
    assert by_kind["chat"]["scope"] == "sponsor" and "fee-offset" in by_kind["chat"]["reason"]
    sug = by_kind["suggestion"]
    assert (sug["action"], sug["skill"], sug["table_row"], sug["chosen"]) == ("override", "map-accounts@1.0", "L3", "31020")
    assert sug["target"] == "table_row"
    # every corrected job is stored as a replay case that carries what was corrected and why
    case = ctx.memory.cases()[0]  # type: ignore[union-attr]
    assert {c["kind"] for c in case["corrections"]} == {"chat", "suggestion"}
    assert "answer_key" not in str(case)


def test_a_rejected_proposal_is_recorded_as_feedback(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    lift = call("lift", path="gl.xlsx", sheet="Sheet", template="GL", columns={"Account_Name": "A"},
                constants={"GP_ID": "GP-001"}, header_row=1, first_row=2, why="copy")
    script = [say("lift", lift), say("write", call("write_upload", stage_id="s1", template="GL", why="deliver")),
              say("ok")]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    start(agent, ctx, "go", "t1")
    decide(agent, ctx, "t1", {"type": "reject", "message": "Wrong sheet, use the Data Source tab"})
    fb = [e for e in ctx.audit.read() if e["action"] == "feedback"]
    assert fb and fb[0]["inputs"]["action"] == "reject" and "Data Source" in fb[0]["rationale"]
    assert fb[0]["inputs"]["record_type"] == "GL"
