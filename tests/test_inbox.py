from __future__ import annotations

import json
from pathlib import Path

import openpyxl
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from scripted_model import ScriptedModel, call, say
from worker import cli
from worker import workbook as wb
from worker.agent import build_agent, pending_gate, start
from worker.audit import AuditLog
from worker.context import RunContext
from worker.inbox import inbox_message, list_inbox
from worker.prompts import system_prompt
from worker.staging import Stage
from worker.tools import build_tools

SET7 = Path(__file__).parent / "fixtures" / "set7"
FOUR = {
    "Commitment_Register.xlsx",
    "Fund_Roster.xlsx",
    "GP_Ledger.xlsx",
    "Investor_Roster_side_by_side.xlsx",
}


def _ctx(tmp_path: Path) -> RunContext:
    out = tmp_path / "out"
    return RunContext(
        sponsor="sponsor-a",
        gp_id="GP-001",
        inbox_roots=[SET7],
        out_dir=out,
        audit=AuditLog(tmp_path / "audit.jsonl"),
        stage=Stage(out),
    )


def _tool(ctx: RunContext, name: str):
    return next(t for t in build_tools(ctx) if t.name == name)


def test_list_files_returns_workbooks_without_answer_keys(tmp_path: Path) -> None:
    out = json.loads(_tool(_ctx(tmp_path), "list_files").invoke({"why": "see the inbox"}))
    names = {f["name"] for f in out["files"]}
    assert names == FOUR
    assert all(f["kind"] == "workbook" and f["bytes"] > 0 for f in out["files"])
    assert {f["name"] for f in list_inbox([SET7])} == FOUR


def test_bare_name_resolves_and_unknown_name_lists_real_names(tmp_path: Path) -> None:
    tool = _tool(_ctx(tmp_path), "list_sheets")
    ok = tool.invoke({"path": "Fund_Roster.xlsx", "why": "open"})
    assert not ok.startswith(("ERROR", "REFUSED"))
    bad = tool.invoke({"path": "Funds.xlsx", "why": "open"})
    assert bad.startswith("ERROR") and "Fund_Roster.xlsx" in bad and "GP_Ledger.xlsx" in bad
    assert "answer" not in bad.lower()


def test_answer_key_is_still_refused_by_relative_name(tmp_path: Path) -> None:
    out = _tool(_ctx(tmp_path), "list_sheets").invoke({"path": "answer_key/nope.csv", "why": "peek"})
    assert out.startswith("REFUSED")


def test_prompt_mentions_list_files() -> None:
    assert "Use list_files to see them" in system_prompt()


def test_inbox_message_is_short_and_lists_names() -> None:
    text = inbox_message([SET7])
    assert text.startswith("Inbox: 4 files:") and len(text.splitlines()) < 60
    assert all(n in text for n in FOUR) and "answer" not in text.lower()


def test_run_first_message_contains_inbox_listing(tmp_path: Path) -> None:
    model = ScriptedModel(script=[say("hello")])
    out_path = tmp_path / "o.txt"
    with out_path.open("w", encoding="utf-8") as fh:
        code = cli.main(
            [
                "run",
                str(SET7 / "Fund_Roster.xlsx"),
                "--sponsor",
                "sponsor-a",
                "--gp-id",
                "GP-001",
                "--out",
                str(tmp_path / "out"),
                "--state-dir",
                str(tmp_path / "state"),
            ],
            input_fn=lambda _: "r",
            out=fh,
            model=model,
            saver=InMemorySaver(),
            environ={},
        )
    assert code == 0
    first = next(m for m in model.seen[0] if isinstance(m, HumanMessage))
    text = str(first.content)
    assert "Inbox: 4 files" in text
    assert all(n in text for n in FOUR)


def test_scripted_flow_list_files_bare_name_lift_coverage_paused(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    sheet = wb.list_sheets(SET7 / "Fund_Roster.xlsx")["sheets"][0]["name"]
    assert openpyxl.load_workbook(SET7 / "Fund_Roster.xlsx")  # fixture is readable
    script = [
        say("listing", call("list_files", why="see the inbox")),
        say("opening", call("list_sheets", path="Fund_Roster.xlsx", why="open by bare name")),
        say(
            "lifting",
            call(
                "lift",
                path="Fund_Roster.xlsx",
                sheet=sheet,
                template="Commitments",
                columns={"GP_ID": "A", "Fund_ID": "B"},
                header_row=1,
                first_row=2,
                why="copy fund ids",
            ),
        ),
        say("coverage", call("coverage_report", stage_id="s1", why="check")),
        say("writing", call("write_upload", stage_id="s1", template="Commitments", why="deliver")),
        say("done"),
    ]
    model = ScriptedModel(script=script)
    agent = build_agent(ctx, model, InMemorySaver())
    result = start(agent, ctx, "build me the Commitments sheet", "t1")
    assert [g["name"] for g in pending_gate(result)] == ["write_upload"]
    actions = [e["action"] for e in ctx.audit.read()]
    for step in ("list_files", "list_sheets", "lift", "coverage_report"):
        assert step in actions
    assert not any(a.endswith((".error", ".refused")) for a in actions)
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert any("Fund_Roster.xlsx" in str(m.content) for m in tool_msgs if m.name == "list_files")
