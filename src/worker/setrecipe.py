"""Recipes over whole columns: the distinct cells a field reads are evaluated once and joined back to the rows.

`transforms.apply_field` is the definition of a recipe: one row in, one outcome out. A column of a million rows
rarely holds a million different values, so this module tags the cells a field reads (`s` text, `d` decimal, `i`
int, `f` float, `D` date, `T` datetime, `b` bool; blank is NULL, so a typed value and its exact `Decimal` text
survive), takes the distinct tuples in one query, evaluates each with the reference, and gathers the outcomes back
to the rows in order. The result is the reference outcome by construction (`tests/test_setrecipe.py` proves the
gather); the cost is one reference call per distinct tuple, not per row.
"""

from __future__ import annotations

import os
import pickle
from collections.abc import Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from worker import transforms as T
from worker.setops import database

_RESULT = ("status", "kind", "payload", "code", "fb", "df", "tr", "txt")
_FLAGS = (("fb", "fallback"), ("df", "defaulted"), ("tr", "truncated"))
_SCHEMA = pa.schema([pa.field(c, pa.bool_() if c in ("fb", "df", "tr") else pa.string()) for c in _RESULT])
_TAG_OF = {str: "s", Decimal: "d", int: "i", float: "f", datetime: "T", date: "D"}
PARALLEL_TUPLES = 20_000  # more distinct tuples than this are evaluated in several processes
PROCESSES = 4


