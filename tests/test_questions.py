"""M15: the gate views carry typed Questions, one per cause, with a code-chosen recommendation."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import openpyxl

from scripted_model import ScriptedModel, call, say
from worker.contracts import SCOPES, Question
from worker.service import Run, RunManager


def _manager(state: Path, script: list[Any]) -> RunManager:
    return RunManager(state, lambda: ScriptedModel(script=list(script)))


def _run(m: RunManager, state: Path) -> Run:
    out = state / "runs" / "r1" / "out"
    out.mkdir(parents=True, exist_ok=True)
    ctx = m._context("sponsor-a", "GP-001", out)
    return Run("r1", "sponsor-a", "GP-001", "inv.xlsx", "t1", out, ctx=ctx)


def _supply(row: int, value: str = "Pension", field: str = "Investor_Type") -> dict[str, Any]:
    return {"name": "supply_value", "args": {"path": "inv.xlsx", "field": field, "value": value, "row": row}}


def _questions(run: Run) -> list[Question]:
    return [Question.from_dict(q) for q in run.questions]


def test_forty_rows_of_one_cause_yield_one_question(tmp_path: Path) -> None:
    m = _manager(tmp_path / "state", [])
    run = _run(m, tmp_path / "state")
    gates = m._gate_views(run, [_supply(r) for r in range(2, 42)])
    assert len(gates) == 40
    (q,) = _questions(run)
    assert {g["question_id"] for g in gates} == {q.question_id}
    assert 1 <= len(q.options) <= 4
    m.close()


def test_different_causes_get_their_own_question(tmp_path: Path) -> None:
    m = _manager(tmp_path / "state", [])
    run = _run(m, tmp_path / "state")
    m._gate_views(run, [_supply(2), _supply(3), _supply(2, "x", field="Investor_Name")])
    assert len(run.questions) == 2
    m.close()


def test_recommended_option_is_chosen_by_code_with_preview_counts(tmp_path: Path) -> None:
    m = _manager(tmp_path / "state", [])
    run = _run(m, tmp_path / "state")
    actions = [_supply(r) for r in range(2, 42)]
    actions.append({"name": "supply_value", "args": {"path": "inv.xlsx", "field": "Investor_Type",
                                                      "value": "Endowment", "row": 0}})
    m._gate_views(run, actions)
    (q,) = _questions(run)
    by_value = {o.payload["value"]: o for o in q.options if o.action == "supply"}
    assert by_value["Pension"].preview.conform == 40 and by_value["Pension"].preview.total == 40
    assert by_value["Endowment"].preview.complete  # covers every blank of the cause
    assert q.recommended in {o.option_id for o in q.options if o.preview.complete}
    declared = next(o for o in q.options if o.action == "declare_missing")
    assert declared.preview.conform == 0 and q.recommended != declared.option_id
    m.close()


def test_a_partial_option_is_never_recommended(tmp_path: Path) -> None:
    m = _manager(tmp_path / "state", [])
    run = _run(m, tmp_path / "state")
    m._gate_views(run, [_supply(2, "A"), _supply(3, "B")])
    (q,) = _questions(run)
    assert q.recommended is None
    assert all(o.preview.total == 2 and o.preview.conform <= 1 for o in q.options if o.action == "supply")
    m.close()


def test_organisation_scope_is_never_offered(tmp_path: Path) -> None:
    m = _manager(tmp_path / "state", [])
    run = _run(m, tmp_path / "state")
    m._gate_views(run, [_supply(2)])
    for raw in run.questions:
        assert tuple(raw["scopes"]) == SCOPES and "organisation" not in raw["scopes"]
    m.close()


def test_a_write_gate_has_no_question_and_is_unchanged(tmp_path: Path) -> None:
    m = _manager(tmp_path / "state", [])
    run = _run(m, tmp_path / "state")
    (g,) = m._gate_views(run, [{"name": "write_upload", "args": {"template": "Investors"}}])
    assert run.questions == [] and "question_id" not in g and g["name"] == "write_upload"
    m.close()


def test_an_old_run_json_without_questions_still_loads_and_renders(tmp_path: Path) -> None:
    state = tmp_path / "state"
    rdir = state / "runs" / "old1"
    rdir.mkdir(parents=True)
    old = {"id": "old1", "sponsor": "sponsor-a", "gp_id": "GP-001", "file": "inv.xlsx", "status": "idle",
           "created": 1.0, "messages": [], "gates": [], "coverage": "", "error": "", "thread_id": "t",
           "out_dir": str(rdir / "out")}
    (rdir / "run.json").write_text(json.dumps(old), encoding="utf-8")
    m = _manager(state, [])
    view = m.get("old1")
    assert view["questions"] == [] and view["gates"] == [] and view["status"] == "idle"
    m.close()


def test_the_answer_path_is_unchanged(tmp_path: Path) -> None:
    state = tmp_path / "state"
    (state / "inbox").mkdir(parents=True)
    book = openpyxl.Workbook()
    book.active.append(["ID", "Type"])
    book.active.append(["I1", None])
    book.save(state / "inbox" / "inv.xlsx")
    m = _manager(state, [say("supply", call("supply_value", path="inv.xlsx", field="Investor_Type",
                                              value="Pension", row=2, why="the person said so")),
                         say("recorded")])
    run_id = m.create_run("inv.xlsx", "sponsor-a", "GP-001")["id"]
    end = time.time() + 60
    while m.get(run_id)["status"] != "needs_approval" and time.time() < end:
        time.sleep(0.05)
    view = m.get(run_id)
    (gate,) = view["gates"]
    (q,) = view["questions"]
    assert gate["name"] == "supply_value" and gate["question_id"] == q["question_id"]
    m.decide(run_id, gate["hash"], "approve", "checked", reviewer="reviewer-a")
    while m.get(run_id)["status"] not in ("idle", "error") and time.time() < end:
        time.sleep(0.05)
    done = m.get(run_id)
    assert done["status"] == "idle" and done["gates"] == [] and done["questions"] == []
    audit = [json.loads(x) for x in (state / "runs" / run_id / "out" / "audit.jsonl").read_text().splitlines()]
    assert any(e["action"] == "gate.approve" and "reviewer-a" in e.get("rationale", "") for e in audit)
    m.close()
