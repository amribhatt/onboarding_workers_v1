"""F5: the clean path of every static type on the agent path writes the console's file (scripted model only).

The clean fixture of each type is the sample file with the rows that carry a planted defect left out, generated in
the test from `worker.sample`; the console approves the same file and the two output files are compared.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from langgraph.checkpoint.memory import InMemorySaver

import expected_rules as X
from id_decision import decide_ids
from scripted_model import ScriptedModel, approve, call, say
from worker import console_engine as CE
from worker import console_idgen as G
from worker import record_facts as RF
from worker import sample as S
from worker.agent import build_agent, decide, pending_gate, start
from worker.audit import AuditLog
from worker.console import Console
from worker.context import RunContext
from worker.registry import Registry
from worker.staging import Stage
from worker.templates import TEMPLATES

# the sample rows (by position) that carry no planted defect and are not already on file
CLEAN_ROWS = {
    "Investors": {1, 2, 10, 13, 14},
    "Commitments": {1, 2, 3, 5},
    "Investments": {1, 3, 4},  # row 2 names its parent as the Position: 23 characters, over the ID limit
    "Vendors": {4, 5, 8},
    "Affiliates": {1, 3, 6, 7},
}
# template field -> source column letter (the source header is SRCCOLS[type])
LIFTS: dict[str, dict[str, str]] = {
    "Investors": {"Investor_ID": "A", "Investor_Name": "B", "Investor_Type": "C", "Country": "E"},
    "Commitments": {"Investor_ID": "A", "Investor_Name": "B", "Fund_ID": "C", "Commitment_Amount": "D",
                    "Commitment_Date": "E"},
    "Vendors": {"Vendor_ID": "A", "Vendor_Name": "B"},
    "Investments": {"Investment_ID": "A", "Investment_Name": "B", "Position_ID": "C", "Investment_Status": "D"},
    "Affiliates": {"Affiliate_ID": "A", "Affiliate_Name": "B", "Affiliate_Type": "C"},
}
# fields the console fills from its own world (entity ids, the investor's Ledgerline ID is resolved the same way)
CONSOLE_ONLY = {"Commitments": {"Fund_ID", "Fund_Name"}}


@pytest.fixture
def clean_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    for typ, keep in CLEAN_ROWS.items():
        rows = [r for r in S.CFG[typ]["rows"] if r.i in keep]
        assert len(rows) == len(keep) and not any(set(r.fl) - {"miss:Country", "onFile"} for r in rows), typ
        monkeypatch.setitem(S.CFG[typ], "rows", rows)


def console_job(c: Console, typ: str) -> dict[str, Any]:
    c.act(f"start|Sponsor A|{typ}")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    if c.ex_pending(j):
        c.act("ex|no")
    c.act("go")
    if typ in RF.id_decision_types():  # approval waits for the sponsor's ID decision: here, the target's own format
        G.decide(c, {"sponsor": "Sponsor A", "record_type": typ, "reason": "the sponsor's standard numbering",
                     "scheme": dict(RF.target(typ).id_generation)}, "reviewer-a", j["id"])
    return j


def iso_dates(row: Any) -> list[Any]:
    """A row as the upload's reader shows it: a true Excel date cell (the writer's, since the user chose real dates)
    is its ISO date text, so a file reads the same as it did when the dates were written as text."""
    return [v.date().isoformat() if isinstance(v, dt.datetime) and v.time() == dt.time() else v for v in row]


def read_file(path: str | Path) -> tuple[list[str], list[dict[str, Any]]]:
    book = openpyxl.load_workbook(path, read_only=True)
    try:
        rows = [iso_dates(r) for r in book.worksheets[0].iter_rows(values_only=True)]
    finally:
        book.close()
    header = [str(h) for h in rows[0]]
    return header, [dict(zip(header, [*r, *([None] * (len(header) - len(r)))], strict=True)) for r in rows[1:]]


def scheme(typ: str) -> dict[str, Any]:
    """The sponsor's ID scheme for a type that always gets a new ID: the console's prefix and width."""
    return call("set_id_scheme", record_type=typ, mode="sequential", prefix=X.pre(typ),
                width=X.pad(typ), start=1, why="the sponsor's scheme, as the console issues IDs")


def agent_script(typ: str, name: str, sheet: str) -> list[Any]:
    lift = call("lift", path=name, sheet=sheet, template=typ, columns=LIFTS[typ], header_row=1, first_row=2,
                constants={"GP_ID": "sponsor-a"}, why="copy the columns")
    steps = [say("open", call("list_sheets", path=name, why="see what is in the file")), say("lift", lift)]
    last = "s1"
    if typ == "Investors":
        steps += [say("scheme", scheme(typ)),
                  say("ids", call("assign_ids", stage_id=last, id_field="Investor_ID",
                                  source_key_field="Source_Investor_Key", why="new IDs from the counter"))]
        last = "s2"
    if typ == "Commitments":
        steps += [say("refs", call("resolve_references", stage_id=last, field="Investor_ID", record_type="Investors",
                                   why="the investors are in the shared store")),
                  say("scheme", scheme(typ)),
                  say("ids", call("assign_ids", stage_id="s2", id_field="Commitment_ID", why="new IDs"))]
        last = "s3"
    steps += [say("write", call("write_upload", stage_id=last, template=typ, why="deliver the file")), say("done")]
    return steps


@pytest.mark.parametrize("typ", list(CLEAN_ROWS))
def test_F5_clean_file_of_each_static_type_equals_the_consoles(
    typ: str, clean_samples: None, tmp_path: Path
) -> None:
    c = Console(tmp_path / "inbox")
    c.act("prov|paid")
    if typ == "Commitments":  # its investors are approved first, in the shared store
        console_job(c, "Investors")
        c.act("acceptall")
        decide_ids(c)
        c.act("approve", who="reviewer-a")
    state = tmp_path / "agent_state"
    if (c.root / "registry").is_dir():  # what the sponsor has before this job is approved
        shutil.copytree(c.root / "registry", state / "registry")
    j = console_job(c, typ)
    if (c.root / "ids").is_dir():  # the sponsor's ID decision (a named person's, in the console) is the agent's too
        shutil.copytree(c.root / "ids", state / "ids", dirs_exist_ok=True)
    assert CE.tally(c, j)["err"] == 0, CE.tally(c, j)
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    want_header, want = read_file(j["out"]["path"])
    assert len(want) == len(CLEAN_ROWS[typ])

    out = tmp_path / "run" / "out"
    ctx = RunContext(sponsor="sponsor-a", gp_id="sponsor-a", inbox_roots=[c.inbox], out_dir=out,
                     audit=AuditLog(out / "audit.jsonl"), stage=Stage(out), registry=Registry(state, "sponsor-a"),
                     id_root=state / "ids")
    source = Path(j["up"]["path"])
    agent = build_agent(ctx, ScriptedModel(script=agent_script(typ, source.name, j["up"]["sheet"])), InMemorySaver())
    result = start(agent, ctx, f"build me the {typ} sheet", "t1")
    if typ in ("Investors", "Commitments"):  # the ID scheme is a human decision
        assert [g["name"] for g in pending_gate(result)] == ["set_id_scheme"]
        result = decide(agent, ctx, "t1", {"type": "approve"}, "the sponsor numbers these like the console", {"reviewer": "reviewer-a"})
    assert not [e["outputs"] for e in ctx.audit.read() if e["action"].endswith(".error")]
    assert [g["name"] for g in pending_gate(result)] == ["write_upload"]
    assert not list(out.glob("*_Upload*.xlsx"))  # nothing is written before the human decides
    approve(agent, ctx, "t1", "checked against the source")  # the gate passes: no ERR is open
    header, got = read_file(out / f"{typ}_Upload.xlsx")
    assert header == want_header == list(TEMPLATES[typ].fields)
    assert len(got) == len(want)
    skip = CONSOLE_ONLY.get(typ, set())
    for mine, theirs in zip(got, want, strict=True):
        assert {k: v for k, v in mine.items() if k not in skip} == {k: v for k, v in theirs.items() if k not in skip}
    assert ctx.audit.verify()[0]
    assert "write_upload" in [e["action"] for e in ctx.audit.read()]


def test_F5_commitments_resolve_their_investors_through_the_shared_store(clean_samples: None, tmp_path: Path) -> None:
    c = Console(tmp_path / "inbox")
    c.act("prov|paid")
    console_job(c, "Investors")
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    cm = console_job(c, "Commitments")
    ctx_out = tmp_path / "run" / "out"
    ctx = RunContext(sponsor="sponsor-a", gp_id="sponsor-a", inbox_roots=[c.inbox], out_dir=ctx_out,
                     audit=AuditLog(ctx_out / "audit.jsonl"), stage=Stage(ctx_out), registry=Registry(c.root, "sponsor-a"),
                     id_root=tmp_path / "ids")
    steps = agent_script("Commitments", Path(cm["up"]["path"]).name, cm["up"]["sheet"])[:3]
    start(build_agent(ctx, ScriptedModel(script=[*steps, say("done")]), InMemorySaver()), ctx, "go", "t1")
    rows = ctx.stage.rows("s2")
    ids = {e["source_key"]: e["key"] for e in Registry(c.root, "sponsor-a").entries("Investors")}
    assert [r["Investor_ID"] for r in rows] == [ids["HS-9901"], ids["HS-9902"], ids["HS-9922"], ids["HS-9960"]]
    assert all(r["Investor_ID"].startswith("GINV-") for r in rows)
    assert "resolve_references" in [e["action"] for e in ctx.audit.read()]


def test_F5_an_investor_the_store_does_not_have_stays_as_written_and_is_listed(
    clean_samples: None, tmp_path: Path
) -> None:
    c = Console(tmp_path / "inbox")
    c.act("prov|paid")
    cm = console_job(c, "Commitments")  # no investors on file yet
    out = tmp_path / "run" / "out"
    ctx = RunContext(sponsor="sponsor-a", gp_id="sponsor-a", inbox_roots=[c.inbox], out_dir=out,
                     audit=AuditLog(out / "audit.jsonl"), stage=Stage(out), registry=Registry(tmp_path / "s", "sponsor-a"),
                     id_root=tmp_path / "ids")
    steps = agent_script("Commitments", Path(cm["up"]["path"]).name, cm["up"]["sheet"])[:3]
    agent = build_agent(ctx, ScriptedModel(script=[*steps, say("done")]), InMemorySaver())
    start(agent, ctx, "go", "t1")
    assert [r["Investor_ID"] for r in ctx.stage.rows("s2")] == ["HS-9901", "HS-9902", "HS-9922", "HS-9960"]
