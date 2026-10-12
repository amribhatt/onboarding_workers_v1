"""Ask the job (tier 3): one read-only DuckDB SELECT over this job's tables, run in the sandbox.

* The statement is checked on the host first (one statement, a SELECT, no file or catalog functions), then run in
  the container with external access off and the configuration locked. Nothing it reads is anything but this job's
  tables and the views compiled from `semantic.yaml`; nothing it does can write.
* The tool's own prose passes the numeric-claim lint against the result rows only. A summary with a number the
  result does not hold is shown as "not verified"; the result table stands alone.
* A result is information. It never becomes a Choice, a supply value, a recipe or an input of any job: this
  module returns text and a blob reference, and nothing in the engine reads either.
* `ask.started` is recorded before the query runs and `ask.finished` before its result is shown. If the record
  cannot be written the query does not run (or its result is not shown).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import tempfile
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import duckdb
import yaml

from worker.flex.grid import canonical_json, sha256_bytes
from worker.flex.runner import Runner
from worker.replay import BlobStore
from worker.roles import unsupported_numbers

SEMANTIC = Path(__file__).with_name("semantic.yaml")
MAX_SQL = 8000
MAX_TABLE_ROWS = 250_000
SHOWN_ROWS = 200
_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,59}$")
_FILE_LITERAL = re.compile(r"\b(from|join)\s*\(*\s*'", re.IGNORECASE)  # FROM 'file.csv': a replacement scan
_PATH_IDENT = re.compile(r'\b(from|join)\s+"[^"]*[/\\.][^"]*"', re.IGNORECASE)  # FROM "dir/file.csv"
_HAZARD = re.compile(
    r"\b(read_\w+|\w+_scan|glob|sniff_csv|duckdb_\w+|pragma_\w+|query|query_table|getenv|current_setting|"
    r"getvariable|copy|attach|detach|install|load|export|import|pragma|call|set|reset|create|insert|update|delete|"
    r"drop|alter|vacuum|checkpoint|summarize|describe|show|explain|http\w*|s3\w*)\b", re.IGNORECASE)
_STRING = re.compile(r"'(?:[^']|'')*'")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_ID_NAME = r"(^|_)(id|key|code|number|no|ref)$"


class AskRefused(Exception):
    def __init__(self, reason: str, sentence: str) -> None:
        super().__init__(sentence)
        self.reason, self.sentence = reason, sentence


def check_select(sql: str) -> str:
    """The statement, if it is one read-only SELECT. Otherwise `AskRefused` with a plain reason."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise AskRefused("empty", "The query is empty.")
    if len(text) > MAX_SQL:
        raise AskRefused("too_long", f"The query is over {MAX_SQL} characters.")
    if _FILE_LITERAL.search(text) or _PATH_IDENT.search(text):
        raise AskRefused("file_access", "Reading a file or path is not allowed; use the job's tables.")
    try:
        stmts = duckdb.extract_statements(text)
    except duckdb.Error as exc:
        raise AskRefused("syntax", f"The query does not parse: {str(exc)[:160]}") from exc
    if len(stmts) != 1 or stmts[0].type != duckdb.StatementType.SELECT:
        raise AskRefused("not_select", "Exactly one SELECT statement is allowed.")
    bare = _STRING.sub("''", re.sub(r"--[^\n]*|/\*.*?\*/", " ", text, flags=re.DOTALL))
    hit = _HAZARD.search(bare)
    if hit:
        raise AskRefused("forbidden", f"`{hit.group(0)}` is not allowed in a question about the job.")
    return text


