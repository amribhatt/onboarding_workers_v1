"""Query harness, version 1. Runs INSIDE the container as `python -B -s /harness/query.py`.

/in/tables.json lists the job's tables as [{"name", "file", "columns": [[name, sql_type]]}], each file a CSV beside
it. /in/views.sql (optional) holds the semantic layer compiled by the host. /in/query.sql is one SELECT. External
access is switched off and the configuration locked before the query runs; /out/result.json is the answer: the
column names and at most LIMIT rows, as text.
"""
import json
import sys

import duckdb

LIMIT = 10001


def main():
    with open("/in/tables.json", encoding="utf-8") as fh:
        tables = json.load(fh)
    with open("/in/query.sql", encoding="utf-8") as fh:
        sql = fh.read().strip().rstrip(";")
    con = duckdb.connect(":memory:")
    for t in tables:
        spec = "{" + ", ".join(f"'{n}': '{ty}'" for n, ty in t["columns"]) + "}"
        con.execute(f'CREATE TABLE "{t["name"]}" AS SELECT * FROM read_csv(?, header=true, columns={spec})',
                    [f"/in/{t['file']}"])
    try:
        with open("/in/views.sql", encoding="utf-8") as fh:
            for stmt in fh.read().split(";\n"):
                if stmt.strip():
                    con.execute(stmt)
    except FileNotFoundError:
        pass
    con.execute("SET enable_external_access=false")
    con.execute("SET lock_configuration=true")
    cur = con.execute(f"SELECT * FROM ({sql}) AS q LIMIT {LIMIT}")
    rows = cur.fetchall()
    out = {"columns": [d[0] for d in cur.description],
           "rows": [[None if v is None else str(v) for v in r] for r in rows]}
    with open("/out/result.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))


if __name__ == "__main__":
    sys.exit(main())
