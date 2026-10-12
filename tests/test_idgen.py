from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from scripted_model import ScriptedModel, approve, call, say
from worker.agent import build_agent, decide, pending_gate, start
from worker.audit import AuditLog
from worker.checks import stage_warnings
from worker.context import RunContext
from worker.idgen import IdScheme, check_scheme, format_id
from worker.staging import Stage

SCHEME = {"mode": "sequential", "prefix": "GINV-", "width": 0, "start": 1}


def _inbox(tmp_path: Path, name: str, ids: list[str]) -> Path:
    d = tmp_path / "inbox"
    d.mkdir(exist_ok=True)
    book = openpyxl.Workbook()
    ws = book.active
    ws.append(["Investor ID", "Name"])
    for i in ids:
        ws.append([i, f"Name {i}"])
    book.save(d / name)
    return d


def _ctx(tmp_path: Path, inbox: Path, tag: str) -> RunContext:
    out = tmp_path / f"out{tag}"
    return RunContext(
        sponsor="sponsor-a", gp_id="GP-001", inbox_roots=[inbox], out_dir=out,
        audit=AuditLog(tmp_path / f"audit{tag}.jsonl"), stage=Stage(out), id_root=tmp_path / "ids",
    )


def _run_fund(tmp_path: Path, name: str, ids: list[str], tag: str, do_approve: bool = True, **assign: Any) -> RunContext:
    inbox = _inbox(tmp_path, name, ids)
    ctx = _ctx(tmp_path, inbox, tag)
    script = [
        say("lift", call("lift", path=name, sheet="Sheet", template="Investors", columns={"GP_ID": "A", "Investor_ID": "A", "Investor_Name": "B"},
                         header_row=1, first_row=2, why="copy")),
        say("ids", call("assign_ids", stage_id="s1", id_field="Investor_ID", source_key_field="Source_Investor_Key",
                        why="new IDs from the counter", **assign)),
        say("write", call("write_upload", stage_id="s2", template="Investors", why="deliver")),
        say("done"),
    ]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    result = start(agent, ctx, "build investors", f"t{tag}")
    assert [g["name"] for g in pending_gate(result)] == ["write_upload"]
    if do_approve:
        approve(agent, ctx, f"t{tag}")
    else:
        decide(agent, ctx, f"t{tag}", {"type": "reject", "message": "no"})
    return ctx


def _set_scheme(tmp_path: Path) -> None:
    IdScheme(tmp_path / "ids", "sponsor-a", "Investors").change(SCHEME, by="reviewer-a", reason="setup", record=lambda *a: None)


def test_format_modes() -> None:
    assert format_id({"mode": "sequential", "prefix": "INV-", "width": 4}, 7) == "INV-0007"
    custom = {"mode": "custom", "prefix": "GP", "pattern": "{PREFIX}-{YEAR}-{SEQ:3}"}
    assert format_id(custom, 12, year=2026) == "GP-2026-012"
    with pytest.raises(ValueError):
        check_scheme({"mode": "custom", "pattern": "{PREFIX}"})
    with pytest.raises(ValueError):
        format_id({"mode": "source"}, 1)


def test_counter_continues_across_funds(tmp_path: Path) -> None:
    _set_scheme(tmp_path)
    c1 = _run_fund(tmp_path, "fund1.xlsx", ["A1", "A2", "A3"], "1")
    assert [r["Investor_ID"] for r in c1.stage.rows("s2")] == ["GINV-1", "GINV-2", "GINV-3"]
    assert [r["Source_Investor_Key"] for r in c1.stage.rows("s2")] == ["A1", "A2", "A3"]
    c2 = _run_fund(tmp_path, "fund2.xlsx", ["B1", "B2"], "2")
    assert [r["Investor_ID"] for r in c2.stage.rows("s2")] == ["GINV-4", "GINV-5"]
    assert IdScheme(tmp_path / "ids", "sponsor-a", "Investors").counter == 6
    assert any(e["action"] == "id.counter_committed" for e in c2.audit.read())


