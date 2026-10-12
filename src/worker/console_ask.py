"""The job screen's "Ask the job" tab: read-only questions over one job's rows (ticket 11, tier 3).

The tab shows what a question can use (the job's tables, the semantic layer's names and glossary), takes one SELECT,
and shows the answer beside the SQL. The tables are Parquet files DuckDB makes from the evaluation's spool once per
evaluation (`job_tables`): the rows, what was written, the findings, the tie-out and what is missing. The query runs in the sandbox (`worker.flex.ask`); nothing it returns is
written to the job. Every question is audited before it runs, on the sponsor's flex stream under the state folder.
"""

from __future__ import annotations

import dataclasses
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from worker import console_engine as CE
from worker import persist
from worker import record_facts as RF
from worker.console_reshape import blobs_of
from worker.flex import ask as A
from worker.flex.runner import runner_from_env

_OUTCOMES = {
    "static": "SELECT o.i AS row, json_extract_string(o.detail, '$.st') AS status, "
              "json_extract_string(o.detail, '$.id') AS source_id, coalesce(n.new_id, '') AS new_id, "
              "json_extract_string(o.detail, '$.nm') AS name, o.why AS why "
              "FROM read_json(?, format='nd', columns={i: 'BIGINT', why: 'VARCHAR', detail: 'JSON'}) o "
              "LEFT JOIN new_ids n ON n.row = o.i",
    "gl": "SELECT json_extract_string(o.detail, '$.l.j') AS journal, json_extract_string(o.detail, '$.l.ln') "
          "AS line, json_extract_string(o.detail, '$.l.acct') AS account, json_extract_string(o.detail, '$.l.an') "
          "AS account_name, json_extract_string(o.detail, '$.l.tt') AS type, "
          "json_extract_string(o.detail, '$.l.dr') AS debit, json_extract_string(o.detail, '$.l.cr') AS credit, "
          "o.status AS status FROM read_json(?, format='nd', columns={status: 'VARCHAR', "
          "detail: 'JSON'}) o WHERE NOT coalesce(try_cast(json_extract_string(o.detail, '$.off') AS BOOLEAN), false)",
}


def _parquet(sql: str, args: list[Any], out: Path, **tables: Any) -> None:
    """`sql` over a JSON-lines file (or Arrow tables registered by name), written whole as one Parquet file."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    con = duckdb.connect(":memory:")
    for name, table in tables.items():
        con.register(name, table)
    con.execute(f"COPY ({sql}) TO '{tmp.as_posix()}' (FORMAT parquet)", args)
    os.replace(tmp, out)


def _spool(ev: Any, name: str, kind: str, d: Path) -> None:
    """The evaluation's outcome spool (one JSON object per line) as `name.parquet`, selected by DuckDB."""
    raw = ev._store.get(ev._outcome_key) or b""
    src = d / "outcomes.jsonl"
    src.write_bytes(raw)
    ids = {int(k): v for k, v in (ev.facts.get("ids") or {}).items()}
    new_ids = pa.table({"row": pa.array(list(ids), pa.int64()), "new_id": pa.array(list(ids.values()), pa.string())})
    _parquet(_OUTCOMES[kind], [src.as_posix()], d / f"{name}.parquet", new_ids=new_ids)


