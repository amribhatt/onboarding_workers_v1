"""The README's generated blocks (providers, commands, tickets) match the code: the merge hook keeps them so."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_generated_blocks_are_current() -> None:
    got = subprocess.run([sys.executable, str(ROOT / "scripts" / "update_readme.py"), "--check"],
                         capture_output=True, text=True, check=False)
    assert got.returncode == 0, "README.md is stale: run `python scripts/update_readme.py`"


def test_every_provider_is_in_the_readme() -> None:
    from worker.agent import PROVIDERS

    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert all(f"`{name}`" in text for name in PROVIDERS)
