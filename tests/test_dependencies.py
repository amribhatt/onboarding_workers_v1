from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from scripted_model import ScriptedModel, approve, call, say
from worker.agent import GateRefused, build_agent, check_approval, decide, pending_gate, start
from worker.audit import AuditLog
from worker.context import RunContext
from worker.registry import Registry
from worker.review import review
from worker.staging import Stage


def _file(tmp_path: Path, name: str, header: list[str], rows: list[list[str]]) -> None:
    d = tmp_path / "inbox"
    d.mkdir(exist_ok=True)
    book = openpyxl.Workbook()
    ws = book.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    book.save(d / name)


def _ctx(tmp_path: Path) -> RunContext:
    out = tmp_path / "out"
    return RunContext(
        sponsor="sponsor-a",
        gp_id="GP-001",
        inbox_roots=[tmp_path / "inbox"],
        out_dir=out,
        audit=AuditLog(out / "audit.jsonl"),
        stage=Stage(out),
        registry=Registry(tmp_path / "state", "sponsor-a"),
    )


def _commitments(name: str = "commit.xlsx") -> dict[str, Any]:
    return call(
        "lift",
        path=name,
        sheet="Sheet",
        template="Commitments",
        columns={"Investor_ID": "A", "Commitment_Amount": "B", "Fund_ID": "C"},
        constants={"GP_ID": "GP-001"},
        header_row=1,
        first_row=2,
        why="copy",
    )


ROWS = [["I1", "100", "F1"], ["I2", "200", "F1"], ["I3", "300", "F1"]]
HEADER = ["Investor", "Amount", "Fund"]


def test_unknown_investor_blocks_and_lists_each_missing_item(tmp_path: Path) -> None:
    _file(tmp_path, "commit.xlsx", HEADER, ROWS)
    ctx = _ctx(tmp_path)
    ctx.registry.add("Investors", [{"key": "I1", "name": "Alpha"}], source="app batch", batch="b1")  # type: ignore[union-attr]
    script = [say("lift", _commitments()), say("write", call("write_upload", stage_id="s1", template="Commitments", why="deliver")), say("done")]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    result = start(agent, ctx, "build", "t1")
    assert [g["name"] for g in pending_gate(result)] == ["write_upload"]
    r = review(ctx, "s1")
    dep = [f for f in r["findings"] if f["code"] == "missing_dependency"]
    assert len(dep) == 1 and dep[0]["severity"] == "ERR" and dep[0]["record_type"] == "Investors"
    assert {m["value"] for m in dep[0]["missing"]} == {"I2", "I3"}
    with pytest.raises(GateRefused, match="not in the Investors"):
        decide(agent, ctx, "t1", {"type": "approve"}, meta={"reviewer": "reviewer-a"})


def test_quick_entry_unblocks_and_is_audited_with_its_source(tmp_path: Path) -> None:
    _file(tmp_path, "commit.xlsx", HEADER, ROWS)
    ctx = _ctx(tmp_path)
    assert ctx.registry is not None
    ctx.registry.add("Investors", [{"key": "I1", "name": "Alpha"}], source="app batch", batch="b1")
    entry = call(
        "add_existing_record",
        record_type="Investors",
        records=[{"key": "I2", "name": "Beta"}, {"key": "I3", "name": "Gamma"}],
        why="loaded into Ledgerline by the sponsor's team last year",
    )
    script = [
        say("lift", _commitments()),
        say("write", call("write_upload", stage_id="s1", template="Commitments", why="deliver")),
        say("quick entry", entry),
        say("write again", call("write_upload", stage_id="s1", template="Commitments", why="deliver")),
        say("done"),
    ]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    start(agent, ctx, "build", "t1")
    with pytest.raises(GateRefused):
        decide(agent, ctx, "t1", {"type": "approve"}, meta={"reviewer": "reviewer-a"})
    # the human rejects, then the agent asks for the quick entry, which is itself gated
    result = decide(agent, ctx, "t1", {"type": "reject", "message": "they exist; I2 and I3 were loaded outside"})
    assert [g["name"] for g in pending_gate(result)] == ["add_existing_record"]
    assert len(ctx.registry.entries("Investors")) == 1  # nothing before approval
    result = decide(agent, ctx, "t1", {"type": "approve"}, "confirmed by the sponsor", {"reviewer": "reviewer-a"})
    assert [g["name"] for g in pending_gate(result)] == ["write_upload"]
    assert review(ctx, "s1")["can_approve"]
    entries = ctx.registry.entries("Investors")
    assert {e["key"]: e["source"] for e in entries} == {"I1": "app batch", "I2": "quick entry", "I3": "quick entry"}
    added = [e for e in ctx.audit.read() if e["action"] == "existing_record.added"]
    assert {e["inputs"]["key"] for e in added} == {"I2", "I3"}
    assert all("loaded into Ledgerline" in e["rationale"] for e in added)
    approve(agent, ctx, "t1")
    assert (tmp_path / "out" / "Commitments_Upload.xlsx").exists()


def test_investors_in_the_same_batch_satisfy_the_dependency(tmp_path: Path) -> None:
    _file(tmp_path, "commit.xlsx", HEADER, ROWS)
    _file(tmp_path, "inv.xlsx", ["ID", "Name"], [["I1", "Alpha"], ["I2", "Beta"], ["I3", "Gamma"]])
    ctx = _ctx(tmp_path)
    investors = call(
        "lift",
        path="inv.xlsx",
        sheet="Sheet",
        template="Investors",
        columns={"Investor_ID": "A", "Investor_Name": "B"},
        constants={"GP_ID": "GP-001"},
        header_row=1,
        first_row=2,
        why="copy",
    )
    script = [say("inv", investors), say("lift", _commitments()), say("done")]
    start(build_agent(ctx, ScriptedModel(script=script), InMemorySaver()), ctx, "go", "t1")
    r = review(ctx, "s2")
    assert not [f for f in r["findings"] if f["code"] == "missing_dependency"]


def test_later_investor_batch_dedups_against_the_quick_entry(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert ctx.registry is not None
    ctx.registry.add("Investors", [{"key": "I2", "name": "Beta"}], source="quick entry", batch="job-1")
    _file(tmp_path, "inv.xlsx", ["ID", "Name"], [["I2", "Beta"], ["I9", "New Co"]])
    st = ctx.stage
    sid = st.lift(tmp_path / "inbox" / "inv.xlsx", "Sheet", {"Investor_ID": "A", "Investor_Name": "B"}, 1, 2, template="Investors")
    out = st.match_existing(sid, ctx.registry.entries("Investors"), ["Investor_ID"], "Investor_Name")
    assert [x["key"] for x in st.left_out(out)] == ["I2"]
    assert st.summary(out)["existing"]["matched_by_source"] == {"quick entry": 1}


def test_check_approval_is_quiet_when_nothing_is_referenced(tmp_path: Path) -> None:
    _file(tmp_path, "commit.xlsx", HEADER, [["", "100", "F1"]])
    ctx = _ctx(tmp_path)
    start(build_agent(ctx, ScriptedModel(script=[say("lift", _commitments()), say("done")]), InMemorySaver()), ctx, "go", "t1")
    r = review(ctx, "s1")
    assert not [f for f in r["findings"] if f["code"] == "missing_dependency"]
    with pytest.raises(GateRefused):  # the blank investor is a required-field WARN, not a dependency
        check_approval(ctx, [{"name": "write_upload", "args": {"stage_id": "s1"}}])
