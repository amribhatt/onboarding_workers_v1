"""Why a run stopped, and what happens next: one typed list and one function.

The loop itself is Deep Agents' (call the model, read the reply, run its tool calls, repeat). This module is the
harness's own reading of every way that loop ends. `handle` maps a `Stop` to the three things a person and the
record need: an audit record (written first), a plain message, and whether the thread can be continued.

* `end_turn`         the model answered without a tool call: the turn is done.
* `tool_use`         the model asked for tools: the loop continues; each tool call is its own audit record.
* `max_tokens`       a reply was cut off at the output limit: never used as if complete (one bounded retry, then a
                     human).
* `max_iterations_exceeded`  too many model calls in one request (`WORKER_BUDGET_ITERATIONS`, or the job's
                     `WORKER_BUDGET_CALLS`, or the graph's step backstop).
* `timeout_exceeded` the request ran past its wall clock (`WORKER_BUDGET_SECONDS`).
* `guardrail_intervened`  the provider's own filter or refusal ended a reply, or code refused an approval.
* `hook_stopped`     a harness hook (a model-call callback, or the audit record that must come first) halted the run.
* `token_budget_exceeded`  the job's token cap is used up.

Every stop except `end_turn` and `tool_use` leaves the checkpointed thread as it was and ends in a "needs a human"
message; the next human message continues from that state. Nothing here retypes a number: the message is fixed text.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphRecursionError

from worker.audit import AuditError, AuditLog
from worker.context import BudgetExceeded, RunContext
from worker.hashing import sha256_json


class Stop(StrEnum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    MAX_ITERATIONS = "max_iterations_exceeded"
    TIMEOUT = "timeout_exceeded"
    GUARDRAIL = "guardrail_intervened"
    HOOK_STOPPED = "hook_stopped"
    TOKEN_BUDGET = "token_budget_exceeded"


# stop -> (message, resumable, needs_human). The message is shown to the person as the agent's last word.
BEHAVIOUR: dict[Stop, tuple[str, bool, bool]] = {
    Stop.END_TURN: ("The agent finished its turn.", True, False),
    Stop.TOOL_USE: ("The agent asked for a tool; the loop continues.", True, False),
    Stop.MAX_TOKENS: (
        ("The model's reply was cut off at its output limit, again after one retry, so it was not used and "
         "nothing was changed by it. Everything done so far is kept. Ask for a smaller step, or raise the "
         "model's output limit, then send a message to continue."), True, True),
    Stop.MAX_ITERATIONS: (
        ("I stopped: this request used up its limit of model calls without finishing. Everything done so far "
         "is kept and recorded. Tell me how to proceed, or raise the limit (WORKER_BUDGET_ITERATIONS / "
         "WORKER_BUDGET_CALLS), then send a message to continue."), True, True),
    Stop.TIMEOUT: (
        ("I stopped: this request ran past its time limit. Everything done so far is kept and recorded. Send "
         "a message to continue, or raise the limit (WORKER_BUDGET_SECONDS)."), True, True),
    Stop.GUARDRAIL: (
        ("I stopped: a guardrail ended this step (the provider's filter refused the reply, or the code "
         "refused an action). Nothing was changed by it. Tell me how to proceed."), True, True),
    Stop.HOOK_STOPPED: (
        ("I stopped: a check that must run before every model call halted the run (for example the audit "
         "record could not be written). Nothing was changed after it. Fix the cause, then send a message to "
         "continue."), True, True),
    Stop.TOKEN_BUDGET: (
        ("I stopped: this job's token cap is used up. Everything done so far is kept. Raise "
         "WORKER_BUDGET_TOKENS (or set it to unlimited), then send a message to continue."), True, True),
}

TRUNCATED = ("length", "max_tokens", "max_output_tokens")  # finish_reason / stop_reason of a cut-off reply
FILTERED = ("content_filter", "refusal", "safety")
RETRY_NOTE = (
    "Your last reply was cut off at the output limit and was discarded. Reply again with one small step: a single "
    "tool call, or a short answer."
)


class RunStopped(Exception):
    """Raised inside the loop to end the run in a defined state; `run_turn`'s caller turns it into an Outcome."""

    def __init__(self, stop: Stop, detail: str = "", **facts: Any) -> None:
        super().__init__(detail or stop.value)
        self.stop, self.detail, self.facts = stop, detail, facts


@dataclass(frozen=True)
class Outcome:
    stop: Stop
    message: str
    resumable: bool
    needs_human: bool
    audited: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"stop": self.stop.value, "message": self.message, "resumable": self.resumable,
                "needs_human": self.needs_human, "audited": self.audited}


def handle(audit: AuditLog, stop: Stop, detail: str = "", **facts: Any) -> Outcome:
    """The one place a stop reason becomes behaviour: audit `run.stopped` first, then the message and the state."""
    message, resumable, needs_human = BEHAVIOUR[stop]
    audited = True
    try:
        audit.record("run.stopped", f"{stop.value}: {detail or message}"[:300],
                     {"stop": stop.value, **facts}, {"resumable": resumable, "needs_human": needs_human})
    except AuditError:
        audited = False  # the person is still told, and is told the record is missing
        message += " (The audit record of this stop could not be written.)"
    return Outcome(stop, message, resumable, needs_human, audited)


