"""Step 4 of ticket 11: ask the job. One read-only DuckDB SELECT per question over this job's tables and the semantic
layer; a numeric-claim lint on the prose; the result never feeds a job; every question audited first.

`local_harness` runs the same DuckDB statements as `sandbox/query.py` on the input files the host built, so the
host's CSVs, column types and compiled views are exercised for real; the container itself is covered by the
`sandbox` tests at the end."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import duckdb
import pytest

from port_fakes import FakeRunner
from test_tool_list import _ctx
from worker.audit import AuditError, AuditLog
from worker.flex import ask as A
from worker.flex.runner import RunResult
from worker.replay import BlobStore
from worker.tools import build_tools

INVESTORS = [
    {"Investor_ID": "0007", "Investor_Name": "Alpha Fund LP", "Country": "US"},
    {"Investor_ID": "0008", "Investor_Name": "Beta Partners", "Country": "UK"},
    {"Investor_ID": "0009", "Investor_Name": "Gamma Holdings", "Country": "UK"},
]
COMMITMENTS = [
    {"Commitment_ID": "C1", "Investor_ID": "0007", "Commitment_Amount": "1000.50", "Currency": "USD"},
    {"Commitment_ID": "C2", "Investor_ID": "0007", "Commitment_Amount": "250", "Currency": "USD"},
    {"Commitment_ID": "C3", "Investor_ID": "0008", "Commitment_Amount": "75.25", "Currency": "USD"},
]
TABLES = {"src_1": INVESTORS, "src_2": COMMITMENTS}
ALIASES = {"t_Investors": "src_1", "t_Commitments": "src_2"}


def local_harness(kind: str, program: bytes, inputs: dict[str, bytes], seed: int) -> bytes:
    """What sandbox/query.py does, in a temp directory instead of /in."""
    assert kind == "query"
    with tempfile.TemporaryDirectory() as d:
        for name, data in inputs.items():
            (Path(d) / name).write_bytes(data)
        tables = json.loads(inputs["tables.json"])
        con = duckdb.connect(":memory:")
        for t in tables:
            spec = "{" + ", ".join(f"'{n}': '{ty}'" for n, ty in t["columns"]) + "}"
            con.execute(f'CREATE TABLE "{t["name"]}" AS SELECT * FROM read_csv(?, header=true, columns={spec})',
                        [str(Path(d) / t["file"])])
        for stmt in inputs["views.sql"].decode().split(";\n"):
            if stmt.strip():
                con.execute(stmt)
        sql = inputs["query.sql"].decode().strip().rstrip(";")
        cur = con.execute(f"SELECT * FROM ({sql}) AS q LIMIT 10001")
        out = {"columns": [c[0] for c in cur.description],
               "rows": [[None if v is None else str(v) for v in r] for r in cur.fetchall()]}
    return json.dumps(out).encode()


class Sink:
    def __init__(self, fail_on: str = "") -> None:
        self.rows: list[tuple[str, dict, dict]] = []
        self.fail_on = fail_on

    def record(self, action: str, rationale: str, inputs: dict | None = None, outputs: dict | None = None) -> dict:
        if action == self.fail_on:
            raise AuditError("sink down")
        self.rows.append((action, dict(inputs or {}), dict(outputs or {})))
        return {}

    def actions(self) -> list[str]:
        return [r[0] for r in self.rows]


def run(tmp_path: Path, sql: str, summary: str = "", audit: Any = None, runner: Any = "fake",
        tables: Any = None) -> dict[str, Any]:
    r = FakeRunner(local_harness, image="sha256:" + "5" * 64) if runner == "fake" else runner
    return A.ask(runner=r, audit=audit or Sink(), blobs=BlobStore(tmp_path / "blobs"), tables=tables or TABLES,
                 aliases=ALIASES, question="how much is committed?", sql=sql, why="the human asked", summary=summary)


# ---- the statement check ----------------------------------------------------------------------------------
@pytest.mark.parametrize("sql", [
    "SELECT * FROM src_1",
    "select count(*) from t_Investors where Country = 'UK'",
    "WITH x AS (SELECT Investor_ID, sum(Commitment_Amount) s FROM src_2 GROUP BY 1) SELECT * FROM x ORDER BY s",
    "SELECT a.Investor_Name FROM src_1 a FULL JOIN src_2 b ON a.Investor_ID = b.Investor_ID WHERE b.Investor_ID IS NULL",
    "SELECT 'a set of copy' AS note",  # words inside a string are not statements
])
def test_a_plain_select_is_allowed(sql: str) -> None:
    assert A.check_select(sql)


@pytest.mark.parametrize("sql,reason", [
    ("COPY src_1 TO '/tmp/x.csv'", "not_select"),
    ("ATTACH '/tmp/x.db'", "not_select"),
    ("INSTALL httpfs", "not_select"),
    ("LOAD httpfs", "not_select"),
    ("PRAGMA database_list", "forbidden"),  # DuckDB parses a PRAGMA as a SELECT; the word list catches it
    ("SET enable_external_access = true", "not_select"),
    ("CALL pragma_version()", "not_select"),
    ("EXPORT DATABASE '/tmp/x'", "not_select"),
    ("SELECT 1; SELECT 2", "not_select"),
    ("CREATE TABLE t AS SELECT 1", "not_select"),
    ("INSERT INTO src_1 VALUES (1)", "not_select"),
    ("SELECT * FROM read_csv('/etc/hostname')", "forbidden"),
    ("SELECT * FROM parquet_scan('x.parquet')", "forbidden"),
    ("SELECT * FROM glob('/*')", "forbidden"),
    ("SELECT * FROM '/etc/hostname'", "file_access"),
    ('SELECT * FROM "data/file.csv"', "file_access"),
    ("SELECT * FROM query('SELECT 1')", "forbidden"),
    ("SELECT getenv('OPENAI_API_KEY')", "forbidden"),
    ("SELECT * FROM duckdb_settings()", "forbidden"),
    ("", "empty"),
    ("SELECT " + "1," * 5000 + "1", "too_long"),
    ("SELEC nonsense", "syntax"),
])
def test_everything_else_is_refused_with_a_reason(sql: str, reason: str) -> None:
    with pytest.raises(A.AskRefused) as exc:
        A.check_select(sql)
    assert exc.value.reason == reason


# ---- the tables, the types and the semantic layer ---------------------------------------------------------
def test_ids_stay_text_and_amounts_become_decimals() -> None:
    _files, manifest = A.table_inputs(TABLES)
    cols = {c[0]: c[1] for m in manifest for c in m["columns"]}
    assert cols["Investor_ID"] == "VARCHAR" and cols["Commitment_Amount"] == "DECIMAL(38,10)"
    assert cols["Country"] == "VARCHAR"


def test_a_question_over_the_job_tables_is_answered(tmp_path: Path) -> None:
    res = run(tmp_path, "SELECT Country, count(*) AS n FROM t_Investors GROUP BY Country ORDER BY Country")
    assert res["status"] == "ok" and res["columns"] == ["Country", "n"]
    assert res["rows"] == [["UK", "2"], ["US", "1"]] and res["row_count"] == 2
    assert BlobStore(tmp_path / "blobs").get(res["result_sha256"])


def test_the_semantic_views_are_compiled_over_what_the_job_has(tmp_path: Path) -> None:
    res = run(tmp_path, 'SELECT total_commitment FROM v_total_commitment')
    assert res["rows"] == [["1325.75"]] and "v_total_commitment" in res["views"]
    joined = run(tmp_path, 'SELECT "Investors.Investor_Name", "Commitments.Commitment_Amount" '
                           'FROM v_Investors__Commitments ORDER BY 2')
    assert [r[0] for r in joined["rows"]] == ["Beta Partners", "Alpha Fund LP", "Alpha Fund LP"]
    only_investors = run(tmp_path, "SELECT * FROM v_investor_count", tables={"src_1": INVESTORS})
    assert only_investors["status"] == "failed" or "v_total_commitment" not in only_investors["views"]


def test_the_semantic_file_is_data_only() -> None:
    doc = A.load_semantic()
    assert set(doc) == {"version", "glossary", "terms", "relationships"}
    text = Path(A.SEMANTIC).read_text(encoding="utf-8").lower()
    assert "select " not in text and "create view" not in text  # no SQL text; code compiles it


# ---- the prose lint ---------------------------------------------------------------------------------------
def test_a_summary_with_a_number_the_result_does_not_hold_is_not_verified(tmp_path: Path) -> None:
    sql = "SELECT sum(Commitment_Amount) AS total FROM src_2"
    ok = run(tmp_path, sql, summary="Total committed is 1,325.75.")
    assert ok["summary_status"] == "verified" and ok["summary"].startswith("Total")
    bad = run(tmp_path, sql, summary="Total committed is 1,500.00, across 3 commitments.")
    assert bad["summary_status"] == "not verified" and "1500.00" in bad["unsupported_numbers"]
    assert bad["summary"] == "" and bad["rows"] == [["1325.75"]]  # the table stands alone


# ---- audit first, and nothing feeds a job ------------------------------------------------------------------
def test_started_is_written_before_the_query_and_finished_before_the_result(tmp_path: Path) -> None:
    audit = Sink()
    run(tmp_path, "SELECT 1 AS one", audit=audit)
    assert audit.actions() == ["ask.started", "ask.finished"]
    started = audit.rows[0]
    assert started[1]["sql_sha256"] and started[1]["table_sha256s"].keys() == {"src_1", "src_2"}
    assert audit.rows[1][2]["result_sha256"] and audit.rows[1][2]["status"] == "ok"


def test_a_failing_sink_means_the_query_never_runs(tmp_path: Path) -> None:
    runner = FakeRunner(local_harness)
    with pytest.raises(AuditError):
        run(tmp_path, "SELECT 1", audit=Sink("ask.started"), runner=runner)
    assert runner.calls == 0
    with pytest.raises(AuditError):  # and a result whose record cannot be written is not returned
        run(tmp_path, "SELECT 1", audit=Sink("ask.finished"))


def test_a_refusal_is_audited_and_runs_nothing(tmp_path: Path) -> None:
    audit, runner = Sink(), FakeRunner(local_harness)
    res = run(tmp_path, "DROP TABLE src_1", audit=audit, runner=runner)
    assert res["status"] == "refused" and runner.calls == 0
    assert audit.rows[0][0] == "ask.refused" and audit.rows[0][2]["reason"] == "not_select"


def test_without_a_sandbox_the_question_is_refused_and_audited(tmp_path: Path) -> None:
    audit = Sink()
    res = run(tmp_path, "SELECT 1", audit=audit, runner=None)
    assert res["status"] == "refused" and res["reason"] == "no_sandbox"
    assert audit.rows[0][2] == {"reason": "no_sandbox"}


def test_a_container_that_fails_returns_a_failure_not_a_result(tmp_path: Path) -> None:
    runner = FakeRunner(lambda *a: RunResult("oom", 137, None, "killed", 1.0, "x", "e"))
    audit = Sink()
    res = run(tmp_path, "SELECT 1", audit=audit, runner=runner)
    assert res["status"] == "failed" and res["reason"] == "oom"
    assert audit.rows[-1][2]["status"] == "oom" and audit.rows[-1][2]["result_sha256"] == ""


# ---- the Coworker tool -------------------------------------------------------------------------------------
def test_the_coworker_tool_answers_audits_and_changes_nothing(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.audit = AuditLog(tmp_path / "audit.jsonl")
    tools = {t.name: t for t in build_tools(ctx, sandbox=FakeRunner(local_harness, image="sha256:" + "6" * 64))}
    out = tools["ask_job"].invoke({"question": "how many tables?", "sql": "SELECT 1 AS one",
                                   "why": "the human asked", "summary": "There is 1 row."})
    body = json.loads(out)
    assert body["status"] == "ok" and body["rows"] == [["1"]] and body["summary_status"] == "verified"
    actions = [e["action"] for e in ctx.audit.read()]
    assert "ask.started" in actions and "ask.finished" in actions and actions[-1] == "ask_job"
    assert ctx.stage.stage_ids() == []
    written = [p for p in Path(ctx.out_dir).rglob("*") if p.is_file() and "flex_blobs" not in p.parts]
    assert [p.name for p in written if "Upload" in p.name] == []  # no upload came of it
    refused = tools["ask_job"].invoke({"question": "x", "sql": "COPY src_1 TO 'x'", "why": "try"})
    assert json.loads(refused)["status"] == "refused"


# ---- the container (needs Docker) --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def docker_runner() -> Any:
    from worker.flex.runner import DockerRunner, resolve_image

    digest = resolve_image("ow-sandbox:1")
    if digest is None:
        pytest.skip("Docker or the ow-sandbox image is not available")
    return DockerRunner(digest, start_grace=150.0)


@pytest.mark.sandbox
def test_the_real_container_answers_and_cannot_read_files(docker_runner: Any, tmp_path: Path) -> None:
    res = run(tmp_path, "SELECT total_commitment FROM v_total_commitment", runner=docker_runner)
    assert res["status"] == "ok" and res["rows"] == [["1325.75"]]
    # past the host check on purpose: the container must still refuse to read a file or switch the setting back
    files, _manifest = A.table_inputs(TABLES)
    files["query.sql"] = b"SELECT * FROM read_csv('/etc/hostname')"
    files["views.sql"] = b""
    r = docker_runner.run("query", b"#\n", files, 0, "askescape0001")
    assert r.status == "exit" and r.output is None
    files["query.sql"] = b"SELECT current_setting('enable_external_access')"
    r = docker_runner.run("query", b"#\n", files, 0, "askescape0002")
    assert r.status == "ok" and json.loads(r.output)["rows"][0][0].lower() == "false"
