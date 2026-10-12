"""Helpers for reading an agent result: what it said, the latest coverage table, image attachments."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = [b.get("text", "") if isinstance(b, dict) else str(b) for b in content or []]
    return "\n".join(p for p in parts if p)


def last_coverage(result: Mapping[str, Any]) -> str:
    for msg in reversed(result.get("messages", [])):
        if isinstance(msg, ToolMessage) and msg.name == "coverage_report":
            return text_of(msg.content)
    return ""


def last_say(result: Mapping[str, Any]) -> str:
    for msg in reversed(result.get("messages", [])):
        if isinstance(msg, AIMessage):
            return text_of(msg.content).strip()
    return ""


def image_block(path: Path) -> dict[str, Any]:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{IMAGE_TYPES[path.suffix.lower()]};base64,{data}"},
    }
