"""F7b: the plan note said a chosen column applies to every sponsor job; only Required? persists (set_column writes
the job's own map, and a new job starts with an empty one). The note must not promise more than the code does."""
from __future__ import annotations

from pathlib import Path

from worker.console import Console

STATIC = Path(__file__).resolve().parents[1] / "src" / "worker" / "static"


def test_note_does_not_promise_that_a_column_choice_persists() -> None:
    assert "Required? and Comes from apply to every" not in (STATIC / "console.js").read_text(encoding="utf-8")


def test_a_column_choice_is_not_carried_to_the_next_job(tmp_path: Path) -> None:
    c = Console(tmp_path / "inbox")
    c.act("prov|paid")
    c.act("start|Sponsor B|Commitments")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    c.act("col|Currency|Unfunded", who="reviewer-a")
    assert j["map"] == {"Currency": "Unfunded"}
    c.act("start|Sponsor B|Commitments")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    j2 = c.job()
    assert j2 is not None and j2["id"] != j["id"]
    assert j2["map"] == {}