def test_rejected_run_reserves_nothing(tmp_path: Path) -> None:
    _set_scheme(tmp_path)
    _run_fund(tmp_path, "fund1.xlsx", ["A1", "A2"], "1", do_approve=False)
    assert IdScheme(tmp_path / "ids", "sponsor-a", "Investors").counter == 1
    c = _run_fund(tmp_path, "fund2.xlsx", ["B1"], "2")
    assert c.stage.rows("s2")[0]["Investor_ID"] == "GINV-1"


def test_existing_ids_are_skipped_never_reissued(tmp_path: Path) -> None:
    _set_scheme(tmp_path)
    c = _run_fund(tmp_path, "f.xlsx", ["A1", "A2"], "1", existing_ids=["GINV-1", "ginv-3"])
    assert [r["Investor_ID"] for r in c.stage.rows("s2")] == ["GINV-2", "GINV-4"]


def test_source_mode_refuses_to_generate(tmp_path: Path) -> None:
    inbox = _inbox(tmp_path, "f.xlsx", ["A1"])
    ctx = _ctx(tmp_path, inbox, "1")
    script = [
        say("lift", call("lift", path="f.xlsx", sheet="Sheet", template="Investors", columns={"Investor_ID": "A"},
                         header_row=1, first_row=2, why="copy")),
        say("ids", call("assign_ids", stage_id="s1", id_field="Investor_ID", why="ids")),
        say("done"),
    ]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    start(agent, ctx, "go", "t1")
    err = [e for e in ctx.audit.read() if e["action"] == "assign_ids.error"]
    assert err and "source" in err[0]["outputs"]["error"]
    # Live run finding: the refusal used to say "ask the human to set a sequential or custom scheme", and real
    # models then asked for a new scheme instead of keeping the source IDs. It now says no new IDs are needed.
    text = err[0]["outputs"]["error"]
    assert "no new IDs are needed" in text and "ask the human to set" not in text


def test_over_length_source_id_is_err_and_not_truncated() -> None:
    rows = [{"_src": "A2", "GP_ID": "GP-001", "Investor_ID": "X" * 21, "Investor_Name": "n"},
            {"_src": "A3", "GP_ID": "GP-001", "Investor_ID": "ok", "Investor_Name": "m"}]
    found = stage_warnings("Investors", rows, "GP-001")
    assert len(found) == 1 and found[0].startswith("ERR:") and "A2" in found[0]
    assert rows[0]["Investor_ID"] == "X" * 21


def test_scheme_change_needs_approval_and_is_audited(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _inbox(tmp_path, "f.xlsx", []), "1")
    script = [
        say("scheme", call("set_id_scheme", record_type="Investors", mode="sequential", prefix="GINV-", width=3,
                           start=1, reset_counter_to=60, why="sponsor standard")),
        say("done"),
    ]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    result = start(agent, ctx, "set ids", "t1")
    assert [g["name"] for g in pending_gate(result)] == ["set_id_scheme"]
    scheme = IdScheme(tmp_path / "ids", "sponsor-a", "Investors")
    assert scheme.scheme["mode"] == "source" and scheme.counter == 1  # nothing before approval
    approve(agent, ctx, "t1", "agreed with the sponsor")
    assert scheme.scheme["prefix"] == "GINV-" and scheme.counter == 60
    changed = [e for e in ctx.audit.read() if e["action"] == "id.scheme_changed"]
    assert changed[0]["inputs"]["old"] == {"scheme": {"mode": "source", "prefix": "", "width": 0, "start": 1, "pattern": ""}, "counter": 1}
    assert changed[0]["outputs"]["new"]["counter"] == 60


def test_same_old_key_gets_the_same_new_id(tmp_path: Path) -> None:
    src = _inbox(tmp_path, "dup.xlsx", ["B1", "A1", "B1"]) / "dup.xlsx"
    st = Stage(tmp_path / "out")
    sid = st.lift(src, "Sheet", {"Investor_ID": "A"}, 1, 2, template="Investors")
    ids = iter(f"GINV-{n}" for n in range(4, 20))
    out, crosswalk = st.assign_ids(sid, "Investor_ID", lambda: next(ids), "Source_Investor_Key")
    assert [r["Investor_ID"] for r in st.rows(out)] == ["GINV-4", "GINV-5", "GINV-4"]
    assert crosswalk == {"B1": "GINV-4", "A1": "GINV-5"}
