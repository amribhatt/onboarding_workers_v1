"""Deep Agents `interrupt_on` gates inside subagents (verified against deepagents 0.7.19, scripted model only).

Findings this file pins:
- a gate holds when the parent calls the gated tool;
- the default general-purpose subagent inherits `interrupt_on`, so the gate holds when a SUBAGENT calls the tool, and
  the repo's own `agent.pending_actions` sees it on the parent thread;
- a declarative subagent that supplies its own `interrupt_on` REPLACES the inherited one: an empty config runs the
  write with no human decision. Any subagent we ever declare must therefore keep the gated tools out of its tool list
  or carry the full gated set (see `test_a_subagent_that_overrides_interrupt_on_is_ungated_so_never_give_it_gated_tools`).
"""

from __future__ import annotations

from typing import Any

import pytest
from deepagents import create_deep_agent
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from scripted_model import ScriptedModel, call, say
from worker.agent import GATED_TOOLS, pending_actions

WRITES: list[str] = []


@tool
def write_upload(stage_id: str) -> str:
    """Write the upload (gated)."""
    WRITES.append(stage_id)
    return f"written {stage_id}"


def _run(script: list[Any], interrupt_on: dict[str, bool], subagents: list[dict[str, Any]] | None = None) -> tuple[Any, dict[str, Any]]:
    WRITES.clear()
    extra: dict[str, Any] = {"subagents": subagents} if subagents is not None else {}
    agent = create_deep_agent(
        model=ScriptedModel(script=script), tools=[write_upload], system_prompt="test",
        interrupt_on=interrupt_on, checkpointer=MemorySaver(), **extra,
    )
    cfg = {"configurable": {"thread_id": "t1"}, "recursion_limit": 60}
    agent.invoke({"messages": [("user", "write it")]}, cfg)
    return agent, cfg


def test_gate_holds_when_the_parent_calls_the_gated_tool() -> None:
    agent, cfg = _run([say("", call("write_upload", stage_id="s1")), say("done")], {"write_upload": True})
    assert WRITES == []
    assert [a["name"] for a in pending_actions(agent, "t1")] == ["write_upload"]
    agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}), cfg)
    assert WRITES == ["s1"]


def test_gate_holds_when_the_default_general_purpose_subagent_calls_the_tool() -> None:
    script = [
        say("", call("task", description="write stage s1", subagent_type="general-purpose")),
        say("", call("write_upload", stage_id="s1")),  # the subagent's own turn
        say("subagent done"),
        say("parent done"),
    ]
    agent, cfg = _run(script, {"write_upload": True})
    assert WRITES == []  # nothing written before a human decision
    assert [a["name"] for a in pending_actions(agent, "t1")] == ["write_upload"]  # the repo's gate API sees it
    agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}), cfg)
    assert WRITES == ["s1"]


def test_a_subagent_that_overrides_interrupt_on_is_ungated_so_never_give_it_gated_tools() -> None:
    """Documents the bypass: an explicit `interrupt_on` on a subagent REPLACES the inherited gate."""
    script = [
        say("", call("task", description="write stage s1", subagent_type="writer")),
        say("", call("write_upload", stage_id="s1")),
        say("subagent done"),
        say("parent done"),
    ]
    writer = {"name": "writer", "description": "writes", "system_prompt": "write", "tools": [write_upload], "interrupt_on": {}}
    agent, _cfg = _run(script, {"write_upload": True}, subagents=[writer])
    assert WRITES == ["s1"]  # written with NO approval: this is why gated tools stay out of declared subagents
    assert pending_actions(agent, "t1") == []


def test_the_repo_agent_declares_no_subagent_that_could_override_the_gates() -> None:
    """Fitness check: `create_deep_agent` in agent.py is called without `subagents=`; the only subagent is the
    default general-purpose one, which inherits GATED_TOOLS. If someone adds `subagents=`, this test must be revisited."""
    import inspect

    import worker.agent as agent_module

    source = inspect.getsource(agent_module)
    assert "subagents=" not in source, "a subagent spec was added: give it no gated tools or the full interrupt_on set"
    assert "write_upload" in GATED_TOOLS


@pytest.mark.parametrize("gated", ["write_upload", "approve_layout_program"])
def test_gated_tool_is_in_the_repo_gate_set(gated: str) -> None:
    assert gated in GATED_TOOLS


def test_the_repo_agent_has_no_task_tool_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """User decision (2026-10-11): the general-purpose subagent is off unless a genuine need appears."""
    from collections.abc import Sequence

    from worker.agent import build_agent
    from worker.audit import AuditLog
    from worker.context import RunContext
    from worker.staging import Stage

    class Spy(ScriptedModel):
        def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Spy:
            SEEN.append(sorted(t.name if hasattr(t, "name") else t.get("name", "?") for t in tools))
            return self

    SEEN: list[list[str]] = []

    def tool_names(env_value: str | None) -> list[str]:
        SEEN.clear()
        if env_value is None:
            monkeypatch.delenv("WORKER_ALLOW_GENERAL_SUBAGENT", raising=False)
        else:
            monkeypatch.setenv("WORKER_ALLOW_GENERAL_SUBAGENT", env_value)
        out = tmp_path / ("o" + str(len(SEEN)) + (env_value or "x"))
        ctx = RunContext(sponsor="sponsor-a", gp_id="GP-001", inbox_roots=[], out_dir=out, audit=AuditLog(out / "audit.jsonl"), stage=Stage(out))
        agent = build_agent(ctx, Spy(script=[say("ok")]), MemorySaver())
        agent.invoke({"messages": [("user", "hi")]}, {"configurable": {"thread_id": "g"}})
        return SEEN[-1]

    assert "task" not in tool_names(None)