# ---- tables -----------------------------------------------------------------------------------------------
def _col(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", str(name)).strip("_") or "col"


def _sql_type(name: str, values: Sequence[str]) -> str:
    """DECIMAL(38,10) only for a column of plain numbers that is not an identifier; everything else stays text."""
    vals = [v for v in values if v != ""]
    if not vals or re.search(_ID_NAME, name.lower()):
        return "VARCHAR"
    if all(_NUMBER.match(v) and not (len(v.split(".")[0].lstrip("-")) > 1 and v.lstrip("-").startswith("0"))
           for v in vals):
        return "DECIMAL(38,10)"
    return "VARCHAR"


_NUMERIC = r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$"  # plain numbers; a leading zero is an identifier, not an amount


def _parquet_table(name: str, path: Path) -> tuple[bytes, dict[str, Any]]:
    """One Parquet file as the container's CSV and its manifest entry, by DuckDB on the host: no row passes
    through Python, and a column is a DECIMAL only when every value is a plain number."""
    con = duckdb.connect(":memory:")
    src = "read_parquet('" + path.as_posix().replace("'", "''") + "')"  # a host-built path
    have = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()]
    n = int(con.execute(f"SELECT count(*) FROM {src}").fetchone()[0])  # type: ignore[index]
    if n > MAX_TABLE_ROWS:
        raise AskRefused("too_large", f"{name} has {n} rows; over the {MAX_TABLE_ROWS} a question reads.")
    safe: list[str] = []
    for c in have:
        s = _col(c)
        while s in safe:
            s += "_"
        safe.append(s)
    sel = ", ".join(f'CAST("{c}" AS VARCHAR) AS "{s}"' for c, s in zip(have, safe, strict=True))
    con.execute(f"CREATE TEMP VIEW t AS SELECT {sel} FROM {src}")
    numeric = [False] * len(safe)
    if n:
        agg = ", ".join(
            f"""bool_and(nullif("{c}", '') IS NULL OR regexp_full_match("{c}", '{_NUMERIC}')) """
            f"""AND bool_or(nullif("{c}", '') IS NOT NULL)""" for c in safe)
        numeric = [bool(v) for v in con.execute(f"SELECT {agg} FROM t").fetchone()]  # type: ignore[union-attr]
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "t.csv"
        con.execute(f"COPY (SELECT * FROM t) TO '{out.as_posix()}' (FORMAT csv, HEADER true)")
        data = out.read_bytes()
    cols = [[s, "DECIMAL(38,10)" if numeric[i] and not re.search(_ID_NAME, s.lower()) else "VARCHAR"]
            for i, s in enumerate(safe)]
    return data, {"name": name, "file": f"{name.lower()}.csv", "rows": n, "columns": cols}



def table_inputs(tables: Mapping[str, Sequence[Mapping[str, Any]] | Path]
                 ) -> tuple[dict[str, bytes], list[dict[str, Any]]]:
    """The container's input files: one CSV per table, and `tables.json` naming them with their column types. A
    table is a list of row dicts (a staged table) or the path of a Parquet file (the job's cache)."""
    files: dict[str, bytes] = {}
    manifest: list[dict[str, Any]] = []
    for name, rows in tables.items():
        if not _IDENT.match(name):
            raise AskRefused("bad_table", f"table name {name!r} is not an identifier")
        if isinstance(rows, Path):
            data, entry = _parquet_table(name, rows)
            files[entry["file"]] = data
            manifest.append(entry)
            continue
        if len(rows) > MAX_TABLE_ROWS:
            raise AskRefused("too_large", f"{name} has {len(rows)} rows; over the {MAX_TABLE_ROWS} a question reads.")
        cols = list(dict.fromkeys(k for r in rows for k in r))
        safe: list[str] = []
        for c in cols:
            s = _col(c)
            while s in safe:
                s += "_"
            safe.append(s)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(safe)
        text_rows = [["" if r.get(c) is None else str(r.get(c)) for c in cols] for r in rows]
        w.writerows(text_rows)
        by_col = list(zip(*text_rows, strict=True)) if text_rows else [() for _ in cols]
        fname = f"{name.lower()}.csv"
        files[fname] = buf.getvalue().encode("utf-8")
        manifest.append({"name": name, "file": fname, "rows": len(rows),
                         "columns": [[s, _sql_type(s, by_col[i])] for i, s in enumerate(safe)]})
    files["tables.json"] = json.dumps(manifest).encode("utf-8")
    return files, manifest


