"""Keyed join of staged tables, the tool wrapper, replay, and the run guards."""

from __future__ import annotations

from pathlib import Path

import pytest

from worker import tools as tools_mod
from worker.agent import make_model
from worker.audit import AuditLog
from worker.context import RunContext
from worker.memory import SponsorMemory
from worker.staging import SRC, Stage

SET7 = Path(__file__).parent / "fixtures" / "set7"
ROSTER = SET7 / "Investor_Roster_side_by_side.xlsx"
SHEET = "Sheet"
LEFT = {"Investor_ID": "A", "Investor_Name": "B", "Investor_Type": "C"}
RIGHT = {"Investor_ID": "F", "Investor_Status": "G", "Country": "H"}


def _staged(tmp_path: Path) -> tuple[Stage, str, str]:
    st = Stage(tmp_path / "out")
    left = st.lift(ROSTER, SHEET, LEFT, 2, 3, template="Investors")
    right = st.lift(ROSTER, SHEET, RIGHT, 2, 3, template="Investors")
    return st, left, right


def test_join_brings_right_fields_by_key_not_by_position(tmp_path: Path) -> None:
    st, left, right = _staged(tmp_path)
    sid = st.join(left, right, "Investor_ID", "Investor_ID", ["Investor_Status", "Country"])
    rows = st.rows(sid)
    by_id = {r["Investor_ID"]: r for r in rows}
    src_right = {r["Investor_ID"]: r for r in st.rows(right)}
    assert len(rows) == len(st.rows(left))
    for iid, row in by_id.items():
        if iid in src_right:
            assert row["Country"] == src_right[iid]["Country"]
            assert row["Investor_Status"] == src_right[iid]["Investor_Status"]
            assert "+" in row[SRC]
        else:
            assert row.get("Country", "") == ""
    stats = st.summary(sid)["join"]
    assert stats["matched"] + stats["left_only"] == len(rows)
    assert stats["matched"] == len(set(by_id) & set(src_right))
    assert stats["conflicts"] == 0


def test_outer_join_keeps_unmatched_right_rows(tmp_path: Path) -> None:
    st, left, right = _staged(tmp_path)
    left_only_ids = {r["Investor_ID"] for r in st.rows(left)}
    right_ids = {r["Investor_ID"] for r in st.rows(right)}
    sid = st.join(left, right, "Investor_ID", "Investor_ID", ["Country"], how="outer")
    ids = {r["Investor_ID"] for r in st.rows(sid)}
    assert ids == left_only_ids | right_ids
    stats = st.summary(sid)["join"]
    assert stats["right_only"] == len(right_ids - left_only_ids)
    assert stats["right_only_kept"] is True


def test_join_refuses_repeated_right_key(tmp_path: Path) -> None:
    st, left, _ = _staged(tmp_path)
    with pytest.raises(ValueError, match="repeat in the right table"):
        st.join(left, left, "Investor_ID", "Investor_Type", ["Investor_Name"])


def test_join_rejects_unknown_field_and_how(tmp_path: Path) -> None:
    st, left, right = _staged(tmp_path)
    with pytest.raises(ValueError, match="not in template"):
        st.join(left, right, "Investor_ID", "Investor_ID", ["Nope"])
    with pytest.raises(ValueError, match="how must be"):
        st.join(left, right, "Investor_ID", "Investor_ID", ["Country"], how="inner")


def test_existing_left_value_is_not_overwritten_and_counts_conflict(tmp_path: Path) -> None:
    st, left, _ = _staged(tmp_path)
    other = st.lift(ROSTER, SHEET, {"Investor_ID": "A", "Investor_Name": "C"}, 2, 3, template="Investors")
    sid = st.join(left, other, "Investor_ID", "Investor_ID", ["Investor_Name"])
    assert st.rows(sid)[0]["Investor_Name"] == st.rows(left)[0]["Investor_Name"]
    assert st.summary(sid)["join"]["conflicts"] > 0


def _ctx(tmp_path: Path, **kw: object) -> RunContext:
    out = tmp_path / "out"
    return RunContext(
        sponsor="sponsor-a",
        gp_id="GP-001",
        inbox_roots=[SET7],
        out_dir=out,
        audit=AuditLog(tmp_path / "audit.jsonl"),
        stage=Stage(out),
        memory=SponsorMemory(tmp_path / "mem", "sponsor-a"),
        **kw,  # type: ignore[arg-type]
    )


def _tools(ctx: RunContext) -> dict[str, object]:
    return {t.name: t for t in tools_mod.build_tools(ctx)}


def test_join_tool_end_to_end_and_replayable(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    t = _tools(ctx)
    name = ROSTER.name
    t["lift"].invoke(
        {
            "path": name,
            "sheet": SHEET,
            "template": "Investors",
            "columns": LEFT,
            "header_row": 2,
            "first_row": 3,
            "why": "l",
        }
    )
    t["lift"].invoke(
        {
            "path": name,
            "sheet": SHEET,
            "template": "Investors",
            "columns": RIGHT,
            "header_row": 2,
            "first_row": 3,
            "why": "r",
        }
    )
    out = t["join_stages"].invoke(
        {
            "left_stage_id": "s1",
            "right_stage_id": "s2",
            "left_key": "Investor_ID",
            "right_key": "Investor_ID",
            "take": ["Investor_Status", "Country"],
            "why": "side-by-side blocks share the investor id",
        }
    )
    assert '"matched"' in out and "ERROR" not in out
    # the tool is called directly, with no gate in between: the person's approval is put on the record first
    ctx.audit.record("gate.approve", "approved in the test", {"decision": {"type": "approve"}, "reviewer": "reviewer-x"})
    written = t["write_upload"].invoke(
        {"stage_id": "s3", "template": "Investors", "why": "deliver", "drop_duplicates": False}
    )
    assert "Investors_Upload.xlsx" in written and "case_id" in written
    from worker.memory import replay

    results = replay(ctx)
    assert results and not any(r["changed"] for r in results), results


def test_repeat_failure_is_blocked_and_audited(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    t = _tools(ctx)
    args = {"path": "nope.xlsx", "why": "try"}
    for _ in range(3):
        assert t["list_sheets"].invoke(args).startswith("ERROR")
    out = t["list_sheets"].invoke(args)
    assert out.startswith("STOP")
    actions = [r["action"] for r in AuditLog(tmp_path / "audit.jsonl").read()]
    assert "list_sheets.blocked" in actions


def test_tool_call_budget_stops_further_calls(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, max_tool_calls=2)
    t = _tools(ctx)
    assert "count" in t["list_files"].invoke({"why": "one"})
    assert "count" in t["list_files"].invoke({"why": "two"})
    assert t["list_files"].invoke({"why": "three"}).startswith("STOP")


def test_make_model_fallbacks_and_timeouts() -> None:
    m = make_model(
        {
            "WORKER_MODEL": "a:free",
            "WORKER_MODEL_PROVIDER": "openrouter",
            "OPENROUTER_API_KEY": "k",
            "WORKER_MODEL_FALLBACKS": "b:free, c:free",
            "WORKER_TIMEOUT": "30",
        }
    )
    assert m.extra_body == {"models": ["a:free", "b:free", "c:free"]}  # type: ignore[attr-defined]
    assert m.request_timeout == 30  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="WORKER_TIMEOUT"):
        make_model({"WORKER_MODEL_PROVIDER": "openai", "OPENAI_API_KEY": "k", "WORKER_TIMEOUT": "x"})
