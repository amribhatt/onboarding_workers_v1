"""A scripted Interpreter for the job chat: each message is one reading the model would give (never a live model)."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage

from scripted_model import ScriptedModel


def reading(kind: str, summary: str, confidence: float = 0.9, **out: Any) -> AIMessage:
    body = {"kind": kind, "rule": None, "value": None, "reason": None, "where": None, **out}
    return AIMessage(json.dumps({"summary": summary, "confidence": confidence, "output": body}))


def interpreter(c: Any, *script: AIMessage, stronger: tuple[AIMessage, ...] = ()) -> ScriptedModel:
    """Make the console's job chat read sentences with this script (and, when given, escalate to `stronger`)."""
    c.chat_models = {"interpreter": ScriptedModel(script=list(script))}
    if stronger:
        c.chat_models["escalation"] = ScriptedModel(script=list(stronger))
    return c.chat_models["interpreter"]  # type: ignore[no-any-return]


def issue_reading(kind: str, summary: str, *decisions: dict[str, Any], confidence: float = 0.9, where: str | None = None) -> AIMessage:
    """One reading of a sentence about an open issue group: decisions are {option, rows, value, note}."""
    body = {"kind": kind, "where": where,
            "decisions": [{"option": "", "rows": [], "value": None, "note": None, **d} for d in decisions]}
    return AIMessage(json.dumps({"summary": summary, "confidence": confidence, "output": body}))