# ---- the semantic layer -----------------------------------------------------------------------------------
def load_semantic(path: str | Path | None = None) -> dict[str, Any]:
    doc = yaml.safe_load(Path(path or SEMANTIC).read_text(encoding="utf-8")) or {}
    for key in ("terms", "relationships", "glossary"):
        doc.setdefault(key, [] if key != "glossary" else {})
    return doc


def _ref(text: str) -> tuple[str, str]:
    t, _, c = str(text).partition(".")
    if not (_IDENT.match(t) and _IDENT.match(c)):
        raise ValueError(f"{text!r} is not Table.column")
    return t, _col(c)


def compile_views(semantic: Mapping[str, Any], manifest: Sequence[Mapping[str, Any]]) -> tuple[str, list[str]]:
    """The semantic file as `CREATE VIEW` statements over `t_<Template>` (and the names the job really has).
    Returns the statements and the names of the views made; terms whose table is not in this job are skipped."""
    have = {m["name"]: [c[0] for c in m["columns"]] for m in manifest}
    stmts: list[str] = []
    made: list[str] = []
    aggs = {"sum": "sum", "count": "count", "count_distinct": "count(distinct", "min": "min", "max": "max"}
    for t in semantic.get("terms", []):
        tbl, col = _ref(t["means"])
        src = f"t_{tbl}"
        if src not in have or col not in have[src] or not _IDENT.match(str(t["term"])):
            continue
        name = f"v_{t['term']}"
        if t.get("kind") == "measure" and t.get("agg") in aggs:
            agg = t["agg"]
            expr = (f"count(distinct \"{col}\")" if agg == "count_distinct" else
                    "count(*)" if agg == "count" else f"{aggs[agg]}(try_cast(\"{col}\" AS DECIMAL(38,10)))")
            stmts.append(f'CREATE VIEW {name} AS SELECT {expr} AS "{t["term"]}" FROM {src}')
        else:
            stmts.append(f'CREATE VIEW {name} AS SELECT DISTINCT "{col}" AS "{t["term"]}" FROM {src}')
        made.append(name)
    for r in semantic.get("relationships", []):
        (lt, lc), (rt, rc) = _ref(r["left"]), _ref(r["right"])
        ls, rs = f"t_{lt}", f"t_{rt}"
        if ls not in have or rs not in have or lc not in have[ls] or rc not in have[rs]:
            continue
        sel = ", ".join([f'l."{c}" AS "{lt}.{c}"' for c in have[ls]] + [f'r."{c}" AS "{rt}.{c}"' for c in have[rs]])
        name = f"v_{lt}__{rt}"
        stmts.append(f'CREATE VIEW {name} AS SELECT {sel} FROM {ls} l JOIN {rs} r ON l."{lc}" = r."{rc}"')
        made.append(name)
    return ";\n".join(stmts), made


