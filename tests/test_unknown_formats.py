"""Migration M24a: the person decides an unknown column or format, on the console backend, for real.

The source is the person's own workbook (its real headers are offered); an unmapped field or a column written in a
form nobody decided becomes a typed question with previews computed by code; an answer is recorded (audit first),
written as the sponsor's recipe or as this job's own step, and applied by the engine's convert stage on every later
job; a second sponsor sees what the first decided as one option and nothing is applied until a person answers.
Fictitious names only; no model is called.
"""

from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from fastapi.testclient import TestClient

from id_decision import decide_ids
from port_fakes import MemoryReader, MemoryWorld
from worker import console_demo as D
from worker import console_engine as CE
from worker import engine as E
from worker import feedback as F
from worker.audit import AuditError, AuditLog
from worker.console import Console, ConsoleError
from worker.console_screens import state_view
from worker.ports import ListAudit, MemoryStore, Ports, ProductionSkills
from worker.web import create_app

HEAD = ["LP ID", "Investor", "Entity (tab)", "Commit Amt", "Commit Date"]  # not the sample's column names
ROWS = [["HS-1", "Alpha Fund LP", "Fund A", 250000, "2025-03-31"],
        ["HS-2", "Beta Partners", "Fund A", 1000000, "2025-04-30"],
        ["HS-3", "Gamma Holdings", "Fund A", 750000, "2025-05-31"]]
PLAN_HEAD = ["LP ID", "Investor", "Entity (tab)", "Total Commitment", "Closing Date"]  # the plan's own names
KEY = "unit-test-learning-key"
ANALYST = "Priya Rao"


@pytest.fixture
def c(tmp_path: Path) -> Console:
    con = Console(tmp_path / "inbox")
    con.act("prov|paid")
    con.learning_key = KEY
    return con


