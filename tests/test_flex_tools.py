"""Ticket 11, the agent's side of the reshape flow: `propose_layout_program` changes nothing, `approve_layout_program`
sits behind `interrupt_on` like `write_upload`, so the agent can never approve its own program."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

import flex_local as L
import worker.flex.runner as FR
import worker.models as M
from port_fakes import FakeRunner
from scripted_model import ScriptedModel, call, say
from worker import agent as A
from worker.agent import build_agent, decide, pending_actions, pending_gate, start
from worker.audit import AuditLog
from worker.context import RunContext
from worker.contracts import GATED_TOOLS as CONTRACT_GATED
from worker.staging import Stage

IMAGE = "sha256:" + "8" * 64


def answer(program: str) -> Any:
    return say(json.dumps({"summary": "Unpivots the statuses.", "confidence": 0.8, "output": {
        "program": program, "case": "nested_pivot", "steps": ["Read each investor row."]}}))


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[RunContext, Any, ScriptedModel, ScriptedModel]:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy(L.PIVOT, inbox / "investors_pivot.xlsx")
    drafter = ScriptedModel(script=[answer(L.PIVOT_PROGRAM)])
    runner = FakeRunner(L.local_layout, image=IMAGE)
    monkeypatch.setattr(FR, "runner_from_env", lambda blobs=None, env=None: runner)
    monkeypatch.setattr(M, "model_for_role", lambda role, env=None: drafter)
    out = tmp_path / "out"
    ctx = RunContext(sponsor="sponsor-a", gp_id="GP-001", inbox_roots=[inbox], out_dir=out,
                     audit=AuditLog(tmp_path / "audit.jsonl"), stage=Stage(out),
                     accounts_root=tmp_path / "state" / "accounts")
    coworker = ScriptedModel(script=[])
    return ctx, build_agent(ctx, coworker, InMemorySaver()), coworker, drafter


def proposal_id(result: dict[str, Any]) -> str:
    msg = next(m for m in result["messages"] if isinstance(m, ToolMessage) and m.name == "propose_layout_program")
    return json.loads(str(msg.content))["proposal_id"]


def test_the_two_tools_are_registered_and_the_approval_is_gated() -> None:
    assert "approve_layout_program" in A.GATED_TOOLS and "approve_layout_program" in CONTRACT_GATED
    assert "propose_layout_program" not in A.GATED_TOOLS  # it changes nothing


def test_a_proposal_changes_nothing_and_an_approval_pauses_for_a_person(
        rig: tuple[RunContext, Any, ScriptedModel, ScriptedModel]) -> None:
    ctx, agent, coworker, _drafter = rig
    coworker.script.append(say("", call("propose_layout_program", path="investors_pivot.xlsx", sheet="Sheet",
                                        template="Investors", why="the reader cannot place the pivot")))
    coworker.script.append(say("here is what I propose"))
    first = start(agent, ctx, "build the investors sheet", "t1")
    pid = proposal_id(first)
    assert pid and not pending_gate(first) and ctx.stage.stage_ids() == []
    assert not (ctx.out_dir / "derived").exists()  # nothing was written
    assert "flex.artifact.approved" not in [e["action"] for e in ctx.audit.read()]

    bound = json.loads(next(m for m in first["messages"] if isinstance(m, ToolMessage)).content)["bound"]
    coworker.script.append(say("", call("approve_layout_program", proposal_id=pid, bound=bound,
                                        acknowledged=["subtotal_rounded"], scope="job", why="I approve my own")))
    coworker.script.append(say("done"))
    second = start(agent, ctx, "go ahead", "t1")
    assert [g["name"] for g in pending_gate(second)] == ["approve_layout_program"]  # paused: the agent cannot decide
    assert [a["name"] for a in pending_actions(agent, "t1")] == ["approve_layout_program"]
    assert "flex.artifact.approved" not in [e["action"] for e in ctx.audit.read()]
    assert not (ctx.out_dir / "derived").exists()

    done = decide(agent, ctx, "t1", {"type": "approve"}, "I checked the preview", meta={"reviewer": "Priya Rao"})
    assert not pending_gate(done)
    names = [e["action"] for e in ctx.audit.read()]
    assert names.index("gate.approve") < names.index("flex.artifact.approved")
    approved = next(e for e in ctx.audit.read() if e["action"] == "flex.artifact.approved")
    assert "Priya Rao" in json.dumps(approved) and "layout_drafter" not in approved["inputs"].get("by", "")
    files = list((ctx.out_dir / "derived").glob("reshaped_*.csv"))
    assert len(files) == 1 and len(files[0].read_text("utf-8").splitlines()) == 44  # header and 43 investors
    assert ctx.audit.verify()[0]


def test_a_rejected_approval_writes_nothing(rig: tuple[RunContext, Any, ScriptedModel, ScriptedModel]) -> None:
    ctx, agent, coworker, _ = rig
    coworker.script.append(say("", call("propose_layout_program", path="investors_pivot.xlsx", sheet="Sheet",
                                        template="Investors", why="pivot")))
    coworker.script.append(say("proposed"))
    first = start(agent, ctx, "build it", "t2")
    bound = json.loads(next(m for m in first["messages"] if isinstance(m, ToolMessage)).content)["bound"]
    coworker.script.append(say("", call("approve_layout_program", proposal_id=proposal_id(first), bound=bound,
                                        acknowledged=["subtotal_rounded"], scope="job", why="approve")))
    coworker.script.append(say("understood"))
    start(agent, ctx, "approve", "t2")
    decide(agent, ctx, "t2", {"type": "reject", "message": "the totals look wrong"})
    assert "flex.artifact.approved" not in [e["action"] for e in ctx.audit.read()]
    assert not (ctx.out_dir / "derived").exists()


def test_calling_the_approval_without_a_gate_decision_is_refused(
        rig: tuple[RunContext, Any, ScriptedModel, ScriptedModel]) -> None:
    from worker.tools import build_tools

    ctx, _agent, _c, _drafter = rig
    tools = {t.name: t for t in build_tools(ctx)}
    out = tools["propose_layout_program"].invoke({"path": "investors_pivot.xlsx", "sheet": "Sheet",
                                                  "template": "Investors", "why": "pivot"})
    p = json.loads(out)
    got = tools["approve_layout_program"].invoke({"proposal_id": p["proposal_id"], "bound": p["bound"],
                                                  "acknowledged": ["subtotal_rounded"], "scope": "job", "why": "x"})
    assert got.startswith("ERROR") and "approval" in got
    assert not (ctx.out_dir / "derived").exists()
