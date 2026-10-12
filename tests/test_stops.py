"""Every way the loop ends has a defined behaviour: an audit record first, a plain message, and a thread that can
be continued. One test per stop reason, on the scripted model (never a live one)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from scripted_model import ScriptedModel, call, say
from worker import stops
from worker.agent import build_agent, start
from worker.audit import AuditError, AuditLog
from worker.context import Budget, BudgetExceeded, Limits, RunContext
from worker.staging import Stage
from worker.transcript import last_say

SET7 = Path(__file__).parent / "fixtures" / "set7"


def _ctx(tmp_path: Path, **kw: Any) -> RunContext:
    out = tmp_path / "out"
    return RunContext(sponsor="sponsor-a", gp_id="GP-001", inbox_roots=[SET7], out_dir=out,
                      audit=AuditLog(tmp_path / "audit.jsonl"), stage=Stage(out), **kw)


def _look(n: int) -> AIMessage:
    return say(f"look {n}", call("list_files", why=f"look {n}"))


def _cut(text: str = "partial", **extra: Any) -> AIMessage:
    return AIMessage(content=text, response_metadata={"finish_reason": "length"}, **extra)


def _actions(ctx: RunContext) -> list[str]:
    return [e["action"] for e in ctx.audit.read()]


def _stops(ctx: RunContext) -> list[dict[str, Any]]:
    return [e for e in ctx.audit.read() if e["action"] == "run.stopped"]


def test_every_stop_reason_has_a_defined_behaviour() -> None:
    assert set(stops.BEHAVIOUR) == set(stops.Stop)
    assert {s.value for s in stops.Stop} >= {"end_turn", "tool_use", "max_tokens", "max_iterations_exceeded",
                                             "timeout_exceeded", "guardrail_intervened", "hook_stopped"}
    for stop, (message, resumable, needs_human) in stops.BEHAVIOUR.items():
        assert message and resumable  # a stop is never a crash: the thread can always be continued
        assert needs_human == (stop not in (stops.Stop.END_TURN, stops.Stop.TOOL_USE))


def test_end_turn_and_tool_use_leave_one_trace_with_a_span_per_call(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    model = ScriptedModel(script=[_look(1), say("all done")])
    agent = build_agent(ctx, model, InMemorySaver())
    result = start(agent, ctx, "what is in the inbox?", "t1")
    assert "stop" not in result and last_say(result) == "all done"
    entries = ctx.audit.read()
    calls = [e for e in entries if e["action"] == "model.call"]
    assert [c["outputs"]["stop"] for c in calls] == ["tool_use", "end_turn"]
    assert calls[0]["outputs"]["tool_calls"] == ["list_files"]
    span = next(e for e in entries if e["action"] == "tool.span")
    trace = ctx.trace_id
    assert trace and {calls[0]["inputs"]["trace_id"], calls[1]["inputs"]["trace_id"],
                      span["inputs"]["trace_id"]} == {trace}  # one trace for the invocation
    assert span["inputs"]["tool"] == "list_files" and span["outputs"]["seconds"] >= 0 and span["outputs"]["ok"]
    assert "system_sha256" in calls[0]["inputs"]
    assert [s["inputs"]["stop"] for s in _stops(ctx)] == ["end_turn"]
    assert ctx.audit.verify()[0]


def test_a_cut_off_reply_is_retried_once_and_never_used_as_complete(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    model = ScriptedModel(script=[_cut(), say("short answer")])
    agent = build_agent(ctx, model, InMemorySaver())
    result = start(agent, ctx, "summarise", "t1")
    assert last_say(result) == "short answer" and "stop" not in result
    assert "model.truncated" in _actions(ctx)
    assert model.seen[1][-1].content == stops.RETRY_NOTE  # the retry carried the smaller ask
    assert not any(m.content == "partial" for m in result["messages"])


def test_max_tokens_twice_hands_to_a_human_and_the_thread_continues(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    model = ScriptedModel(script=[_look(1), _cut(), _cut(), say("back on track")])
    agent = build_agent(ctx, model, InMemorySaver())
    result = start(agent, ctx, "do it", "t1")
    assert result["stop"]["stop"] == "max_tokens" and result["stop"]["needs_human"]
    assert last_say(result) == result["stop"]["message"]
    assert not any(getattr(m, "content", "") == "partial" for m in result["messages"])  # not in the state
    assert [s["inputs"]["stop"] for s in _stops(ctx)] == ["max_tokens"]
    again = start(agent, ctx, "one small step please", "t1")  # resumed from the saved state
    assert last_say(again) == "back on track" and "stop" not in again
    assert any(isinstance(m, HumanMessage) and m.content == "do it" for m in model.seen[-1])


def test_max_iterations_stops_with_a_message_and_resumes(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, limits=Limits(max_iterations=2))
    model = ScriptedModel(script=[_look(1), _look(2), _look(3), say("finished")])
    agent = build_agent(ctx, model, InMemorySaver())
    result = start(agent, ctx, "keep looking", "t1")
    assert result["stop"]["stop"] == "max_iterations_exceeded" and len(model.seen) == 2
    assert "limit of model calls" in last_say(result)
    record = _stops(ctx)[0]
    assert record["outputs"] == {"resumable": True, "needs_human": True}
    ctx.limits = Limits()  # the human raises the limit and says go on
    again = start(agent, ctx, "go on", "t1")
    assert last_say(again) == "finished" or last_say(again).startswith("look")
    assert ctx.audit.verify()[0]


def test_the_job_call_cap_and_token_cap_are_defined_stops_too(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, budget=Budget(max_calls=1))
    model = ScriptedModel(script=[_look(1), say("never sent")])
    result = start(build_agent(ctx, model, InMemorySaver()), ctx, "go", "t1")
    assert result["stop"]["stop"] == "max_iterations_exceeded" and len(model.seen) == 1
    assert _actions(ctx).count("budget.exceeded") == 1 and _stops(ctx)[0]["inputs"]["stop"] == stops.Stop.MAX_ITERATIONS
    out = stops.from_exception(ctx.audit, BudgetExceeded("token cap"))
    assert out is not None and out.stop is stops.Stop.TOKEN_BUDGET


def test_timeout_is_checked_before_a_model_call_and_between_tool_calls(tmp_path: Path, monkeypatch: Any) -> None:
    ctx = _ctx(tmp_path, limits=Limits(max_seconds=0.0))
    model = ScriptedModel(script=[say("never sent")])
    result = start(build_agent(ctx, model, InMemorySaver()), ctx, "go", "t1")
    assert result["stop"]["stop"] == "timeout_exceeded" and model.seen == []

    ctx = _ctx(tmp_path / "b", limits=Limits(max_seconds=60.0))
    model = ScriptedModel(script=[_look(1), say("never sent")])
    real = stops.RunLimits
    monkeypatch.setattr(stops, "RunLimits",
                        lambda c: real(c, clock=lambda: time.monotonic() + 1000 * len(model.seen)))
    result = start(build_agent(ctx, model, InMemorySaver()), ctx, "go", "t2")
    assert result["stop"]["stop"] == "timeout_exceeded" and len(model.seen) == 1
    assert "list_files" not in _actions(ctx)  # the tool call after the deadline did not run
    assert _stops(ctx)[0]["inputs"]["stop"] == "timeout_exceeded"


def test_a_filtered_reply_is_a_guardrail_stop_and_is_not_used(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    model = ScriptedModel(script=[AIMessage("blocked text", response_metadata={"finish_reason": "content_filter"})])
    result = start(build_agent(ctx, model, InMemorySaver()), ctx, "go", "t1")
    assert result["stop"]["stop"] == "guardrail_intervened"
    assert not any(getattr(m, "content", "") == "blocked text" for m in result["messages"])
    assert _stops(ctx)[0]["inputs"]["stop"] == "guardrail_intervened"


def test_a_refused_approval_is_audited_as_a_guardrail_stop(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = stops.handle(ctx.audit, stops.Stop.GUARDRAIL, "1 ERR must be resolved", refused="approval")
    assert out.needs_human and out.resumable and out.audited
    assert _stops(ctx)[0]["inputs"] == {"stop": "guardrail_intervened", "refused": "approval"}


class _Halt(BaseCallbackHandler):
    raise_error = True
    run_inline = True

    def on_chat_model_start(self, *args: Any, **kwargs: Any) -> None:
        raise stops.RunStopped(stops.Stop.HOOK_STOPPED, "pre-call check refused", hook="test")


def test_a_hook_that_halts_the_run_is_a_defined_stop(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    model = ScriptedModel(script=[say("never sent")], callbacks=[_Halt()])
    result = start(build_agent(ctx, model, InMemorySaver()), ctx, "go", "t1")
    assert result["stop"]["stop"] == "hook_stopped" and model.seen == []
    assert _stops(ctx)[0]["inputs"] == {"stop": "hook_stopped", "hook": "test"}


def test_an_unwritable_span_stops_the_run_before_its_tool_call_runs(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    real = ctx.audit.record

    def flaky(action: str, *args: Any, **kwargs: Any) -> Any:
        if action == "model.call":
            raise AuditError("disk full")
        return real(action, *args, **kwargs)

    ctx.audit.record = flaky  # type: ignore[method-assign]
    model = ScriptedModel(script=[_look(1), say("never sent")])
    result = start(build_agent(ctx, model, InMemorySaver()), ctx, "go", "t1")
    assert result["stop"]["stop"] == "hook_stopped" and "list_files" not in _actions(ctx)
    ctx.audit.record = real  # type: ignore[method-assign]


def test_a_stop_whose_record_cannot_be_written_still_tells_the_person(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "a.jsonl")

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise AuditError("disk full")

    audit.record = broken  # type: ignore[method-assign]
    out = stops.handle(audit, stops.Stop.TIMEOUT, "x")
    assert not out.audited and "could not be written" in out.message and out.resumable


def test_an_error_that_is_not_a_defined_stop_is_not_swallowed(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert stops.from_exception(ctx.audit, KeyError("x")) is None
    assert _stops(ctx) == []
    with pytest.raises(AssertionError):  # the scripted model ran out: a real error still surfaces
        start(build_agent(ctx, ScriptedModel(script=[]), InMemorySaver()), ctx, "go", "t1")


def test_the_span_names_the_model_that_answered(tmp_path: Path) -> None:
    """Live run finding: with OpenRouter fallbacks the configured model and the answering one differ, and the
    `model.call` span did not say which answered, so fallback use could not be read from the record."""
    ctx = _ctx(tmp_path)
    fell_back = AIMessage(content="done", response_metadata={"model_name": "vendor/fallback-b:free"})
    start(build_agent(ctx, ScriptedModel(script=[_look(1), fell_back]), InMemorySaver()), ctx, "go", "t1")
    calls = [e for e in ctx.audit.read() if e["action"] == "model.call"]
    assert calls[-1]["outputs"]["model"] == "vendor/fallback-b:free"
    assert stops.answered_by(AIMessage("x")) == ""
