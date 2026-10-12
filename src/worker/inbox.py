"""The inbox as a person sees it: the files under the inbox roots, never answer keys."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

MAX_ENTRIES = 100
MAX_DEPTH = 3
MAX_MESSAGE_FILES = 50

_KINDS = {
    ".xlsx": "workbook",
    ".xlsm": "workbook",
    ".xls": "workbook",
    ".csv": "csv",
    ".json": "json",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
}


def list_inbox(roots: Iterable[str | Path], limit: int = MAX_ENTRIES) -> list[dict[str, Any]]:
    """Files at most MAX_DEPTH levels under each root: {name relative to the root, bytes, kind}."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in (Path(r) for r in roots):
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if len(found) >= limit:
                return found
            rel = p.relative_to(root)
            if not p.is_file() or len(rel.parts) > MAX_DEPTH:
                continue
            if any(part.lower() == "answer_key" for part in rel.parts):
                continue
            if p.name.lower().endswith(".answer.json"):
                continue
            name = rel.as_posix()
            if name in seen:
                continue
            seen.add(name)
            found.append(
                {"name": name, "bytes": p.stat().st_size, "kind": _KINDS.get(p.suffix.lower(), "other")}
            )
    return found


def _size(n: int) -> str:
    return f"{n} B" if n < 1000 else f"{n / 1000:.0f} kB" if n < 1_000_000 else f"{n / 1_000_000:.1f} MB"


def inbox_message(roots: Iterable[str | Path]) -> str:
    """Text for the first user message: `Inbox: <n> files:` then one `name (size)` line per file (<= 60 lines)."""
    files = list_inbox(roots)
    if not files:
        return "Inbox: no files."
    shown = files[:MAX_MESSAGE_FILES]
    lines = [f"Inbox: {len(files)} files:"]
    lines += [f"- {f['name']} ({_size(f['bytes'])})" for f in shown]
    if len(files) > len(shown):
        lines.append(f"- ... and {len(files) - len(shown)} more (use list_files)")
    return "\n".join(lines)