def job_tables(c: Any, j: dict[str, Any]) -> dict[str, Path]:
    """The job's tables as Parquet files, built once per evaluation under `<state>/flex/ask-cache/<eval key>/`:
    `job_rows` (static) or `job_lines` (GL), `out_<Type>` (the rows as written), `findings`, `tie_out` and
    `missing`. Empty before a run. Nothing is copied row by row: DuckDB reads the engine's spool."""
    if not j.get("runs"):
        return {}
    ev = CE.evaluation(c, j)
    key = ev.manifest.eval_key
    gl = RF.is_gl(j["type"])
    out_name = "out_" + re.sub(r"\W+", "_", str(j["type"]))
    d = Path(c.root) / "flex" / "ask-cache" / key[:2] / key
    names = ["job_lines" if gl else "job_rows", out_name, "findings", "tie_out", "missing"]
    if not all((d / f"{n}.parquet").is_file() for n in names):
        with tempfile.TemporaryDirectory() as work:
            w = Path(work)
            _spool(ev, names[0], "gl" if gl else "static", w)
            table = b"".join(ev._store.get(k) or b"" for k in ev._table_keys)
            (w / "table.jsonl").write_bytes(table)
            if table:
                _parquet("SELECT COLUMNS(*)::VARCHAR FROM read_json(?, format='nd', sample_size=-1)",
                         [(w / "table.jsonl").as_posix()], w / f"{out_name}.parquet")
            else:
                pq.write_table(pa.table({"empty": pa.array([], pa.string())}), w / f"{out_name}.parquet")
            _parquet("SELECT * FROM f", [], w / "findings.parquet", f=pa.table({
                "id": [f.id for f in ev.findings], "rule": [f.rule.rule_id for f in ev.findings],
                "severity": [f.severity for f in ev.findings], "field": [f.field for f in ev.findings],
                "message": [f.message for f in ev.findings], "count": [f.count for f in ev.findings],
                "where_in_file": [f.ranges_text for f in ev.findings]}))
            t = ev.tie_out
            _parquet("SELECT * FROM t", [], w / "tie_out.parquet", t=pa.table(
                {k: [None if v is None else str(v)] for k, v in (dataclasses.asdict(t) if t else {}).items()}
                or {"rows_read": pa.array([], pa.string())}))
            _parquet("SELECT * FROM m", [], w / "missing.parquet", m=pa.table({
                "field": [m.field for m in ev.missing], "rows": [m.rows for m in ev.missing],
                "reason": [m.reason for m in ev.missing]}))
            d.mkdir(parents=True, exist_ok=True)
            for n in names:
                os.replace(w / f"{n}.parquet", d / f"{n}.parquet")
    return {n: d / f"{n}.parquet" for n in names}


def _shape(p: Path) -> dict[str, Any]:
    f = pq.ParquetFile(p)
    return {"rows": f.metadata.num_rows, "columns": list(f.schema_arrow.names)}


def _stream(c: Any, j: dict[str, Any]) -> Any:
    return persist.audit(Path(c.root) / "flex" / f"{j['sp']}-ask.audit.jsonl")


def ask_view(c: Any, j: dict[str, Any]) -> dict[str, Any]:
    tables = job_tables(c, j)
    sem = A.load_semantic()
    started = {e["inputs"]["query_id"]: e for e in _stream(c, j).read()
               if e["action"] == "ask.started" and e["inputs"].get("job") == j["id"]}
    done = {e["inputs"].get("query_id"): e["outputs"] for e in _stream(c, j).read() if e["action"] == "ask.finished"}
    history = [{"question": e["inputs"].get("question", ""), "sql": e["inputs"].get("sql", ""),
                "status": done.get(q, {}).get("status", "not finished"), "rows": done.get(q, {}).get("rows", 0)}
               for q, e in list(started.items())[-8:]]
    view: dict[str, Any] = {
        "kind": "ask", "title": "Ask the job",
        "sub": "One read-only SELECT over this job's rows, run in the sandbox. The answer is information: it never "
               "changes the job.",
        "tables": [{"name": n, **_shape(p)} for n, p in tables.items()],
        "terms": [t["term"] for t in sem["terms"]], "glossary": [{"term": k, "means": v}
                                                                for k, v in sem["glossary"].items()],
        "history": history, "sandbox": runner_from_env(blobs_of(c)) is not None}
    if not tables:
        view["empty"] = "No run yet. Run the process, then ask about what was read, written and left out."
    return view


def ask_act(c: Any, j: dict[str, Any], question: str, sql: str, who: str) -> dict[str, Any]:
    tables = job_tables(c, j)
    out = next((n for n in tables if n.startswith("out_")), "")
    return A.ask(runner=runner_from_env(blobs_of(c)), audit=_stream(c, j), blobs=blobs_of(c), tables=tables,
                 aliases={f"t_{j['type']}": out} if out else None,  # the semantic views read `t_<Type>`
                 question=question, sql=sql, why=f"asked by {who or 'local'} on the job screen",
                 subject={"sponsor": j["sp"], "job": j["id"], "by": who})
