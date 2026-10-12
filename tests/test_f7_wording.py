"""F7 browser run: the Checker line said "1 lines need your call". One item reads as one item."""

from __future__ import annotations

from pathlib import Path

import pytest

from id_decision import decide_ids
from worker.console import Console
from worker.console_screens import state_view


@pytest.fixture
def c(tmp_path: Path) -> Console:
    return Console(tmp_path / "inbox")


def _checker_line(c: Console) -> str:
    chat = state_view(c)["job"]["chat"]
    return next(m["text"] for m in chat if m["text"].endswith("each with my suggestion."))


def test_one_line_needing_you_is_singular(c: Console) -> None:
    c.act("prov|paid")
    c.act("start|Sponsor B|GL Fund")
    c.act("ask_set|fund|B Fund I")
    c.act("ask_sample")
    c.act("ask_go")
    c.act("go")
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    c.act("start|Sponsor B|GL LP")
    c.act("ask_set|fund|B Fund I")
    c.act("ask_sample")
    c.act("ask_go")
    st = c.act("go")["job"]
    assert st["tally"]["err"] == 1
    text = _checker_line(c)
    assert text.startswith("1 line needs your call."), text


def test_several_lines_keep_the_plural(c: Console) -> None:
    c.act("prov|paid")
    c.act("start|Sponsor B|GL Fund")
    c.act("ask_set|fund|B Fund I")
    c.act("ask_sample")
    c.act("ask_go")
    c.act("go")
    assert _checker_line(c).startswith("11 lines need your call.")
