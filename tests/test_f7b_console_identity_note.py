"""F7b: the console page (the one people use) must say identity is self-declared, as the landing page does."""
from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "worker" / "static"


def test_console_says_identity_is_self_declared() -> None:
    text = (STATIC / "console.js").read_text(encoding="utf-8") + (STATIC / "console.html").read_text(encoding="utf-8")
    assert "Identity is self-declared; this is not production authentication." in text
