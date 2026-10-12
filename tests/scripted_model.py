"""A chat model that replays a fixed list of AIMessages; never calls a network."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import count
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field, PrivateAttr

_ids = count(1)


def call(name: str, **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args, "id": f"call_{next(_ids)}", "type": "tool_call"}


def say(text: str = "", *calls: dict[str, Any]) -> AIMessage:
    return AIMessage(content=text, tool_calls=list(calls))


class ScriptedModel(BaseChatModel):
    script: list[AIMessage] = Field(default_factory=list)
    seen: list[list[BaseMessage]] = Field(default_factory=list)
    _i: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> ScriptedModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self.seen.append(list(messages))
        if self._i >= len(self.script):
            raise AssertionError("scripted model ran out of messages")
        msg = self.script[self._i]
        self._i += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])


def approve(agent: Any, ctx: Any, thread_id: str, reason: str = "approved") -> dict[str, Any]:
    """A human approval that also acknowledges every open WARN (ERR still refuses), as a reviewer would."""
    from worker.agent import GateRefused, check_approval, decide, pending_actions

    ack: dict[str, str] = {}
    try:
        check_approval(ctx, pending_actions(agent, thread_id))
    except GateRefused as exc:
        ack = {fid: "acknowledged in test" for fid in exc.review["unacknowledged_warn"]}
    return decide(agent, ctx, thread_id, {"type": "approve"}, reason, {"reviewer": "reviewer-a"}, ack)
