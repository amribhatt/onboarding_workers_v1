"""Per-job spend cap: the call after the cap is refused, and `budget.exceeded` is audited before the refusal."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from scripted_model import ScriptedModel, call, say
from worker.agent import build_agent, start
from worker.audit import AuditError, AuditLog
from worker.context import Budget, BudgetExceeded, RunContext
from worker.models import budgeted
from worker.staging import Stage

SET7 = Path(__file__).parent / "fixtures" / "set7"


def _ctx(tmp_path: Path, budget: Budget | None = None) -> RunContext:
    out = tmp_path / "out"
    ctx = RunContext(
        sponsor="sponsor-a",
        gp_id="GP-001",
        inbox_roots=[SET7],
        out_dir=out,
        audit=AuditLog(tmp_path / "audit.jsonl"),
        stage=Stage(out),
    )
    if budget is not None:
        ctx.budget = budget
    return ctx


def _used(n: int) -> dict[str, int]:
    return {"input_tokens": n - 10, "output_tokens": 10, "total_tokens": n}


def test_budget_refuses_after_cap_and_audits_first(tmp_path: Path) -> None:
    # Deep Agent: a call cap of 2; the third model call is refused before it reaches the model.
    ctx = _ctx(tmp_path, Budget(max_calls=2))
    script = [
        say("files", call("list_files", why="see the inbox")),
        say("files again", call("list_files", why="check again")),
        say("never sent"),
    ]
    model = ScriptedModel(script=script)
    agent = build_agent(ctx, model, InMemorySaver())
    result = start(agent, ctx, "build me the Commitments sheet", "t1")  # a defined stop, not a crash (worker.stops)
    assert result["stop"]["stop"] == "max_iterations_exceeded"
    assert len(model.seen) == 2  # the refused call never reached the model
    entries = ctx.audit.read()
    assert [e["action"] for e in entries][-2:] == ["budget.exceeded", "run.stopped"]
    assert entries[-2]["outputs"]["calls"] == 2 and entries[-2]["outputs"]["max_calls"] == 2
    assert [e["action"] for e in entries].count("list_files") == 2
    assert ctx.audit.verify()[0]

    # every other role counts through the same wrapper: a token cap of 150 refuses the third call.
    budget = Budget(max_tokens=150)
    audit = AuditLog(tmp_path / "resolve_audit.jsonl")
    replies = [AIMessage('{"a": "x"}', usage_metadata=_used(100)) for _ in range(3)]
    inner = ScriptedModel(script=replies)
    capped = budgeted(inner, budget, audit, "exec")
    with pytest.raises(BudgetExceeded, match="token cap"):
        for _ in range(3):
            capped.invoke("Decide each key.")
    assert budget.tokens == 200 and budget.calls == 2 and len(inner.seen) == 2
    assert [e["action"] for e in audit.read()] == ["budget.exceeded"]

    # Audit before effect: when the refusal cannot be audited, the call still does not happen.
    broken = AuditLog(tmp_path / "broken.jsonl")

    def no_audit(*args: object, **kwargs: object) -> dict:
        raise AuditError("disk full")

    broken.record = no_audit  # type: ignore[method-assign]
    spent = Budget(max_calls=0)
    probe = ScriptedModel(script=[AIMessage("hi")])
    with pytest.raises(AuditError):
        budgeted(probe, spent, broken).invoke([HumanMessage("hello")])
    assert probe.seen == [] and spent.calls == 0


def test_budget_unset_is_unlimited(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert not ctx.budget.is_set
    model = ScriptedModel(script=[])
    assert budgeted(model, ctx.budget, ctx.audit) is model  # no wrapper at all: today's behaviour
    assert budgeted(model, None, ctx.audit) is model

    script = [say(f"step {i}", call("list_files", why=f"look {i}")) for i in range(6)] + [say("done")]
    model = ScriptedModel(script=script)
    agent = build_agent(ctx, model, InMemorySaver())
    start(agent, ctx, "build me the Commitments sheet", "t1")
    assert len(model.seen) == 7
    assert not any(e["action"] == "budget.exceeded" for e in ctx.audit.read())

    # a Budget built by code has no cap; one read from the environment has the default unless it is lifted by name
    default = Budget.from_env({})
    assert (default.max_tokens, default.max_calls) == (Budget.DEFAULT_TOKENS, Budget.DEFAULT_CALLS)
    assert Budget.from_env({"WORKER_BUDGET_TOKENS": "", "WORKER_BUDGET_CALLS": " "}) == default
    assert Budget.from_env({"WORKER_BUDGET_TOKENS": "unlimited", "WORKER_BUDGET_CALLS": "Unlimited"}) == Budget()
    capped = Budget.from_env({"WORKER_BUDGET_TOKENS": "5000", "WORKER_BUDGET_CALLS": "30"})
    assert (capped.max_tokens, capped.max_calls) == (5000, 30)
    with pytest.raises(ValueError, match="WORKER_BUDGET_CALLS"):
        Budget.from_env({"WORKER_BUDGET_CALLS": "lots"})
