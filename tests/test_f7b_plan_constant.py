"""F7b: the plan table showed the demo constant "sponsor-a" as GP_ID for every sponsor; the file carried the right one."""
from __future__ import annotations

from pathlib import Path

from worker import console_screens as S
from worker.console import Console


def test_plan_shows_the_sponsors_own_gp_id_constant(tmp_path: Path) -> None:
    c = Console(tmp_path / "inbox")
    c.act("prov|paid")
    c.act("start|Sponsor C|Investors")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    if c.ex_pending(j):
        c.act("ex|no")
    field = next(f for f in S.plan_view(c, j)["fields"] if f["field"] == "GP_ID")
    assert field["constant"] == "sponsor-c"