def from_exception(audit: AuditLog, exc: BaseException) -> Outcome | None:
    """The Outcome for an exception the loop ends with, or None for an error that is not a defined stop."""
    if isinstance(exc, RunStopped):
        return handle(audit, exc.stop, exc.detail, **exc.facts)
    if isinstance(exc, BudgetExceeded):  # `budget.exceeded` was audited when it was raised
        stop = Stop.TOKEN_BUDGET if "token cap" in str(exc) else Stop.MAX_ITERATIONS
        return handle(audit, stop, str(exc))
    if isinstance(exc, GraphRecursionError):
        return handle(audit, Stop.MAX_ITERATIONS, "the graph's step backstop was reached")
    if isinstance(exc, AuditError):  # the record that must come first could not be written
        return handle(audit, Stop.HOOK_STOPPED, f"audit unavailable: {exc}")
    return None


def reason_of(message: Any) -> str:
    """The provider's finish / stop reason on a reply ('' when it gives none)."""
    meta = getattr(message, "response_metadata", None) or {}
    return str(meta.get("finish_reason") or meta.get("stop_reason") or "").lower()


def classify(message: Any) -> Stop:
    """The stop reason a model reply stands for."""
    reason = reason_of(message)
    if reason in TRUNCATED:
        return Stop.MAX_TOKENS
    if reason in FILTERED or (getattr(message, "additional_kwargs", None) or {}).get("refusal"):
        return Stop.GUARDRAIL
    return Stop.TOOL_USE if getattr(message, "tool_calls", None) else Stop.END_TURN


class RunLimits(AgentMiddleware):
    """Per-request limits and the per-call trace span, around every model call and tool call of the loop.

    Before a model call: the wall clock and the iteration limit (`ctx.limits`). After it: a cut-off reply is retried
    once and then ends the run, a filtered reply ends the run, and one `model.call` record (the span) is written with
    the trace id, the seconds, the tokens, the reason and the hashes of what was sent.
    """

    def __init__(self, ctx: RunContext, clock: Callable[[], float] = time.monotonic) -> None:
        self.ctx, self.clock = ctx, clock

    def _on_time(self) -> None:
        cap = self.ctx.limits.max_seconds
        if cap is not None and self.clock() - self.ctx.turn_started >= cap:
            raise RunStopped(Stop.TIMEOUT, f"{cap:g} seconds used", seconds=cap)

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        """The span of one tool call (`tool.span`); the tool's own record stays as the tool wrote it."""
        self._on_time()
        started = self.clock()
        reply = handler(request)
        call = getattr(request, "tool_call", None) or {}
        content = getattr(reply, "content", "")
        failed = isinstance(content, str) and content.startswith(("ERROR", "REFUSED", "STOP"))
        self.ctx.audit.record(
            "tool.span", f"tool call {call.get('name', '?')}",
            {"trace_id": self.ctx.trace_id, "tool": call.get("name", ""), "call_id": call.get("id", "")},
            {"seconds": round(self.clock() - started, 4), "ok": not failed})
        return reply

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        ctx = self.ctx
        self._on_time()
        ctx.turn_iterations += 1
        cap = ctx.limits.max_iterations
        if cap is not None and ctx.turn_iterations > cap:
            raise RunStopped(Stop.MAX_ITERATIONS, f"{cap} model calls used", iterations=cap)
        started = self.clock()
        reply = handler(request)
        message = _ai(reply)
        retried = False
        if classify(message) is Stop.MAX_TOKENS:  # never complete: one bounded retry with a smaller ask
            ctx.audit.record("model.truncated", "reply cut off at the output limit; one retry",
                             {"trace_id": ctx.trace_id, "reason": reason_of(message)}, {"retry": True})
            retried = True
            reply = handler(request.override(messages=[*request.messages, HumanMessage(RETRY_NOTE)]))
            message = _ai(reply)
            if classify(message) is Stop.MAX_TOKENS:
                raise RunStopped(Stop.MAX_TOKENS, "cut off again after one retry", trace_id=ctx.trace_id)
        if classify(message) is Stop.GUARDRAIL:
            raise RunStopped(Stop.GUARDRAIL, f"provider ended the reply: {reason_of(message) or 'refusal'}",
                             trace_id=ctx.trace_id)
        usage = getattr(message, "usage_metadata", None) or {}
        ctx.audit.record(
            "model.call", f"model call {ctx.turn_iterations} of this request",
            {"trace_id": ctx.trace_id, "iteration": ctx.turn_iterations,
             "system_sha256": _sha(request.system_message.text if request.system_message else ""),
             "tools_sha256": _sha(",".join(sorted(getattr(t, "name", str(t)) for t in request.tools))),
             "history_messages": len(request.messages)},
            {"seconds": round(self.clock() - started, 4), "tokens_in": int(usage.get("input_tokens") or 0),
             "tokens_out": int(usage.get("output_tokens") or 0), "stop": classify(message).value,
             "tool_calls": [c.get("name", "") for c in getattr(message, "tool_calls", None) or []],
             "retried": retried, "model": answered_by(message)})
        return reply


def answered_by(message: Any) -> str:
    """The model the provider says answered ('' when it does not say). With OpenRouter fallbacks (`models` list) the
    configured model and the answering one differ; the span records which one it was."""
    meta = getattr(message, "response_metadata", None) or {}
    return str(meta.get("model_name") or meta.get("model") or "")[:120]


def _ai(reply: Any) -> AIMessage:
    result = getattr(reply, "result", None)
    return result[-1] if result else reply


def _sha(text: str) -> str:
    return sha256_json(text)
