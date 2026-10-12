"""Every static record type, end to end through the agent path: lift the sample source, check, gate, write, verify.

The scripted model drives the same tools the live model would; the source files are the sample world's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from scripted_model import ScriptedModel, approve, call, say
from worker import workbook as wb
from worker.agent import GateRefused, build_agent, pending_gate, start
from worker.audit import AuditLog
from worker.context import RunContext
from worker.sample import CFG, sample_file_name, write_static
from worker.staging import Stage
from worker.templates import TEMPLATES

# template field -> source column letter (the source header is SRCCOLS[type])
LIFTS: dict[str, dict[str, str]] = {
    "Investors": {"Investor_ID": "A", "Investor_Name": "B", "Investor_Type": "C", "Country": "E"},
    "Commitments": {"Investor_ID": "A", "Investor_Name": "B", "Fund_ID": "C", "Commitment_Amount": "D",
                    "Commitment_Date": "E"},
    "Vendors": {"Vendor_ID": "A", "Vendor_Name": "B", "Vendor_Type": "C"},
    "Investments": {"Investment_ID": "A", "Investment_Name": "B", "Position_Name": "C", "Investment_Status": "D",
                    "Expected_Cost": "E", "Expected_Unrealized": "F"},
    "Affiliates": {"Affiliate_ID": "A", "Affiliate_Name": "B", "Affiliate_Type": "C", "Legal_Entity": "D"},
}
KEY = {"Investors": "Investor_ID", "Commitments": "Investor_ID", "Vendors": "Vendor_ID",
       "Investments": "Investment_ID", "Affiliates": "Affiliate_ID"}


def _run(tmp_path: Path, template: str) -> tuple[RunContext, Any, Any]:
    inbox = tmp_path / "inbox"
    src = write_static(inbox / sample_file_name(template, "Sponsor A", "Fund A"), template)
    ctx = RunContext(sponsor="sponsor-a", gp_id="GP-001", inbox_roots=[inbox], out_dir=tmp_path / "out",
                     audit=AuditLog(tmp_path / "audit.jsonl"), stage=Stage(tmp_path / "out"))
    sheet = wb.list_sheets(src)["sheets"][0]["name"]
    script = [
        say("opening", call("list_sheets", path=src.name, why="see what is in the file")),
        say("lifting", call("lift", path=src.name, sheet=sheet, template=template, columns=LIFTS[template],
                            header_row=1, first_row=2, constants={"GP_ID": "GP-001"}, why="copy the columns")),
        say("coverage", call("coverage_report", stage_id="s1", why="check what is filled")),
        say("writing", call("write_upload", stage_id="s1", template=template, why="deliver the file")),
        say("done"),
    ]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    result = start(agent, ctx, f"build me the {template} sheet", "t1")
    return ctx, agent, result


# The sample files carry deliberate defects, and the gate is code: approval is refused while an ERR is open.
REFUSED = {
    "Investors": "longer than the 20 characters",
    "Commitments": "are not in the Investors the sponsor has",
    "Investments": "longer than the 20 characters",
    "Affiliates": "longer than the 30 characters",  # the Affiliates ID limit
}


@pytest.mark.parametrize("template", list(LIFTS))
def test_static_type_end_to_end(tmp_path: Path, template: str) -> None:
    ctx, agent, result = _run(tmp_path, template)
    assert [g["name"] for g in pending_gate(result)] == ["write_upload"]
    assert not list(ctx.out_dir.glob("*.xlsx"))  # nothing is written before the human decides
    target = ctx.out_dir / f"{template}_Upload.xlsx"
    if template in REFUSED:
        with pytest.raises(GateRefused, match=REFUSED[template]):
            approve(agent, ctx, "t1", "checked against the source")
        assert not target.exists()  # refused: nothing is written
        return
    approve(agent, ctx, "t1", "checked against the source")
    rows = list(openpyxl.load_workbook(target).active.iter_rows(values_only=True))
    header = list(rows[0])
    assert header == list(TEMPLATES[template].fields)
    body = rows[1:]
    assert len(body) == len(CFG[template]["rows"])
    key = header.index(KEY[template])
    assert sum(1 for r in body if not r[key]) == 3  # the three blank vendor ids stay blank, never invented
    assert all(r[header.index("GP_ID")] == "GP-001" for r in body)
    assert ctx.audit.verify()[0]
    actions = [e["action"] for e in ctx.audit.read()]
    for step in ("lift", "coverage_report", "gate.approve", "write_upload"):
        assert step in actions
