"""Ticket 11, step 4: the job screen's "Ask the job" tab and its endpoint. The container is replaced by the
in-process harness of `test_flex_ask`; the job is a real console job."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import worker.console_ask as CA
from port_fakes import FakeRunner
from test_console_working import start
from test_flex_ask import local_harness
from worker import console_engine as CE
from worker import console_working as W
from worker.console import Console
from worker.web import create_app


@pytest.fixture
def c(tmp_path: Path) -> Console:
    con = Console(tmp_path / "inbox")
    con.act("prov|paid")
    return con


@pytest.fixture
def sandbox_on(monkeypatch: pytest.MonkeyPatch) -> FakeRunner:
    r = FakeRunner(local_harness, image="sha256:" + "7" * 64)
    monkeypatch.setattr(CA, "runner_from_env", lambda blobs=None, env=None: r)
    return r


def test_the_tab_lists_the_job_tables_and_the_semantic_names(c: Console, sandbox_on: FakeRunner) -> None:
    j = start(c, "Investors")
    v = W.working_view(c, j, "ask")
    assert v["kind"] == "ask" and v["sandbox"] is True and "empty" not in v
    by = {t["name"]: t for t in v["tables"]}
    assert list(by) == ["job_rows", "out_Investors", "findings", "tie_out", "missing"] and by["job_rows"]["rows"] > 0
    assert {"row", "status", "source_id", "new_id", "name", "why"} <= set(by["job_rows"]["columns"])
    assert {"severity", "message", "count"} <= set(by["findings"]["columns"]) and by["tie_out"]["rows"] == 1
    assert {"field", "rows", "reason"} == set(by["missing"]["columns"])
    assert "Investor_ID" in by["out_Investors"]["columns"] and by["out_Investors"]["rows"] > 0
    assert "total_commitment" in v["terms"] and v["glossary"]


def test_a_question_is_answered_audited_and_leaves_the_job_alone(c: Console, sandbox_on: FakeRunner) -> None:
    j = start(c, "Investors")
    before = (len(c.S["audit"]), [dict(r) for r in j["runs"]])
    res = CA.ask_act(c, j, "how many rows were left out?",
                     "SELECT status, count(*) AS n FROM job_rows GROUP BY status ORDER BY status", "Priya Rao")
    assert res["status"] == "ok" and res["columns"] == ["status", "n"] and res["row_count"] >= 1
    assert sum(int(r[1]) for r in res["rows"]) == W.working_view(c, j, "ask")["tables"][0]["rows"]
    assert not hasattr(CA, "_rows_copied")
    assert (len(c.S["audit"]), j["runs"]) == before  # the job and its decisions are untouched
    hist = W.working_view(c, j, "ask")["history"]
    assert hist[-1]["question"] == "how many rows were left out?" and hist[-1]["status"] == "ok"
    assert sandbox_on.calls == 1


def test_a_write_is_refused_and_a_missing_sandbox_says_so(c: Console, monkeypatch: pytest.MonkeyPatch,
                                                         sandbox_on: FakeRunner) -> None:
    j = start(c, "Investors")
    res = CA.ask_act(c, j, "clean up", "DELETE FROM job_rows", "Priya Rao")
    assert res["status"] == "refused" and res["reason"] == "not_select" and sandbox_on.calls == 0
    monkeypatch.setattr(CA, "runner_from_env", lambda blobs=None, env=None: None)
    off = CA.ask_act(c, j, "x", "SELECT 1", "Priya Rao")
    assert off["reason"] == "no_sandbox"
    assert W.working_view(c, j, "ask")["sandbox"] is False


def test_before_a_run_there_is_nothing_to_ask(c: Console, sandbox_on: FakeRunner) -> None:
    j = start(c, "Investors", run=False)
    v = W.working_view(c, j, "ask")
    assert v["tables"] == [] and v["empty"]


def test_gl_jobs_get_a_lines_table(c: Console, sandbox_on: FakeRunner) -> None:
    j = start(c, "GL Fund", fund="B Fund I")
    v = W.working_view(c, j, "ask")
    assert [t["name"] for t in v["tables"]][:2] == ["job_lines", "out_GL_Fund"] and v["tables"][0]["rows"] > 0
    res = CA.ask_act(c, j, "debits", "SELECT count(*) AS n FROM job_lines", "Priya Rao")
    assert res["status"] == "ok" and int(res["rows"][0][0]) == v["tables"][0]["rows"]


def test_the_endpoint_and_the_script_carry_the_tab(tmp_path: Path, sandbox_on: FakeRunner) -> None:
    app = create_app(tmp_path / "state", environ={"WORKER_MODEL_PROVIDER": "demo",
                                                  "WORKER_ENV_FILE": str(tmp_path / "t.env")})
    http = TestClient(app)
    for a in ("start|Sponsor A|Investors", "ask_set|cov|all", "ask_sample", "ask_go"):
        http.post("/api/console/act", json={"action": a})
    jid = http.get("/api/console/state").json()["job"]["id"]
    tab = http.get(f"/api/console/jobs/{jid}/working/ask")
    assert tab.status_code == 200 and tab.json()["kind"] == "ask"
    r = http.post(f"/api/console/jobs/{jid}/ask", json={"question": "q", "sql": "SELECT 1 AS one"})
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["rows"] == [["1"]]
    assert http.post("/api/console/jobs/zzz/ask", json={"sql": "SELECT 1"}).status_code == 404
    js = http.get("/working.js").text
    assert '"ask"' in js and "/ask`" in js


def test_the_tables_come_from_a_parquet_cache_not_a_row_copy(c: Console, sandbox_on: FakeRunner) -> None:
    j = start(c, "Investors")
    tables = CA.job_tables(c, j)
    assert tables and all(p.suffix == ".parquet" and p.is_file() for p in tables.values())
    key = CE.evaluation(c, j).manifest.eval_key
    assert all(key in p.parts for p in tables.values())  # content-addressed by the evaluation
    first = {n: p.stat().st_mtime_ns for n, p in tables.items()}
    assert {n: p.stat().st_mtime_ns for n, p in CA.job_tables(c, j).items()} == first  # built once


def test_findings_tie_out_and_missing_can_be_asked(c: Console, sandbox_on: FakeRunner) -> None:
    j = start(c, "Investors")
    res = CA.ask_act(c, j, "which checks failed", "SELECT severity, count(*) AS n FROM findings GROUP BY severity",
                     "Priya Rao")
    assert res["status"] == "ok" and res["columns"] == ["severity", "n"]
    tie = CA.ask_act(c, j, "tie", "SELECT rows_read, rows_out FROM tie_out", "Priya Rao")
    assert tie["status"] == "ok" and int(tie["rows"][0][0]) >= int(tie["rows"][0][1])
    out = CA.ask_act(c, j, "written", "SELECT count(*) AS n FROM out_Investors", "Priya Rao")
    assert int(out["rows"][0][0]) == CE.tally(c, j)["out"]