def _tag_cell(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "b1" if v else "b0"
    t = _TAG_OF.get(type(v))
    if t is None:
        return "o" + str(v)
    return t + (v.isoformat() if t in "TD" else repr(v) if t == "f" else str(v))


_UNTAG = {"s": str, "d": Decimal, "i": int, "f": float, "b": lambda r: r == "1", "T": datetime.fromisoformat,
          "D": date.fromisoformat}


def _untag_cell(s: str | None) -> Any:
    return None if s is None else _UNTAG.get(s[0], str)(s[1:])


def _tag_sql(col: str, typ: pa.DataType) -> str:
    """The tagged text of an Arrow column, in SQL (blank stays NULL)."""
    text = f"CAST({col} AS VARCHAR)"
    if pa.types.is_string(typ) or pa.types.is_large_string(typ):
        return f"'s' || {col}"
    if pa.types.is_boolean(typ):
        return f"(CASE WHEN {col} THEN 'b1' ELSE 'b0' END)"
    if pa.types.is_floating(typ):  # repr keeps the point: 1500.0, not 1500
        return (f"'f' || (CASE WHEN contains({text}, '.') OR contains({text}, 'e') OR contains({text}, 'n') "
                f"THEN {text} ELSE {text} || '.0' END)")
    tag = "i" if pa.types.is_integer(typ) else "d" if pa.types.is_decimal(typ) else \
        "D" if pa.types.is_date(typ) else "T" if pa.types.is_timestamp(typ) else "o"
    return f"'{tag}' || {text}"


def _payload(v: Any) -> tuple[str | None, str | None]:
    if v is None:
        return None, None
    if isinstance(v, bool):
        return "b", "1" if v else "0"
    if isinstance(v, Decimal):
        return "n", str(v)
    if isinstance(v, datetime):
        return "T", v.isoformat()
    if isinstance(v, date):
        return "D", v.isoformat()
    return "s", str(v)


def _value_of(kind: str | None, payload: str | None) -> Any:
    if kind is None or payload is None:
        return None
    return {"n": Decimal, "D": date.fromisoformat, "T": datetime.fromisoformat,
            "b": lambda p: p == "1"}.get(kind, str)(payload)


@dataclass
class FieldResult:
    """One field over `n` rows: an Arrow table (`status` ok|missing|failed, `kind`, `payload`, `code`, `fb`, `df`,
    `tr`, `txt` = the text the value is written as) in row order, and the raw source column for display."""

    table: pa.Table
    source: Sequence[Any] | pa.Array | pa.ChunkedArray | None = None

    def __len__(self) -> int:
        return self.table.num_rows

    @property
    def status(self) -> list[str]:
        return self.table["status"].to_pylist()

    @property
    def code(self) -> list[str | None]:
        return self.table["code"].to_pylist()

    @property
    def value(self) -> list[Any]:
        """The typed values (None unless ok), as `transforms.apply_field` gives them."""
        t = self.table
        return [_value_of(k, p) if s == "ok" else None
                for s, k, p in zip(t["status"].to_pylist(), t["kind"].to_pylist(), t["payload"].to_pylist(),
                                   strict=True)]

    @property
    def flags(self) -> list[frozenset[str]]:
        cols = [self.table[c].to_pylist() for c, _n in _FLAGS]
        return [frozenset(name for (_c, name), on in zip(_FLAGS, row, strict=True) if on) for row in zip(*cols)]

    def source_cell(self, i: int) -> Any:
        if self.source is None:
            return None
        got = self.source[i]
        return got.as_py() if hasattr(got, "as_py") else got


def _operands(cf: T.CompiledField, cols: Mapping[str, list[int]]) -> list[int]:
    """The header positions a field reads: its source column and every column an op names."""
    names = [cf.source["from"]["header"]] if "from" in cf.source else []
    names += [c["col"] for nop in cf.ops for c in T._cols_in(nop) if "col" in c]
    return sorted({i for n in names for i in cols.get(" ".join(n.casefold().split()), [])})


def _outcome(cf: T.CompiledField, headers: tuple[str, ...], ctx: T.Context, cols: Mapping[str, list[int]],
             row: Sequence[Any]) -> tuple[Any, ...]:
    """The reference outcome of one row, as the `_RESULT` values."""
    o = T.apply_field(cf, T.Row("", headers, tuple(row)), ctx, cols)  # the reference reads `cols`, never writes it
    kind, payload = _payload(o.value) if o.status == "ok" else (None, None)
    return (o.status, kind, payload, o.code, "fallback" in o.flags, "defaulted" in o.flags, "truncated" in o.flags,
            T._text(o.value) if o.status == "ok" else None)


def _reference_rows(cf: T.CompiledField, headers: tuple[str, ...], idx: Sequence[int], ctx: T.Context,
                    cols: Mapping[str, list[int]], tuples: Sequence[Sequence[str | None]]) -> list[tuple[Any, ...]]:
    """The reference outcome of each tagged tuple of the cells at header positions `idx` (the rest are blank)."""
    out = []
    for cells in tuples:
        row: list[Any] = [None] * len(headers)
        for i, c in zip(idx, cells, strict=True):
            row[i] = _untag_cell(c)
        out.append(_outcome(cf, headers, ctx, cols, row))
    return out


def _evaluate(cf: T.CompiledField, headers: tuple[str, ...], idx: Sequence[int], ctx: T.Context,
              cols: Mapping[str, list[int]], tuples: list[tuple[str | None, ...]]) -> list[tuple[Any, ...]]:
    """`_reference_rows`, in several processes when there are many tuples (the work is pure Python, so threads
    would not help); a field that cannot be sent to another process is evaluated here."""
    workers = min(PROCESSES, os.cpu_count() or 1)
    if len(tuples) < PARALLEL_TUPLES or workers < 2:
        return _reference_rows(cf, headers, idx, ctx, cols, tuples)
    size = -(-len(tuples) // workers)
    try:
        with ProcessPoolExecutor(max_workers=workers, max_tasks_per_child=1) as pool:
            parts = pool.map(_reference_rows, *zip(*[(cf, headers, idx, ctx, cols, tuples[k:k + size])
                                                     for k in range(0, len(tuples), size)], strict=True))
            return [r for part in parts for r in part]
    except (BrokenProcessPool, OSError, pickle.PicklingError, TypeError, AttributeError):
        return _reference_rows(cf, headers, idx, ctx, cols, tuples)


def _table(columns: Mapping[str, list[Any]]) -> pa.Table:
    return pa.table({c: pa.array(columns.get(c, []), type=_SCHEMA.field(c).type) for c in _RESULT})


def apply_field(cf: T.CompiledField, headers: Sequence[str], columns: Sequence[Any],
                ctx: T.Context | None = None, rows: int | None = None) -> FieldResult:
    """One compiled field over a batch given as one column of cells per header (a list, or an Arrow array of
    text, numbers or dates; all the same length): what `transforms.apply_field` gives on each row, in row order."""
    context = ctx or T._EMPTY_CONTEXT
    hdr = tuple(str(h) for h in headers)
    n = rows if rows is not None else (len(columns[0]) if columns else 0)
    cols = T._columns(hdr)
    idx = _operands(cf, cols)
    source = None
    if "from" in cf.source:
        hit = cols.get(" ".join(cf.source["from"]["header"].casefold().split()), [])
        source = columns[hit[0]] if len(hit) == 1 else None
    if n == 0 or not idx:  # nothing is read: the outcome is the same for every row
        one = _outcome(cf, hdr, context, cols, [None] * len(hdr))
        return FieldResult(_table({c: [one[k]] * n for k, c in enumerate(_RESULT)}), source)
    names = [f"o{j}" for j in range(len(idx))]
    raw: dict[str, Any] = {"_i": pa.array(range(n), type=pa.int64())}
    tagged: list[str] = []
    for o, i in zip(names, idx, strict=True):
        col = columns[i]
        if isinstance(col, pa.ChunkedArray | pa.Array):
            raw[f"r_{o}"] = col
            tagged.append(f"{_tag_sql(f'r_{o}', col.type)} AS {o}")
        else:
            raw[o] = pa.array([_tag_cell(v) for v in col], type=pa.string())
            tagged.append(o)
    con = database().cursor()
    try:
        con.register("raw_t", pa.table(raw))
        con.execute(f"CREATE TEMP TABLE rows_t AS SELECT {', '.join(tagged)}, _i FROM raw_t")
        con.execute(f"CREATE TEMP TABLE d AS SELECT row_number() OVER () AS _d, * FROM "
                    f"(SELECT DISTINCT {', '.join(names)} FROM rows_t)")
        distinct = con.execute("SELECT * FROM d").fetchall()
        got = _evaluate(cf, hdr, idx, context, cols, [tuple(r[1:]) for r in distinct])  # once per distinct tuple
        out = {c: [r[k] for r in got] for k, c in enumerate(_RESULT)}
        con.register("ref", _table(out).append_column("_d", pa.array([r[0] for r in distinct], type=pa.int64())))
        on = " AND ".join(f"COALESCE(t.{o}, '') = COALESCE(d.{o}, '')" for o in names)
        joined = con.execute(
            f"SELECT {', '.join('r.' + c for c in _RESULT)} FROM rows_t t JOIN d ON {on} "
            "JOIN ref r ON r._d = d._d ORDER BY t._i").to_arrow_table()
    finally:
        con.close()
    return FieldResult(joined.cast(_SCHEMA), source)


def apply_recipe(compiled: T.CompiledRecipe, headers: Sequence[str], columns: Sequence[Any],
                 ctx: T.Context | None = None, rows: int | None = None) -> dict[str, FieldResult]:
    """Every field of a compiled recipe over one batch (`rows` says how many when there are no columns)."""
    return {name: apply_field(cf, headers, columns, ctx, rows) for name, cf in compiled.fields.items()}


# ---- how many rows conform ----------------------------------------------------------------------------------------
def _runs(rows: Iterable[T.Row]) -> Iterator[tuple[tuple[str, ...], list[T.Row]]]:
    """Consecutive rows that share a header layout."""
    run: list[T.Row] = []
    headers: tuple[str, ...] | None = None
    for r in rows:
        if headers is not None and r.headers != headers:
            yield headers, run
            run = []
        headers = r.headers
        run.append(r)
    if headers is not None:
        yield headers, run


def verdict_of(cf: T.CompiledField, result: FieldResult, srcs: Sequence[str]) -> T.Verdict:
    """How many rows conform to the target rule, MISSING, FAILED by code, flagged counts and up to five examples,
    as `transforms.verify` counts them."""
    n = len(result)
    v = T.Verdict(cf.name, cf.field_hash)
    v.total = n
    if n == 0:
        return v
    rule = cf.rule
    lens = pc.utf8_length(pc.fill_null(result.table["txt"], "")).cast(pa.int64())
    con = database().cursor()
    try:
        con.register("res", result.table.append_column("i", pa.array(range(n), type=pa.int64()))
                     .append_column("len", lens))
        con.register("ref", pa.table({"s": pa.array(sorted(rule.reference or ()), type=pa.string())}))
        limit = f"len > {int(rule.max_len)}" if rule.max_len is not None else "false"
        too_long = "'id_too_long'" if rule.is_id else "'too_long'"
        in_ref = "txt NOT IN (SELECT s FROM ref)" if rule.reference is not None else "false"
        required = "'required_blank'" if rule.required else "NULL"
        con.execute(
            "CREATE TEMP TABLE r0 AS SELECT i, status, code, txt, fb, df, tr, CASE "
            f"WHEN status = 'failed' THEN code WHEN status = 'missing' THEN {required} "
            f"WHEN {limit} THEN {too_long} WHEN {in_ref} THEN 'not_in_reference' END AS reason FROM res")
        dup = ("SELECT i, row_number() OVER (PARTITION BY txt ORDER BY i) AS rn FROM r0 "
               "WHERE status = 'ok' AND reason IS NULL") if rule.is_key else "SELECT NULL::BIGINT AS i, 0 AS rn"
        con.execute("CREATE TEMP TABLE r1 AS SELECT r0.i, CASE WHEN r0.reason IS NOT NULL THEN r0.reason "
                    f"WHEN d.rn > 1 THEN 'duplicate_key' END AS reason FROM r0 LEFT JOIN ({dup}) d ON d.i = r0.i")
        v.conform = int(con.execute("SELECT count(*) FROM r1 WHERE reason IS NULL").fetchone()[0])
        v.missing = int(con.execute("SELECT count(*) FROM r0 WHERE status = 'missing'").fetchone()[0])
        # insertion order is the order of first appearance, as the row loop built it
        for code, count in con.execute(
                "SELECT code, count(*) FROM r0 WHERE status = 'failed' GROUP BY code ORDER BY min(i)").fetchall():
            v.failed[code or ""] = int(count)
        firsts = []
        for col, name in _FLAGS:
            first, count = con.execute(f"SELECT min(i), count(*) FROM r0 WHERE {col}").fetchone()
            if count:
                firsts.append((int(first), name, int(count)))
        for _i, name, count in sorted(firsts):
            v.flagged[name] = count
        bad = con.execute(
            "SELECT r1.i, r1.reason, r0.txt FROM r1 JOIN r0 ON r0.i = r1.i WHERE r1.reason IS NOT NULL "
            f"ORDER BY r1.i LIMIT {T.MAX_EXAMPLES}").fetchall()
    finally:
        con.close()
    for i, reason, txt in bad:
        v.examples.append({"src": srcs[i], "source_value": T._cut(result.source_cell(i)), "result": txt,
                           "reason": reason})
    return v


def verify(recipe: Mapping[str, Any], rows: Iterable[T.Row], target_rules: Mapping[str, T.FieldRule] | None = None,
           context: T.Context | None = None) -> dict[str, T.Verdict]:
    """`transforms.verify` over whole columns: per field, how many rows conform to the target, MISSING, FAILED
    by code, flagged counts and up to five failing examples."""
    compiled = T.compile_recipe(recipe, target_rules, context)
    pieces: dict[str, list[pa.Table]] = {n: [] for n in compiled.fields}
    sources: dict[str, list[Any]] = {n: [] for n in compiled.fields}
    srcs: list[str] = []
    for headers, run in _runs(rows):
        columns = [[r.cells[i] if i < len(r.cells) else None for r in run] for i in range(len(headers))]
        srcs.extend(r.src for r in run)
        for name, cf in compiled.fields.items():
            got = apply_field(cf, headers, columns, context, len(run))
            pieces[name].append(got.table)
            sources[name].extend([got.source_cell(i) for i in range(len(got))])
    empty = _table({})
    return {name: verdict_of(cf, FieldResult(pa.concat_tables(pieces[name]) if pieces[name] else empty,
                                             sources[name]), srcs)
            for name, cf in compiled.fields.items()}
