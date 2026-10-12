# ruff: noqa: F401, F811
"""M24c: a person can undo an answer to an unknown column or format question.

The undo reverts the sponsor's recipe step through FieldRecipes (`recipe.reverted` audited first), appends a feedback
`undo` record (the decision is never deleted), evaluates the job again, and the question is asked again. Same
reviewer rules as an answer. Fictitious names only; no model is called.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from id_decision import decide_ids
from test_unknown_formats import (
    ANALYST,
    HEAD,
    ROWS,
    answer,
    book,
    c,
    own_job,
    questions,
    recipe_path,
    recommended,
)
from worker import feedback as F
from worker.audit import AuditLog
from worker.console import Console, ConsoleError
from worker.console_screens import state_view
from worker.web import create_app


def actions(c: Console, j: dict[str, Any]) -> list[str]:
    return [e["action"] for e in AuditLog(c.job_dir(j) / "audit.jsonl").read()]


def store(c: Console) -> F.FeedbackStore:
    return F.FeedbackStore(Path(c.root) / "feedback", "sponsor-a")


def test_undo_of_a_sponsor_answer_reverts_the_recipe_audits_first_and_asks_again(c: Console) -> None:
    j = own_job(c, ROWS)
    q = questions(c, j)["Commitment_Date"]
    got = answer(c, j, q, recommended(q), scope="sponsor")
    assert recipe_path(c).is_file() and "Commitment_Date" not in questions(c, j)
    out = c.undo_answer(j["id"], q["question_id"], ANALYST)
    assert out["question_id"] == q["question_id"] and out["by"] == ANALYST and out["recipe_reverted"] is True
    assert not recipe_path(c).exists()                       # the only step was the one undone
    acts = actions(c, j)
    assert acts.index("recipe.changed") < acts.index("recipe.reverted")
    assert store(c).undone() == {got["feedback_id"]}          # an undo record; the decision itself stays
    assert [r["feedback_id"] for r in store(c).list(kind=F.FIELD_DECISION)] == [got["feedback_id"]]
    assert "Commitment_Date" in questions(c, j)               # the next evaluation asks again
    with pytest.raises(ConsoleError, match="no answer"):
        c.undo_answer(j["id"], q["question_id"], ANALYST)     # it is not undone twice


def test_undo_of_a_job_answer_leaves_no_step_and_is_recorded(c: Console) -> None:
    j = own_job(c, ROWS)
    q = questions(c, j)["Commitment_Date"]
    got = answer(c, j, q, recommended(q), scope="job")
    assert "Commitment_Date" in j["map"] or "Commitment_Date" in (j.get("steps") or {})
    c.undo_answer(j["id"], q["question_id"], ANALYST)
    assert "Commitment_Date" not in j["map"] and "Commitment_Date" not in (j.get("steps") or {})
    assert "field_decision.undone" in actions(c, j)
    assert store(c).undone() == {got["feedback_id"]}
    assert "Commitment_Date" in questions(c, j)


def test_undo_has_the_reviewer_rules_of_an_answer(c: Console) -> None:
    j = own_job(c, ROWS)
    q = questions(c, j)["Commitment_Date"]
    answer(c, j, q, recommended(q), scope="sponsor")
    for who in ("", "local", "worker-agent", "system"):
        with pytest.raises(ConsoleError):
            c.undo_answer(j["id"], q["question_id"], who)
    assert recipe_path(c).is_file() and "recipe.reverted" not in actions(c, j)


def test_undo_through_act_uses_the_reviewer_and_the_answer_act_exists(c: Console) -> None:
    j = own_job(c, ROWS)
    q = questions(c, j)["Commitment_Date"]
    opt = recommended(q)
    with pytest.raises(ConsoleError, match="not a person"):
        c.act(f"answer|{j['id']}|{q['question_id']}|{opt}|sponsor", "", "local")
    c.act(f"answer|{j['id']}|{q['question_id']}|{opt}|sponsor", "their export", ANALYST)
    assert recipe_path(c).is_file()
    c.act(f"undo_answer|{j['id']}|{q['question_id']}", "", ANALYST)
    assert not recipe_path(c).exists() and "Commitment_Date" in questions(c, j)


def test_undo_over_http_needs_the_reviewer_header(tmp_path: Path) -> None:
    app = create_app(tmp_path / "state", environ={"WORKER_MODEL_PROVIDER": "demo",
                                                   "WORKER_ENV_FILE": str(tmp_path / "t.env")})
    cl = TestClient(app)
    r = cl.put("/api/console/jobs/ask/attach?name=allocation.xlsx&sponsor=Sponsor%20A&type=Commitments",
               content=book(HEAD, ROWS))
    assert r.status_code == 200, r.text
    for act in ("ask_set|cov|all", "ask_go"):
        assert cl.post("/api/console/act", json={"action": act, "text": ""}).status_code == 200
    j = cl.get("/api/console/state").json()["job"]
    if j.get("ex_pending"):
        cl.post("/api/console/act", json={"action": "ex|no", "text": ""})
    assert cl.post("/api/console/act", json={"action": "go", "text": ""}).status_code == 200
    q = next(x for x in cl.get("/api/console/state").json()["job"]["questions"] if x["field"] == "Commitment_Date")
    body = {"question_id": q["question_id"], "option_id": q["recommended"], "scope": "sponsor", "reason": ""}
    assert cl.post(f"/api/console/jobs/{j['id']}/answer", json=body).status_code == 409
    who = {"X-Reviewer": ANALYST}
    ok = cl.post(f"/api/console/jobs/{j['id']}/answer", json=body, headers=who)
    assert ok.status_code == 200, ok.text
    undo = {"action": f"undo_answer|{j['id']}|{q['question_id']}", "text": ""}
    assert cl.post("/api/console/act", json=undo).status_code == 409                # no reviewer: refused
    assert cl.post("/api/console/act", json=undo, headers=who).status_code == 200
    again = cl.get("/api/console/state").json()["job"]["questions"]
    assert any(x["field"] == "Commitment_Date" for x in again)


def test_the_job_view_lists_the_answers_in_force_so_undo_survives_a_reload(c: Console) -> None:
    j = own_job(c, ROWS)
    q = questions(c, j)["Commitment_Date"]
    assert state_view(c)["job"]["answered"] == []
    answer(c, j, q, recommended(q), scope="sponsor")
    got = state_view(c)["job"]["answered"]
    assert [(a["question_id"], a["field"], a["scope"], a["by"]) for a in got] == [
        (q["question_id"], "Commitment_Date", "sponsor", ANALYST)]
    assert got[0]["text"].startswith("Saved for Sponsor A") and "Commit Date" in got[0]["text"]
    c.undo_answer(j["id"], q["question_id"], ANALYST)
    assert state_view(c)["job"]["answered"] == []


def decide_and_approve(c: Console, j: dict[str, Any], scope: str) -> None:
    for q in questions(c, j).values():
        if q["recommended"]:
            answer(c, j, q, recommended(q), scope=scope)
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    assert j["approved"]


def test_undo_after_approval_reverts_only_the_sponsor_recipe_and_leaves_the_finished_job(c: Console) -> None:
    j = own_job(c, ROWS)
    q = questions(c, j)["Commitment_Date"]
    decide_and_approve(c, j, "sponsor")
    out = c.undo_answer(j["id"], q["question_id"], ANALYST)
    assert out["recipe_reverted"] is True and j["approved"]
    acts = actions(c, j)
    assert acts.index("recipe.changed") < acts.index("recipe.reverted")
    recipe = c.field_recipes("Sponsor A").load("Commitments")
    assert "Commitment_Date" not in ((recipe or {}).get("fields") or {})        # later jobs ask again
    c.act("start|Sponsor A|Commitments")                                      # (the approved job stays as it was)


def test_a_job_scope_answer_of_an_approved_job_is_not_undone(c: Console) -> None:
    j = own_job(c, ROWS)
    q = questions(c, j)["Commitment_Date"]
    decide_and_approve(c, j, "job")
    with pytest.raises(ConsoleError, match="finished work"):
        c.undo_answer(j["id"], q["question_id"], ANALYST)