def book(header: list[str], rows: list[list[Any]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Allocations"
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def seed_investors(c: Console, sponsor: str, rows: list[list[Any]]) -> None:
    c.store_add(sponsor, "Investors", [{"key": r[0], "name": r[1], "label": "seeded for the test"} for r in rows],
                "quick entry", "seed")


def own_job(c: Console, rows: list[list[Any]], header: list[str] = HEAD, *, sponsor: str = "Sponsor A",
            name: str = "allocation.xlsx", seed: bool = True) -> dict[str, Any]:
    if seed:
        seed_investors(c, sponsor, rows)
    c.act(f"start|{sponsor}|Commitments")
    c.act("ask_set|cov|all")
    c.ask_attach_bytes(name, book(header, rows))
    c.act("ask_go")
    j = c.job()
    assert j is not None
    if c.ex_pending(j):
        c.act("ex|no")
    c.act("go")
    return j


def questions(c: Console, j: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {q["field"]: q for q in state_view(c)["job"]["questions"]}


def answer(c: Console, j: dict[str, Any], q: dict[str, Any], option_id: str | None = None, *, scope: str = "sponsor",
           who: str = ANALYST, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"question_id": q["question_id"], "scope": scope, "reason": "", **extra}
    if option_id is not None:
        body["option_id"] = option_id
    return c.answer_question(j["id"], body, who)


def recommended(q: dict[str, Any]) -> str:
    assert q["recommended"], q
    return str(q["recommended"])


def decide_both(c: Console, j: dict[str, Any], scope: str = "sponsor") -> None:
    qs = questions(c, j)
    for field in ("Commitment_Amount", "Commitment_Date"):
        answer(c, j, qs[field], recommended(qs[field]), scope=scope)


def written(path: str | Path) -> list[dict[str, Any]]:
    book_ = openpyxl.load_workbook(path, read_only=True)
    try:
        rows = list(book_.worksheets[0].iter_rows(values_only=True))
    finally:
        book_.close()
    # a date cell is the date it holds: compared as its ISO text, which is what the person's file said
    iso = [[v.date().isoformat() if isinstance(v, datetime) else v for v in r] for r in rows]
    return [dict(zip(iso[0], r, strict=False)) for r in iso[1:]]


def recipe_path(c: Console, sponsor: str = "Sponsor A") -> Path:
    return c.field_recipes(sponsor).path("Commitments")


# ---- (1) the source file is the person's own ---------------------------------------------------------------------
def test_own_file_shows_its_real_headers_not_the_sample_columns(c: Console) -> None:
    j = own_job(c, ROWS)
    v = state_view(c)["job"]
    plan = {f["field"]: f for f in v["plan"]["fields"]}
    assert j["up"]["own"] is True and v["file"] == "allocation.xlsx"
    assert plan["Commitment_Amount"]["columns"] == HEAD != D.SRCCOLS["Commitments"]
    assert "Total Commitment" not in plan["Commitment_Amount"]["columns"]
    assert v["plan"]["rows"] == len(ROWS)


def test_attach_over_http_uses_the_inbox_limits_and_the_same_file_is_not_replaced_by_another(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(tmp_path / "state", environ={"WORKER_MODEL_PROVIDER": "demo",
                                                   "WORKER_ENV_FILE": str(tmp_path / "t.env")})
    cl = TestClient(app)
    ok = cl.put("/api/console/jobs/ask/attach?name=allocation.xlsx&sponsor=Sponsor%20A&type=Commitments",
                content=book(HEAD, ROWS))
    assert ok.status_code == 200, ok.text
    assert ok.json()["attach"]["columns"] == HEAD and ok.json()["ask"]["file"] == "allocation.xlsx"
    assert cl.post("/api/console/jobs/ask/attach?name=allocation.xlsx", content=book(HEAD, ROWS)).status_code == 200
    for name, body, why in (("notes.png", b"x", "Excel workbook"), ("a/b.xlsx", b"x", "not allowed"),
                            ("x_answer_key.xlsx", book(HEAD, ROWS), "answer key"), ("empty.xlsx", b"", "empty"),
                            ("broken.xlsx", b"not a workbook", "cannot be read")):
        r = cl.put(f"/api/console/jobs/ask/attach?name={name}", content=body)
        assert r.status_code == 409 and why in r.json()["detail"], (name, r.text)
    state = tmp_path / "state" / "console_inbox"
    first = (state / "allocation.xlsx").read_bytes()
    other = cl.put("/api/console/jobs/ask/attach?name=allocation.xlsx", content=book(HEAD, ROWS[:2]))
    assert other.status_code == 200
    assert (state / "allocation.xlsx").read_bytes() == first  # a different file of that name does not replace it
    assert (state / "allocation_v2.xlsx").is_file()
    monkeypatch.setattr("worker.web.MAX_UPLOAD_BYTES", 10)
    big = cl.put("/api/console/jobs/ask/attach?name=big.xlsx", content=b"x" * 11)
    assert big.status_code == 413


# ---- (3) questions -------------------------------------------------------------------------------------------------
def test_an_unmapped_field_asks_with_the_files_columns_and_a_preview(c: Console) -> None:
    j = own_job(c, ROWS)
    qs = questions(c, j)
    q = qs["Commitment_Date"]
    assert q["kind"] == "unknown_field" and q["scopes"] == ["job", "sponsor"] and q["blocking"] is False
    assert q["columns"] == HEAD
    pick = next(o for o in q["options"] if o["column"] == "Commit Date")
    assert pick["preview"] == {"conform": 3, "total": 3, "text": "3/3 rows"} and pick["proposed_by"] == "mapper"
    assert q["recommended"] == pick["option_id"]  # every row conforms: code recommends it, the person decides
    assert any(o["action"] == "declare_missing" for o in q["options"])
    assert 1 <= len(q["options"]) <= 4
    assert all(r["st"] == "err" for r in CE.static_rows(c, j))  # nothing is applied before the answer


def test_an_ambiguous_date_column_has_no_recommendation_and_shows_counts_and_examples(c: Console) -> None:
    rows = [["HS-1", "Alpha Fund LP", "Fund A", 250000, "03/04/2025"],
            ["HS-2", "Beta Partners", "Fund A", 1000000, "25/12/2025"],
            ["HS-3", "Gamma Holdings", "Fund A", 750000, "01/02/2025"]]
    j = own_job(c, rows, PLAN_HEAD)
    q = questions(c, j)["Commitment_Date"]
    assert q["kind"] == "unknown_format" and q["blocking"] is True and q["column"] == "Closing Date"
    assert q["recommended"] is None
    by = {tuple(o["ops"][0]["patterns"]): o for o in q["options"] if o["ops"]}
    day, month = by[("D/M/YYYY",)], by[("M/D/YYYY",)]
    assert day["person_choice"] and month["person_choice"]
    assert day["preview"]["text"] == "3/3 rows" and month["preview"]["text"] == "2/3 rows"
    assert [e["source_value"] for e in month["examples"]] == ["25/12/2025"]
    assert q["evidence"] == {"first_number_above_12": 1, "second_number_above_12": 0}
    with pytest.raises(ConsoleError, match="Decide how Commitment_Date"):
        decide_ids(c)
        c.act("approve", who="reviewer-a")  # it would be written as text; a person decides first
    assert state_view(c)["job"]["can_approve"] is False


def test_even_when_the_evidence_is_decisive_day_or_month_is_not_recommended(c: Console) -> None:
    rows = [["HS-1", "Alpha Fund LP", "Fund A", 250000, "25/12/2025"],
            ["HS-2", "Beta Partners", "Fund A", 1000000, "31/01/2025"]]
    j = own_job(c, rows, PLAN_HEAD)
    q = questions(c, j)["Commitment_Date"]
    day = next(o for o in q["options"] if o["ops"] and o["ops"][0]["patterns"] == ["D/M/YYYY"])
    assert day["preview"]["text"] == "2/2 rows" and q["recommended"] is None


def test_a_number_column_asks_and_the_decimal_mark_is_never_guessed_when_both_readings_fit(c: Console) -> None:
    one = [["HS-1", "Alpha Fund LP", "Fund A", "46,418.20", "2025-03-31"],
           ["HS-2", "Beta Partners", "Fund A", "1,000.50", "2025-04-30"]]
    j = own_job(c, one, PLAN_HEAD)
    q = questions(c, j)["Commitment_Amount"]
    assert q["kind"] == "unknown_format" and q["blocking"] is True
    only = next(o for o in q["options"] if o["ops"])
    assert only["ops"][0]["decimal"] == "." and only["preview"]["text"] == "2/2 rows"
    assert q["recommended"] == only["option_id"]  # the comma reading fits no row: the counts decide, not a guess
    both = [["HS-1", "Alpha Fund LP", "Fund A", "1,234", "2025-03-31"],
            ["HS-2", "Beta Partners", "Fund A", "2,500", "2025-04-30"]]
    c2 = Console(Path(c.inbox).parent / "second" / "inbox")
    c2.learning_key = KEY
    j2 = own_job(c2, both, PLAN_HEAD)
    q2 = questions(c2, j2)["Commitment_Amount"]
    assert q2["recommended"] is None and sum(1 for o in q2["options"] if o["person_choice"]) == 2


# ---- (2)+(4) recipes applied, answers recorded --------------------------------------------------------------------
def test_a_sponsor_answer_changes_the_written_values_the_pin_and_the_recipe_file(c: Console) -> None:
    j = own_job(c, ROWS)
    decide_both(c, j)
    assert [r["st"] for r in CE.static_rows(c, j)] == ["ok", "ok", "ok"]
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    got = written(j["out"]["path"])
    assert [r["Commitment_Date"] for r in got] == ["2025-03-31", "2025-04-30", "2025-05-31"]
    assert [r["Commitment_Amount"] for r in got] == [250000, 1000000, 750000]
    pin = c.field_recipes("Sponsor A").pin("Commitments")
    assert pin and j["manifest"]["field_recipes"] == {"Commitments": pin}
    assert recipe_path(c).is_file() and recipe_path(c).parent.name == "recipes"
    last = AuditLog(c.job_dir(j) / "engine.audit.jsonl")
    assert last is not None  # the engine's own log exists beside the job


def test_a_recipe_that_parses_text_changes_what_is_written_and_failed_rows_are_errors_never_passed(c: Console) -> None:
    rows = [["HS-1", "Alpha Fund LP", "Fund A", "46,418.20", "03/04/2025"],
            ["HS-2", "Beta Partners", "Fund A", "1,000.50", "31/02/2025"],
            ["HS-3", "Gamma Holdings", "Fund A", "750.00", "25/12/2025"]]
    j = own_job(c, rows, PLAN_HEAD)
    qs = questions(c, j)
    amount = qs["Commitment_Amount"]
    answer(c, j, amount, recommended(amount))
    date = qs["Commitment_Date"]
    day = next(o for o in date["options"] if o["ops"] and o["ops"][0]["patterns"] == ["D/M/YYYY"])
    answer(c, j, date, day["option_id"])  # the person picks day first
    st = CE.static_rows(c, j)
    assert [r["st"] for r in st] == ["ok", "err", "ok"]
    bad = st[1]
    assert bad["failed"] == [{"field": "Commitment_Date", "code": "not_a_date"}]
    assert "31/02/2025" not in bad["why"] and "could not be read" in bad["why"]
    ev = CE.evaluation(c, j)
    f = next(x for x in ev.findings if x.rule.rule_id == "console.transform_failed")
    assert f.severity == "ERR" and f.field == "Commitment_Date" and f.detail["failure"] == "not_a_date"
    assert "31/02/2025" not in f.message and f.count == 1
    # the original value is only in the failure record
    assert [(x["field"], x["code"], x["value"]) for x in ev.facts["failures"]] == [
        ("Commitment_Date", "not_a_date", "31/02/2025")]
    with pytest.raises(ConsoleError, match="still need you"):
        decide_ids(c)
        c.act("approve", who="reviewer-a")
    c.fill(j, str(bad["i"]), "Commitment_Date", "2025-02-28")  # a person's own value, typed, never inferred
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    got = written(j["out"]["path"])
    assert [r["Commitment_Date"] for r in got] == ["2025-04-03", "2025-02-28", "2025-12-25"]
    assert [float(r["Commitment_Amount"]) for r in got] == [46418.2, 1000.5, 750.0]


def test_the_answer_goes_to_the_audit_before_the_recipe_file_and_a_refusal_changes_nothing(
        c: Console, monkeypatch: pytest.MonkeyPatch) -> None:
    j = own_job(c, ROWS)
    qs = questions(c, j)
    real = AuditLog.record

    def failing(self: AuditLog, action: str, *a: Any, **kw: Any) -> Any:
        if action == "recipe.changed":
            raise AuditError("disk full")
        return real(self, action, *a, **kw)

    monkeypatch.setattr(AuditLog, "record", failing)
    with pytest.raises(ConsoleError, match="could not be recorded"):
        answer(c, j, qs["Commitment_Date"], recommended(qs["Commitment_Date"]))
    assert not recipe_path(c).exists()  # the file never exists without its audit record
    assert not j.get("answers") and not j.get("steps") and "Commitment_Date" in questions(c, j)
    monkeypatch.setattr(AuditLog, "record", real)
    answer(c, j, qs["Commitment_Date"], recommended(qs["Commitment_Date"]))
    actions = [e["action"] for e in AuditLog(c.job_dir(j) / "audit.jsonl").read()]
    assert actions.index("feedback") < actions.index("recipe.changed")
    history = AuditLog(recipe_path(c).with_name("Commitments.history.jsonl")).read()
    assert [e["action"] for e in history] == ["recipe.changed"]


@pytest.mark.parametrize("who", ["Mapper", "agent", "local", "improver", "console", "gpt-4o", "", "writer"])
def test_an_agent_or_reserved_name_cannot_answer(c: Console, who: str) -> None:
    j = own_job(c, ROWS)
    q = questions(c, j)["Commitment_Date"]
    with pytest.raises(ConsoleError, match="person|say who you are"):
        answer(c, j, q, recommended(q), who=who)
    assert not recipe_path(c).exists() and not j.get("answers")
    assert not (Path(c.root) / "feedback").exists()


def test_the_http_answer_uses_the_reviewer_and_refuses_the_default_local_name(tmp_path: Path) -> None:
    app = create_app(tmp_path / "state", environ={"WORKER_MODEL_PROVIDER": "demo",
                                                   "WORKER_ENV_FILE": str(tmp_path / "t.env")})
    cl = TestClient(app)
    cl.put("/api/console/jobs/ask/attach?name=allocation.xlsx&sponsor=Sponsor%20A&type=Commitments",
           content=book(HEAD, ROWS))
    cl.post("/api/console/act", json={"action": "ask_set|cov|all"})
    cl.post("/api/console/act", json={"action": "ask_go"})
    st = cl.get("/api/console/state").json()
    job = st["job"]
    if job["ex_pending"]:
        cl.post("/api/console/act", json={"action": "ex|no"})
    q = next(x for x in cl.get("/api/console/state").json()["job"]["questions"] if x["field"] == "Commitment_Date")
    body = {"question_id": q["question_id"], "option_id": q["recommended"], "scope": "sponsor", "reason": ""}
    refused = cl.post(f"/api/console/jobs/{job['id']}/answer", json=body)
    assert refused.status_code == 409 and "not a person" in refused.json()["detail"]
    org = cl.post(f"/api/console/jobs/{job['id']}/answer", json={**body, "scope": "organisation"},
                  headers={"X-Reviewer": ANALYST})
    assert org.status_code == 409 and "this job or this sponsor" in org.json()["detail"]
    ok = cl.post(f"/api/console/jobs/{job['id']}/answer", json=body, headers={"X-Reviewer": ANALYST})
    assert ok.status_code == 200, ok.text
    got = ok.json()["answered"]
    assert got["by"] == ANALYST and got["scope"] == "sponsor" and got["action"] == "accept" and got["recipe_changed"]
    left = {x["field"] for x in ok.json()["job"]["questions"]}
    assert "Commitment_Date" not in left


def test_accept_and_override_are_recorded_and_a_job_answer_is_this_jobs_alone(c: Console) -> None:
    j = own_job(c, ROWS)
    qs = questions(c, j)
    other = next(o for o in qs["Commitment_Amount"]["options"] if o["action"] == "declare_missing")
    a = answer(c, j, qs["Commitment_Amount"], other["option_id"], scope="job", reason="not in this file")
    b = answer(c, j, qs["Commitment_Date"], recommended(qs["Commitment_Date"]), scope="job")
    assert (a["action"], b["action"]) == ("override", "accept")
    assert not recipe_path(c).exists()  # job scope: feedback only, the sponsor's recipe is not touched
    assert j["map"]["Commitment_Amount"] == "" and j["steps"]["Commitment_Date"]["from"] == "Commit Date"
    recs = F.FeedbackStore(Path(c.root) / "feedback", "sponsor-a").list(kind=F.FIELD_DECISION)
    assert [(r["target_field"], r["scope"], r["action"]) for r in recs] == [
        ("Commitment_Amount", "job", "override"), ("Commitment_Date", "job", "accept")]
    assert all(r["principal"]["name"] == ANALYST and r["recipe_after"] is None for r in recs)
    c.act("go")
    j2 = own_job(c, ROWS, name="allocation_two.xlsx", seed=False)  # a next job asks again: nothing was kept
    assert {"Commitment_Amount", "Commitment_Date"} <= set(questions(c, j2))


def test_the_organisation_scope_is_never_offered(c: Console) -> None:
    j = own_job(c, ROWS)
    q = questions(c, j)["Commitment_Date"]
    assert q["scopes"] == ["job", "sponsor"]
    for scope in ("organisation", "organization", "every sponsor", ""):
        with pytest.raises(ConsoleError, match="this job or this sponsor"):
            answer(c, j, q, recommended(q), scope=scope)
    assert not recipe_path(c).exists()


def test_a_custom_choice_is_a_column_of_the_file_with_a_reason_and_a_closed_step(c: Console) -> None:
    rows = [["HS-1", "Alpha Fund LP", "Fund A", 250000, "31.03.2025"],
            ["HS-2", "Beta Partners", "Fund A", 1000000, "30.04.2025"]]
    j = own_job(c, rows)
    q = questions(c, j)["Commitment_Date"]
    custom = {"action": "transform", "header": "Commit Date",
              "ops": [{"op": "date_parse", "patterns": ["DD.MM.YYYY"]}]}
    with pytest.raises(ConsoleError, match="needs a reason"):
        answer(c, j, q, None, custom=custom)
    with pytest.raises(ConsoleError, match="not a column of this file"):
        answer(c, j, q, None, custom={**custom, "header": "Nope"}, reason="mine")
    with pytest.raises(ConsoleError, match="closed vocabulary"):
        answer(c, j, q, None, custom={**custom, "ops": [{"op": "eval", "code": "1"}]}, reason="mine")
    got = answer(c, j, q, None, custom=custom, reason="this client writes dd.mm.yyyy")
    assert got["action"] == "override"
    rec = F.FeedbackStore(Path(c.root) / "feedback", "sponsor-a").list(kind=F.FIELD_DECISION)[-1]
    assert rec["chosen"]["custom"]["proposed_by"] == "human" and rec["reason"] == "this client writes dd.mm.yyyy"


# ---- (5) the next job, and a second sponsor ----------------------------------------------------------------------
def test_the_next_job_applies_the_saved_recipe_without_asking(c: Console) -> None:
    j = own_job(c, ROWS)
    decide_both(c, j)
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    again = [["HS-1", "Alpha Fund LP", "Fund A", 90000, "2025-06-30"],
             ["HS-2", "Beta Partners", "Fund A", 80000, "2025-07-31"]]
    j2 = own_job(c, again, name="allocation_q3.xlsx", seed=False)
    v = state_view(c)["job"]
    assert v["questions"] == [] and not j2.get("answers")
    plan = {f["field"]: f for f in v["plan"]["fields"]}
    assert plan["Commitment_Date"]["source"] == "Commit Date" and plan["Commitment_Date"]["kind"] == "user"
    assert plan["Commitment_Date"]["recipe"]["scope"] == "sponsor"
    assert [r["st"] for r in CE.static_rows(c, j2)] != ["err", "err"]
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    got = written(j2["out"]["path"])
    assert [r["Commitment_Date"] for r in got] == ["2025-06-30", "2025-07-31"]
    assert j2["manifest"]["field_recipes"] == j["manifest"]["field_recipes"]


def test_a_second_sponsor_with_the_same_header_and_shape_gets_one_suggestion_never_an_application(c: Console) -> None:
    ja = own_job(c, ROWS)
    decide_both(c, ja)
    rows_b = [["HS-91", "Delta Capital", "B Fund I", 500000, "2025-01-31"],
              ["HS-92", "Epsilon Trust", "B Fund I", 650000, "2025-02-28"]]
    jb = own_job(c, rows_b, sponsor="Sponsor B", name="b_allocation.xlsx")
    q = questions(c, jb)["Commitment_Date"]
    learned = [o for o in q["options"] if o["proposed_by"] == "learned"]
    assert len(learned) == 1 and learned[0]["suggestion_id"] and learned[0]["preview"]["text"] == "2/2 rows"
    assert c.field_recipes("Sponsor B").pin("Commitments") is None  # nothing was applied
    assert all(r["st"] == "err" for r in CE.static_rows(c, jb))
    assert "manifest" not in jb
    got = answer(c, jb, q, learned[0]["option_id"], scope="sponsor")
    rec = F.FeedbackStore(Path(c.root) / "feedback", "sponsor-b").list(kind=F.FIELD_DECISION)[-1]
    assert rec["via_suggestion"] == learned[0]["suggestion_id"] and got["recipe_changed"]
    assert c.field_recipes("Sponsor B").pin("Commitments")


def test_without_a_learning_key_there_is_no_cross_sponsor_hint_and_nothing_else_changes(c: Console) -> None:
    c.learning_key = None
    own_job(c, ROWS)
    j = c.job()
    assert j is not None
    decide_both(c, j)
    jb = own_job(c, [["HS-91", "Delta Capital", "B Fund I", 500000, "2025-01-31"]], sponsor="Sponsor B",
                 name="b2.xlsx")
    q = questions(c, jb)["Commitment_Date"]
    import os

    if not os.environ.get("WORKER_LEARNING_KEY"):
        assert not any(o["proposed_by"] == "learned" for o in q["options"])


# ---- what stays as it was --------------------------------------------------------------------------------------
def test_the_sample_flow_has_no_questions_no_recipe_pin_and_the_same_file(c: Console) -> None:
    c.act("start|Sponsor A|Investors")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    if c.ex_pending(j):
        c.act("ex|no")
    c.act("go")
    v = state_view(c)["job"]
    assert v["questions"] == [] and v["questions_blocking"] == []
    plan = {f["field"]: f for f in v["plan"]["fields"]}
    assert plan["Investor_Type"]["columns"] == D.SRCCOLS["Investors"]
    c.act("acceptall")
    decide_ids(c)
    c.act("approve", who="reviewer-a")
    assert "field_recipes" not in j["manifest"]
    assert not (Path(c.root) / "sponsors").exists()


def test_the_plan_table_column_choice_is_a_job_scope_field_decision(c: Console) -> None:
    c.act("start|Sponsor A|Investors")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    c.act("col|Investor_Type|Type", who="Asha Menon")
    assert j["map"]["Investor_Type"] == "Type"
    recs = F.FeedbackStore(Path(c.root) / "feedback", "sponsor-a").list(kind=F.FIELD_DECISION)
    assert len(recs) == 1 and recs[0]["scope"] == "job" and recs[0]["principal"]["name"] == "Asha Menon"
    assert recs[0]["target_field"] == "Investor_Type" and recs[0]["source"]["header"] == "Type"
    assert not (Path(c.root) / "sponsors").exists()  # the sponsor's recipe is changed only by a sponsor answer


# ---- the engine, for an agent-built spec ------------------------------------------------------------------------
def test_an_agent_spec_gets_the_sponsors_recipe_from_its_world_and_the_manifest_pins_it(tmp_path: Path) -> None:
    rows = [["Investor ID", "Investor Name", "Country"], ["HS-1", "Alpha Fund LP", "us"], ["HS-2", "Beta Partners", "uk"]]
    steps = [{"step": "lift", "source": 0, "columns": {"Investor_ID": "A", "Investor_Name": "B", "Country": "C"},
              "header_row": 1, "first_row": 2, "constants": {"GP_ID": "sponsor-a"}}]
    recipe_step = {"step": "transform", "source": 0,
                   "recipe": {"schema_version": 1, "id": "Investors", "sponsor": "sponsor-a",
                              "fields": {"Country": {"from": "Country", "ops": [{"op": "case", "to": "upper"}]}}},
                   "field_recipe": {"pin": "sha256:abc", "record_type": "Investors", "approved_by": ANALYST}}
    world = MemoryWorld("sponsor-a", schemes={"Investors": {"mode": "sequential", "prefix": "GINV-", "width": 4,
                                                            "start": 1, "pattern": ""}})
    ports = Ports(world, MemoryReader({"investors.csv": rows}), ListAudit(), MemoryStore(), tmp_path / "out",
                  ProductionSkills("sponsor-a"))
    spec = E.JobSpec("job-1", "sponsor-a", "Investors", None, (E.SourceRef("file", "investors.csv", None, 1, ""),),
                     E.PinSet(), E.Recipe.of(*steps), (), {"gp_id": "sponsor-a"})
    plain = E.evaluate(spec, ports)
    assert [o.dest["Country"] for o in plain.rows()] == ["us", "uk"]
    assert "field_recipes" not in plain.manifest.as_record()  # a world with no recipe changes nothing
    world.field_recipe = lambda rt: recipe_step if rt == "Investors" else None  # type: ignore[attr-defined]
    fresh = Ports(world, MemoryReader({"investors.csv": rows}), ListAudit(), MemoryStore(), tmp_path / "out2",
                  ProductionSkills("sponsor-a"))
    with_recipe = E.evaluate(spec, fresh)
    assert [o.dest["Country"] for o in with_recipe.rows()] == ["US", "UK"]
    assert with_recipe.manifest.as_record()["field_recipes"] == {"Investors": "sha256:abc"}
    assert with_recipe.manifest.eval_key != plain.manifest.eval_key