# ---- the question -----------------------------------------------------------------------------------------
def lint_summary(summary: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    """Numbers in `summary` that the result rows do not hold (empty: the summary is clean). Nothing else is a fact."""
    return unsupported_numbers([summary], {"columns": list(columns), "rows": [list(r) for r in rows],
                                           "row_count": len(rows)})


def stage_tables(stage: Any) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """This job's staged tables as `src_<n>`, and `t_<Template>` for the latest of each record type."""
    tables: dict[str, list[dict[str, Any]]] = {}
    aliases: dict[str, str] = {}
    for n, sid in enumerate(stage.stage_ids(), start=1):
        tables[f"src_{n}"] = stage.rows(sid)
        aliases[f"t_{stage.meta(sid)['template']}"] = f"src_{n}"
    return tables, aliases


def _decimal(v: Any) -> Any:
    try:
        return str(Decimal(str(v)).normalize()) if _NUMBER.match(str(v)) else v
    except InvalidOperation:
        return v


def ask(*, runner: Runner | None, audit: Any, blobs: BlobStore, tables: Mapping[str, Sequence[Mapping[str, Any]] | Path],
        question: str, sql: str, why: str, summary: str = "", semantic: Mapping[str, Any] | None = None,
        aliases: Mapping[str, str] | None = None, subject: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One question. `aliases` names a table a second time (`t_Investors` for `src_2`) as a view. Returns
    {status: ok|refused|failed, ...}; raises only `AuditError`."""
    subj = dict(subject or {})
    qtext = str(question).strip()[:300]
    if runner is None:
        audit.record("ask.refused", why, {**subj, "question": qtext}, {"reason": "no_sandbox"})
        return {"status": "refused", "reason": "no_sandbox",
                "message": "Questions need the sandbox, which is not running."}
    try:
        stmt = check_select(sql)
        files, manifest = table_inputs(tables)
    except AskRefused as exc:
        audit.record("ask.refused", why, {**subj, "question": qtext, "sql_sha256": sha256_bytes(sql.encode())},
                     {"reason": exc.reason, "message": exc.sentence})
        return {"status": "refused", "reason": exc.reason, "message": exc.sentence}
    named = [m for m in manifest]
    cols_of = {m["name"]: m["columns"] for m in manifest}
    alias_sql = [f'CREATE VIEW {a} AS SELECT * FROM {t}' for a, t in (aliases or {}).items()
                 if t in cols_of and _IDENT.match(a)]
    named += [{"name": a, "columns": cols_of[t]} for a, t in (aliases or {}).items() if t in cols_of]
    views, made = compile_views(semantic if semantic is not None else load_semantic(), named)
    files["query.sql"] = stmt.encode("utf-8")
    files["views.sql"] = ";\n".join([*alias_sql, views] if views else alias_sql).encode("utf-8")
    qid = hashlib.sha256(canonical_json({"sql": stmt, "tables": {m["name"]: m["rows"] for m in manifest},
                                         "subject": subj})).hexdigest()[:16]
    table_shas = {m["name"]: sha256_bytes(files[m["file"]]) for m in manifest}
    base = {**subj, "query_id": qid, "image_digest": runner.image, "harness": "query-harness/1"}
    audit.record("ask.started", why, {**base, "question": qtext, "sql": stmt[:2000], "sql_sha256": sha256_bytes(
        stmt.encode()), "table_sha256s": table_shas}, {"views": made})
    r = runner.run("query", b"# query harness: the SQL is /in/query.sql\n", files, 0, qid + "-0")
    result: dict[str, Any] | None = None
    why_not = r.status
    if r.status == "ok" and r.output is not None:
        try:
            doc = json.loads(r.output.decode("utf-8"))
            if (isinstance(doc, dict) and set(doc) == {"columns", "rows"} and isinstance(doc["columns"], list)
                    and isinstance(doc["rows"], list) and len(doc["rows"]) <= 10_001):
                result = {"columns": [str(c) for c in doc["columns"]],
                          "rows": [[None if v is None else _decimal(v) for v in row] for row in doc["rows"]]}
        except ValueError:
            pass
        if result is None:
            why_not = "bad_output"
    rows = result["rows"] if result else []
    bad = lint_summary(summary, result["columns"], rows) if (result and summary.strip()) else []
    result_sha = blobs.put(canonical_json(result)) if result else ""
    audit.record("ask.finished", why, {**base}, {
        "status": "ok" if result else why_not, "result_sha256": result_sha, "rows": len(rows),
        "summary": "none" if not summary.strip() else "verified" if not bad else "unverified",
        "stderr_sha256": sha256_bytes(r.stderr.encode())})
    if result is None:
        return {"status": "failed", "reason": why_not, "message": f"The query did not complete ({why_not}): "
                f"{r.stderr[:200]}", "query_id": qid}
    return {"status": "ok", "query_id": qid, "columns": result["columns"], "rows": rows[:SHOWN_ROWS],
            "row_count": len(rows), "truncated": len(rows) > SHOWN_ROWS or len(rows) > 10_000,
            "result_sha256": result_sha, "views": made,
            "summary_status": "none" if not summary.strip() else "verified" if not bad else "not verified",
            "unsupported_numbers": bad, "summary": summary if (summary.strip() and not bad) else ""}
