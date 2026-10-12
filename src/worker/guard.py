"""Path guard: file tools may only touch allowed roots and never answer keys."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


class PathRefused(Exception):
    """Raised when a path may not be read by the agent's file tools."""


def check_path(path: str | Path, allowed_roots: Iterable[str | Path]) -> Path:
    """Resolve `path` and return it, or raise `PathRefused`.

    Refuses paths outside every allowed root, any path with a part named
    `answer_key`, and any file name ending `.answer.json`.
    """
    resolved = Path(path).resolve()
    roots = [Path(r).resolve() for r in allowed_roots]
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise PathRefused(f"path is outside the allowed roots: {resolved.name}")
    if any(part.lower() == "answer_key" for part in resolved.parts):
        raise PathRefused("answer keys are not readable by the agent")
    if resolved.name.lower().endswith(".answer.json"):
        raise PathRefused("answer keys are not readable by the agent")
    return resolved
