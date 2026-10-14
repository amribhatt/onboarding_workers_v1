"""The five static record types (Investors, Commitments, Investments, Vendors, Affiliates) look and behave the same on
every screen; only the data differs. These tests pin what the consistency audit (docs/consistency-audit-static-types.md)
found: the job view has the same shape for every type, a pending ID decision is said the same way and blocks Approve for
every type, and `source_file` is the job's source before and after approval."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from worker.console import Console
from worker.console_screens import state_view

STATIC = ["Investors", "Commitments", "Investments", "Vendors", "Affiliates"]
WHO = "Priya Shah"


@pytest.fixture
def c(tmp_path: Path) -> Console:
    return Console(tmp_path / "inbox")


def converted(c: Console, type_: str) -> dict[str, Any]:
    c.act(f"start|Sponsor A|{type_}")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    c.act("ex|no")
    c.act("go")
    c.act("acceptall")
    j = c.job()
    assert j is not None
    return state_view(c)["job"]


def test_the_job_view_has_the_same_shape_for_every_static_type(c: Console) -> None:
    shapes = {t: sorted(converted(c, t)) for t in STATIC}
    first = shapes[STATIC[0]]
    assert all(s == first for s in shapes.values()), {t: set(s) ^ set(first) for t, s in shapes.items() if s != first}


def test_a_pending_id_decision_blocks_approval_the_same_way_for_every_type(c: Console) -> None:
    for t in STATIC:
        v = converted(c, t)
        assert v["id_pending"] is True and v["can_approve"] is False, t
        c.act("id_decide|sequential|number them in sequence", "", WHO)
        v = state_view(c)["job"]
        assert v["id_pending"] is False and v["can_approve"] is True, t
        c.act("approve", "", WHO)
        v = state_view(c)["job"]
        assert v["approved"] and isinstance(v["source_file"], str) and v["source_file"].endswith(".xlsx"), t
        assert not isinstance(v["file"], str) or v["file"] == v["source_file"]  # `file` may be the written upload; source_file never is


def test_every_static_type_shows_its_id_decision_as_one_row_of_the_plan(c: Console) -> None:
    for t in STATIC:
        plan = converted(c, t)["plan"]
        rows = [f["field"] for f in plan["fields"] if f["id_field"]] + ([plan["id_row"]] if plan["id_row"] else [])
        assert len(rows) == 1, (t, rows)  # a field of the plan, or (Commitments) the row the plan adds for it: never none, never two
        c.act("id_decide|sequential|number them in sequence", "", WHO)
        c.act("approve", "", WHO)


def test_the_screen_says_why_each_type_starts_where_it_starts_on_ids(c: Console) -> None:
    from worker import console_idgen as G

    said = {t: G.view(c, "Sponsor A", t)["rule"] for t in STATIC}
    for t in ("Investors", "Commitments"):
        assert "always get new IDs from your counter" in said[t] and "source key" in said[t]
    for t in ("Investments", "Vendors", "Affiliates"):
        assert "keep the ID the file gives" in said[t]


def test_a_record_file_does_not_name_the_gl_checker(c: Console) -> None:
    from worker import console_recon as CR

    j = None
    for t in STATIC[:1]:
        converted(c, t)
        j = c.job()
    assert j is not None and CR.model(c, j)["skill"] == ""
