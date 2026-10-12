from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
import pytest
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from scripted_model import ScriptedModel, approve, call, say
from worker import workbook as wb
from worker.agent import build_agent, decide, make_model, pending_gate, start
from worker.audit import AuditLog
from worker.context import RunContext
from worker.staging import Stage
from worker.templates import TEMPLATES

SET7 = Path(__file__).parent / "fixtures" / "set7"
ROSTER = SET7 / "Fund_Roster.xlsx"


def _ctx(tmp_path: Path) -> RunContext:
    out = tmp_path / "out"
    return RunContext(
        sponsor="sponsor-a",
        gp_id="GP-001",
        inbox_roots=[SET7],
        out_dir=out,
        audit=AuditLog(tmp_path / "audit.jsonl"),
        stage=Stage(out),
    )


def _happy_script() -> list[Any]:
    sheet = wb.list_sheets(ROSTER)["sheets"][0]["name"]
    lift = call(
        "lift",
        path="Fund_Roster.xlsx",
        sheet=sheet,
        template="Commitments",
        columns={"GP_ID": "A", "Fund_ID": "B"},
        header_row=1,
        first_row=2,
        why="copy fund ids",
    )
    return [
        say("opening", call("list_sheets", path="Fund_Roster.xlsx", why="see what is in the file")),
        say("looking", call("look", path="Fund_Roster.xlsx", sheet=sheet, why="see the header")),
        say("lifting", lift),
        say("coverage", call("coverage_report", stage_id="s1", why="check what is filled")),
        say("writing", call("write_upload", stage_id="s1", template="Commitments", why="deliver the file")),
        say("done"),
    ]


def _run(tmp_path: Path, script: list[Any]):
    ctx = _ctx(tmp_path)
    model = ScriptedModel(script=script)
    agent = build_agent(ctx, model, InMemorySaver())
    result = start(agent, ctx, "build me the Commitments sheet", "t1")
    return ctx, agent, model, result


def test_pause_then_approve(tmp_path: Path) -> None:
    ctx, agent, _, result = _run(tmp_path, _happy_script())
    gate = pending_gate(result)
    assert [g["name"] for g in gate] == ["write_upload"]
    assert not list(ctx.out_dir.glob("*.xlsx"))
    result = approve(agent, ctx, "t1", "looks right")
    assert not pending_gate(result)
    target = ctx.out_dir / "Commitments_Upload.xlsx"
    assert target.exists()
    header = [c.value for c in next(openpyxl.load_workbook(target).active.iter_rows(max_row=1))]
    assert header == list(TEMPLATES["Commitments"].fields)
    ok, n = ctx.audit.verify()
    assert ok and n > 0
    entries = ctx.audit.read()
    actions = [e["action"] for e in entries]
    for step in ("list_sheets", "look", "lift", "coverage_report", "gate.approve", "write_upload"):
        assert step in actions
    whys = {e["action"]: e["rationale"] for e in entries}
    assert whys["lift"] == "copy fund ids"
    assert whys["write_upload"] == "deliver the file"


def test_reject_writes_nothing_and_audits_reason(tmp_path: Path) -> None:
    script = _happy_script()
    script[-1] = say("understood, not writing")
    ctx, agent, model, result = _run(tmp_path, script)
    assert pending_gate(result)
    decide(agent, ctx, "t1", {"type": "reject", "message": "Fund ids look wrong"})
    assert not list(ctx.out_dir.glob("*.xlsx"))
    rejected = [e for e in ctx.audit.read() if e["action"] == "gate.reject"]
    assert len(rejected) == 1 and rejected[0]["rationale"] == "Fund ids look wrong"
    assert ctx.audit.verify()[0]
    last = model.seen[-1]
    assert any(isinstance(m, ToolMessage) and "Fund ids look wrong" in str(m.content) for m in last)


def test_answer_key_refused_and_audited(tmp_path: Path) -> None:
    script = [
        say("peeking", call("list_sheets", path="answer_key/GL.csv", why="try the key")),
        say("ok, refused"),
    ]
    ctx, _, model, _ = _run(tmp_path, script)
    refused = [e for e in ctx.audit.read() if e["action"] == "list_sheets.refused"]
    assert len(refused) == 1
    tool_msgs = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert tool_msgs and "REFUSED" in str(tool_msgs[-1].content)
    assert ctx.audit.verify()[0]


def test_make_model_picks_provider() -> None:
    m = make_model({"WORKER_MODEL": "m1", "OPENAI_API_KEY": "k"})
    assert m.model_name == "m1" and not m.openai_api_base
    r = make_model({"WORKER_MODEL": "m2", "WORKER_MODEL_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "k2"})
    assert r.model_name == "m2" and r.openai_api_base == "https://openrouter.ai/api/v1"
    with pytest.raises(ValueError):
        make_model({"WORKER_MODEL_PROVIDER": "nope"})
