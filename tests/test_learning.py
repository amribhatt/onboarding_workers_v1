from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from langchain_core.messages import SystemMessage
from langgraph.checkpoint.memory import InMemorySaver

from scripted_model import ScriptedModel, approve, call, say
from worker import workbook as wb
from worker.agent import build_agent, decide, pending_gate, start
from worker.audit import AuditLog
from worker.context import RunContext
from worker.memory import SponsorMemory, replay
from worker.staging import Stage

SET7 = Path(__file__).parent / "fixtures" / "set7"
ROSTER = "Fund_Roster.xlsx"
RULE = "Inv ID means Investor_ID"


def _ctx(tmp_path: Path, inbox: Path, sponsor: str = "sponsor-a", tag: str = "") -> RunContext:
    out = tmp_path / f"out{tag}"
    return RunContext(
        sponsor=sponsor,
        gp_id="GP-001",
        inbox_roots=[inbox],
        out_dir=out,
        audit=AuditLog(tmp_path / f"audit{tag}.jsonl"),
        stage=Stage(out),
        memory=SponsorMemory(tmp_path / "memory", sponsor),
    )


def _lift(sheet: str, why: str = "copy fund ids") -> dict[str, Any]:
    return call(
        "lift",
        path=ROSTER,
        sheet=sheet,
        template="Commitments",
        columns={"GP_ID": "A", "Fund_ID": "B"},
        header_row=1,
        first_row=2,
        why=why,
    )


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    d = tmp_path / "inbox"
    d.mkdir()
    shutil.copy(SET7 / ROSTER, d / ROSTER)
    return d


def _sheet(inbox: Path) -> str:
    return wb.list_sheets(inbox / ROSTER)["sheets"][0]["name"]


def test_memory_lifecycle(tmp_path: Path) -> None:
    mem = SponsorMemory(tmp_path, "sponsor-a")
    rid = mem.propose(RULE, "header 'Inv ID' in Investors.xlsx")
    assert rid in mem.pending()
    assert not mem.agents_path.exists() and mem.rules_text() == ""
    mem.approve(rid)
    assert rid not in mem.pending()
    assert f"## Rule {rid}" in mem.agents_path.read_text(encoding="utf-8")
    assert RULE in mem.rules_text() and rid in mem.rules_text()

    bad = mem.propose("Amount is in thousands", "one sample")
    mem.reject(bad, "not true for this file")
    assert "thousands" not in mem.rules_text()
    assert "thousands" not in mem.agents_path.read_text(encoding="utf-8")
    assert bad not in mem.pending()
    with pytest.raises(KeyError):
        mem.approve(bad)

    assert mem.propose("another", "ev") not in {rid, bad}


def test_two_runs_rule_reaches_prompt_and_is_cited(tmp_path: Path, inbox: Path) -> None:
    sheet = _sheet(inbox)
    ctx1 = _ctx(tmp_path, inbox, tag="1")
    script1 = [
        say(
            "proposing",
            call("propose_rule", text=RULE, evidence="column header 'Inv ID'", why="human corrected me"),
        ),
        say("done"),
    ]
    model1 = ScriptedModel(script=script1)
    agent1 = build_agent(ctx1, model1, InMemorySaver())
    result = start(agent1, ctx1, "map the investors", "t1")
    gate = pending_gate(result)
    assert [g["name"] for g in gate] == ["propose_rule"]
    assert ctx1.memory is not None and ctx1.memory.rules_text() == ""  # nothing saved before approval
    approve(agent1, ctx1, "t1", "yes, that is right")
    assert RULE in ctx1.memory.rules_text()
    actions = [e["action"] for e in ctx1.audit.read()]
    assert "rule.approved" in actions and "replay" in actions

    ctx2 = _ctx(tmp_path, inbox, tag="2")
    model2 = ScriptedModel(
        script=[say("lifting", _lift(sheet, why="per R1, Inv ID is Investor_ID; copy ids")), say("done")]
    )
    agent2 = build_agent(ctx2, model2, InMemorySaver())
    start(agent2, ctx2, "map the investors", "t2")
    first = model2.seen[0]
    system = next(m for m in first if isinstance(m, SystemMessage))
    assert "Rules this sponsor has approved" in str(system.content)
    assert f"R1: {RULE}" in str(system.content)
    cited = [e for e in ctx2.audit.read() if e["action"] == "rule.cited"]
    assert len(cited) == 1 and cited[0]["inputs"]["rule_id"] == "R1"
    assert ctx2.audit.verify()[0]


def test_rejected_rule_is_not_saved(tmp_path: Path, inbox: Path) -> None:
    ctx = _ctx(tmp_path, inbox)
    model = ScriptedModel(
        script=[say("proposing", call("propose_rule", text=RULE, evidence="ev", why="because")), say("ok")]
    )
    agent = build_agent(ctx, model, InMemorySaver())
    start(agent, ctx, "go", "t1")
    decide(agent, ctx, "t1", {"type": "reject", "message": "not generally true"})
    assert ctx.memory is not None
    assert ctx.memory.rules_text() == "" and not ctx.memory.pending()
    assert any(e["action"] == "rule.rejected" for e in ctx.audit.read())


def test_replay_detects_changed_input(tmp_path: Path, inbox: Path) -> None:
    sheet = _sheet(inbox)
    ctx = _ctx(tmp_path, inbox)
    script = [
        say("lifting", _lift(sheet)),
        say("writing", call("write_upload", stage_id="s1", template="Commitments", why="deliver")),
        say("done"),
    ]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    result = start(agent, ctx, "build it", "t1")
    assert pending_gate(result)
    assert ctx.memory is not None and not ctx.memory.cases()  # no case before approval
    approve(agent, ctx, "t1")
    cases = ctx.memory.cases()
    assert len(cases) == 1
    case = cases[0]
    assert case["template"] == "Commitments" and case["mapping"]["sources"] == {"GP_ID": "A", "Fund_ID": "B"}
    assert len(case["input"]["sha256"]) == 64 and len(case["output"]["sha256"]) == 64

    assert [r["changed"] for r in replay(ctx)] == [False]

    path = inbox / ROSTER
    book = openpyxl.load_workbook(path)
    book[sheet]["B2"] = "ALTERED-FUND"
    book.save(path)
    after = replay(ctx)
    assert [r["changed"] for r in after] == [True]
    assert after[0]["reason"] == "input file changed"


def test_isolation_between_sponsors(tmp_path: Path, inbox: Path) -> None:
    SponsorMemory(tmp_path / "memory", "sponsor-a")
    a = SponsorMemory(tmp_path / "memory", "sponsor-a")
    a.approve(a.propose("sponsor-a secret convention", "ev"))
    ctx_b = _ctx(tmp_path, inbox, sponsor="sponsor-b")
    model = ScriptedModel(script=[say("hi")])
    agent = build_agent(ctx_b, model, InMemorySaver())
    start(agent, ctx_b, "go", "t1")
    system = next(m for m in model.seen[0] if isinstance(m, SystemMessage))
    assert "sponsor-a secret convention" not in str(system.content)
    assert "Rules this sponsor has approved" not in str(system.content)
    assert not (tmp_path / "memory" / "sponsor-b" / "AGENTS.md").exists()
    assert replay(ctx_b) == []
    with pytest.raises(ValueError):
        SponsorMemory(tmp_path, "../sponsor-a")
