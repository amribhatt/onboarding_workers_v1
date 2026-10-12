"""F2: the console and the agent path read and write one store of existing records (`worker.registry`)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from id_decision import decide_ids
from scripted_model import ScriptedModel, approve, call, say
from worker import console_engine as CE
from worker import engine_console as EC
from worker.agent import build_agent, pending_gate, start
from worker.audit import AuditLog
from worker.console import Console
from worker.context import RunContext
from worker.registry import Registry
from worker.review import review
from worker.staging import Stage

INV_COLS = {"Investor_ID": "A", "Investor_Name": "B"}
CMT_COLS = {"Investor_ID": "A", "Investor_Name": "B", "Fund_ID": "C", "Commitment_Amount": "D",
            "Commitment_Date": "E"}


@pytest.fixture
def c(tmp_path: Path) -> Console:
    con = Console(tmp_path / "inbox")
    con.act("prov|paid")
    return con


def job(c: Console, typ: str, sponsor: str = "Sponsor A") -> dict[str, Any]:
    c.act(f"start|{sponsor}|{typ}")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    if c.ex_pending(j):
        c.act("ex|no")
    c.act("go")
    return j


def ctx_for(c: Console, tmp_path: Path) -> RunContext:
    out = tmp_path / "agent" / "out"
    return RunContext(sponsor="sponsor-a", gp_id="GP-001", inbox_roots=[c.inbox], out_dir=out,
                      audit=AuditLog(out / "audit.jsonl"), stage=Stage(out),
                      registry=Registry(c.root, "sponsor-a"))


def lift(path: Path, template: str, columns: dict[str, str], last_row: int | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"path": path.name, "sheet": EC.read_sheet(path)[0], "template": template,
                            "columns": columns, "constants": {"GP_ID": "GP-001"}, "header_row": 1, "first_row": 2,
                            "why": "copy"}
    if last_row:
        args["last_row"] = last_row
    return call("lift", **args)


def test_F2_approving_in_the_console_registers_the_records_with_provenance(c: Console) -> None:
    j = job(c, "Investors")
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    entries = Registry(c.root, "sponsor-a").entries("Investors")
    assert len(entries) == j["written"] == 14
    first = entries[0]
    assert (first["key"], first["source_key"], first["name"]) == ("GINV-0001", "HS-9901", "State Investment Council")
    assert (first["source"], first["batch"], first["job"], first["fund"]) == ("app batch", j["id"], j["id"], "Fund A")
    assert first["label"] == "the approved Sponsor A investors job"


def test_F2_the_agent_path_sees_investors_approved_in_the_console(c: Console, tmp_path: Path) -> None:
    job(c, "Investors")
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    cm = job(c, "Commitments")
    ctx = ctx_for(c, tmp_path)
    script = [say("lift", lift(Path(cm["up"]["path"]), "Commitments", CMT_COLS)), say("done")]
    start(build_agent(ctx, ScriptedModel(script=script), InMemorySaver()), ctx, "go", "t1")
    dep = [f for f in review(ctx, "s1")["findings"] if f["code"] == "missing_dependency"]
    # every investor the console approved resolves by its source key; only the one never loaded is missing
    assert len(dep) == 1 and {m["value"] for m in dep[0]["missing"]} == {"HS-9977"}


def test_F2_the_console_sees_investors_the_agent_path_approved(c: Console, tmp_path: Path) -> None:
    cm = job(c, "Commitments")  # the file is read; its investors are not on file yet
    before = CE.tally(c, cm)["err"]
    inv_path = c.inbox / "agent_investors.xlsx"
    book = openpyxl.Workbook()
    ws = book.active
    ws.append(["Row Labels", "Investor"])
    for key, name in [("HS-9901", "State Investment Council"), ("HS-9902", "Municipal Retirement Board")]:
        ws.append([key, name])
    book.save(inv_path)
    ctx = ctx_for(c, tmp_path)
    script = [say("lift", lift(inv_path, "Investors", INV_COLS)),
              say("write", call("write_upload", stage_id="s1", template="Investors", why="deliver")), say("done")]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    assert [g["name"] for g in pending_gate(start(agent, ctx, "go", "t1"))] == ["write_upload"]
    approve(agent, ctx, "t1")
    store = c.reg_of("Sponsor A")["Investors"]
    assert [(e["id"], e["name"]) for e in store] == [("HS-9901", "State Investment Council"),
                                                     ("HS-9902", "Municipal Retirement Board")]
    assert "app batch" in store[0]["src"]
    assert CE.tally(c, cm)["err"] < before  # the commitments of those two investors no longer miss them
    assert c.store_text("Investors", "Sponsor A").startswith("2 investors already in Ledgerline")


def test_F2_rerunning_an_approved_file_reports_exact_matches_not_new_records(c: Console, tmp_path: Path) -> None:
    first = job(c, "Investors")
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    again = job(c, "Investors")
    rows = CE.static_rows(c, again)  # type: ignore[arg-type]
    skipped = [r for r in rows if r["st"] == "skip"]
    assert len(skipped) == first["written"] == 14
    assert all(r["why"].startswith("Already in Ledgerline, from the approved Sponsor A investors job") for r in skipped)
    # the agent path agrees: the same file matches every written row by its source key
    ctx = ctx_for(c, tmp_path)
    script = [say("lift", lift(Path(again["up"]["path"]), "Investors", INV_COLS, last_row=15)), say("done")]
    start(build_agent(ctx, ScriptedModel(script=script), InMemorySaver()), ctx, "go", "t1")
    spec = ctx.stage.templates["Investors"]
    assert ctx.registry is not None
    sid = ctx.stage.match_existing("s1", ctx.registry.entries("Investors"), spec.key, spec.name_field)
    assert len(ctx.stage.left_out(sid)) == 14


def test_F2_an_export_loaded_in_the_console_is_in_the_registry_and_back(c: Console) -> None:
    job(c, "Investors")
    c.ext_load("Investors")
    reg = Registry(c.root, "sponsor-a")
    out = [e for e in reg.entries("Investors") if e["source"] == "outside export"]
    assert out and all(e["batch"].endswith(".xlsx") for e in out)
    reg.add("Vendors", [{"key": "V-777", "name": "Added On The Agent Path"}], source="outside export", batch="exp.xlsx")
    assert ("V-777", "Added On The Agent Path") in [(e["id"], e["name"]) for e in c.reg_of("Sponsor A")["Vendors"]]


def test_F2_an_old_console_state_file_loads_without_loss(tmp_path: Path) -> None:
    state = tmp_path / "state"
    first = Console(state / "inbox", state / "console.json")
    first.act("prov|paid")
    old = json.loads((state / "console.json").read_text(encoding="utf-8"))
    old["reg"] = {"Sponsor A": {"Investors": [
        {"id": "GINV-0001", "key": "HS-9901", "name": "State Investment Council",
         "src": "the approved Sponsor A investors job", "job": "j1"},
        {"id": "X-1", "key": "X-1", "name": "Loaded Outside", "src": "the Ledgerline export e.xlsx (2026-10-10)"},
        {"id": "Q-1", "key": "Q-1", "name": "Quick", "src": "a quick entry by you"}], "Vendors": []}}
    (state / "console.json").write_text(json.dumps(old), encoding="utf-8")
    shutil.rmtree(state / "registry")  # the old file is all there is
    again = Console(state / "inbox", state / "console.json")
    got = {e["id"]: e for e in again.reg_of("Sponsor A")["Investors"]}
    assert sorted(got) == ["GINV-0001", "Q-1", "X-1"]
    assert got["GINV-0001"]["key"] == "HS-9901" and got["GINV-0001"]["job"] == "j1"
    assert got["X-1"]["src"].startswith("the Ledgerline export")
    sources = {e["key"]: e["source"] for e in Registry(state, "sponsor-a").entries("Investors")}
    assert sources == {"GINV-0001": "app batch", "X-1": "outside export", "Q-1": "quick entry"}
    assert "reg" not in again.S
