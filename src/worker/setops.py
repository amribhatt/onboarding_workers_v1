"""Set-based operations over a job table: Arrow columns queried by DuckDB, never a Python loop over rows.

A `RowTable` holds the rows of one stage as text columns (what the checks compared: `str(cell)`), a row position
and the row's source address. Every question the checks ask (blanks, lengths, duplicates, identical columns,
distinct values, counts, exact totals) is one SQL statement over it, so a million rows cost milliseconds to
seconds instead of a pass of Python per question.

Text is compared as Python's `str.strip` / `str.split` compare it (the Unicode whitespace set below), so a finding
is the same whether the rows were scanned by a loop or by this module.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import weakref
from collections.abc import Iterable, Iterator, Mapping, Sequence
from decimal import Context, Decimal
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

SRC = "_src"
POS = "_i"
BATCH_ROWS = 10_000
MEMORY_LIMIT = "192MB"  # what DuckDB may hold for queries; the rows themselves are in files
MONEY_SCALE = 12  # twelve fraction digits, 38 digits in all; a value with more fraction digits is refused, not rounded
# What Python's str.strip()/split() treat as whitespace.
_WS = "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0          " \
      "      　"
_WS_CLASS = "[" + "".join(f"\\x{{{ord(c):x}}}" for c in _WS) + "]+"
_WS_OTHER = "[" + "".join(f"\\x{{{ord(c):x}}}" for c in _WS if c != " ") + "]"  # whitespace but the plain space
_lock = threading.Lock()
_DB: duckdb.DuckDBPyConnection | None = None


def database() -> duckdb.DuckDBPyConnection:
    """The process's in-memory DuckDB (created once, with the text macros); every user works on a cursor."""
    global _DB
    with _lock:
        if _DB is None:
            db = duckdb.connect(config={"memory_limit": MEMORY_LIMIT})
            # the whitespace trim is a plain-ASCII fast path first: trim(x, chars) alone costs ~2 s per million rows
            fast = ("CASE WHEN x = '' THEN x WHEN ascii(x) > 32 AND ascii(x) < 127 AND ascii(right(x, 1)) > 32 "
                    f"AND ascii(right(x, 1)) < 127 THEN x ELSE trim(x, '{_WS}') END")
            db.execute(f"CREATE MACRO pystrip(x) AS {fast}")
            # a string with no whitespace but single plain spaces is already collapsed: the regex replace is skipped
            db.execute(f"CREATE MACRO pynorm(x) AS lower(CASE WHEN regexp_matches(pystrip(x), "
                       f"'{_WS_OTHER}|  ') THEN regexp_replace(pystrip(x), '{_WS_CLASS}', ' ', 'g') "
                       "ELSE pystrip(x) END)")
            _DB = db
        return _DB


class PrecisionError(ValueError):
    """A value does not fit the exact decimal type; nothing is rounded."""


def _text_column(values: list[Any]) -> pa.Array:
    if set(map(type, values)) <= {str}:
        return pa.array(values, type=pa.string())
    return pa.array([v if type(v) is str else str(v) for v in values], type=pa.string())


class RowTable:
    """Rows as `str(cell)` columns. Column `c<k>` is `fields[k]`; `_src` and `_i` (0-based position) are fixed.

    The rows live in Parquet part files under a temporary folder (one per batch, so a million rows are never in
    memory at once); the view `t` reads them, a field a batch never had reading as the empty text."""

    def __init__(self, parts: Sequence[Path], fields: Sequence[str], folder: Path | None = None,
                 where: str = "") -> None:
        self.fields: list[str] = list(fields)
        self._index = {f: i for i, f in enumerate(self.fields)}
        self._parts = list(parts)
        self._folder = folder
        if folder is not None:
            weakref.finalize(self, shutil.rmtree, str(folder), True)
        self._where = where
        listed = ", ".join("'" + p.as_posix().replace("'", "''") + "'" for p in self._parts)
        cols = ", ".join([*(f"coalesce(c{k}, '') AS c{k}" for k in range(len(self.fields))), SRC, POS])
        self._con = database().cursor()
        if self._parts:
            source = f"read_parquet([{listed}], union_by_name = true)"
        else:
            source = "(SELECT NULL::VARCHAR AS _src, NULL::BIGINT AS _i WHERE false)"
            cols = ", ".join([*(f"''::VARCHAR AS c{k}" for k in range(len(self.fields))), SRC, POS])
        self._con.execute(f"CREATE TEMP VIEW t AS SELECT {cols} FROM {source} {where}")

    # ---- building ----
    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, Any]], fields: Sequence[str] | None = None) -> RowTable:
        return cls.from_batches(_batched(rows, BATCH_ROWS), fields)

    @classmethod
    def from_batches(cls, batches: Iterable[list[Mapping[str, Any]]], fields: Sequence[str] | None = None
                     ) -> RowTable:
        names: dict[str, None] = {f: None for f in (fields or ())}
        folder = Path(tempfile.mkdtemp(suffix=".rows"))
        parts: list[Path] = []
        start = 0
        for batch in batches:
            if not fields:
                for r in batch:
                    for k in r:
                        if k != SRC:
                            names.setdefault(k, None)
            cols = {f"c{k}": _text_column([r.get(f, "") for r in batch]) for k, f in enumerate(names)}
            cols[SRC] = _text_column([r.get(SRC, "") for r in batch])
            cols[POS] = pa.array(range(start, start + len(batch)), type=pa.int64())
            parts.append(folder / f"part-{len(parts)}.parquet")
            pq.write_table(pa.table(cols), parts[-1], compression="zstd", compression_level=1)
            start += len(batch)
        return cls(parts, list(names), folder)

    def __len__(self) -> int:
        return int(self._one("SELECT count(*) FROM t"))

    @property
    def table(self) -> pa.Table:
        """The rows as an Arrow table (read on demand)."""
        return self._con.execute("SELECT * FROM t").to_arrow_table()

    def col(self, field: str) -> str:
        """The SQL name of a field's column; a field the rows never had reads as the empty text."""
        k = self._index.get(field)
        return f"c{k}" if k is not None else "''"

    # ---- generic ----
    def rows_where(self, predicate: str, params: Sequence[Any] = (), limit: int | None = 50
                   ) -> tuple[int, list[str]]:
        """(how many rows satisfy `predicate`, the `_src` of the first `limit` of them in row order)."""
        n = self._one(f"SELECT count(*) FROM t WHERE {predicate}", params)
        sql = f"SELECT {SRC} FROM t WHERE {predicate} ORDER BY {POS}" + (f" LIMIT {int(limit)}" if limit else "")
        return int(n), [r[0] for r in self._all(sql, params)]

    def _one(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return self._all(sql, params)[0][0]

    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        return self._con.execute(sql, list(params)).fetchall()

    def scalar(self, expression: str) -> Any:
        """One aggregate over every row, as SQL over the columns named by `col` (for example a `sum`)."""
        return self._one(f"SELECT {expression} FROM t")

    def without(self, srcs: set[str]) -> RowTable:
        """The same rows minus those whose `_src` is in `srcs` (a filtered view over the same files)."""
        if not srcs:
            return self
        listed = ", ".join("'" + x.replace("'", "''") + "'" for x in sorted(srcs))
        where = f"{self._where} {'AND' if self._where else 'WHERE'} {SRC} NOT IN ({listed})"
        return RowTable(self._parts, self.fields, None, where)

    def blank(self, field: str, limit: int | None = 50) -> tuple[int, list[str]]:
        """Rows where `str(cell).strip() == ""`."""
        return self.rows_where(f"pystrip({self.col(field)}) = ''", (), limit)

    def first_value(self, field: str, predicate: str, params: Sequence[Any] = ()) -> str:
        rows = self._all(f"SELECT {self.col(field)} FROM t WHERE {predicate} ORDER BY {POS} LIMIT 1", params)
        return rows[0][0] if rows else ""

    def differs_from(self, field: str, constant: str, limit: int | None = 50) -> tuple[int, list[str]]:
        """Rows whose trimmed lower-cased cell is neither blank nor `constant.lower()`."""
        c = self.col(field)
        return self.rows_where(f"lower(pystrip({c})) NOT IN ('', ?)", (constant.lower(),), limit)

    def identical_columns(self, fields: Sequence[str]) -> list[tuple[str, str]]:
        """Pairs (a, b) of fields that are non-blank and equal (trimmed) on every row, for at least two rows."""
        n = len(self)
        if n < 2:
            return []
        names = [f for f in fields if f in self._index]
        exprs: list[str] = []
        pairs: list[tuple[str, str]] = []
        for i, a in enumerate(names):
            for j in range(i + 1, len(names)):
                # count(CASE ...) over trimmed-once columns: a hundred FILTER aggregates cost 400 MB of buffers
                exprs.append(f"count(CASE WHEN s{i} <> '' AND s{i} = s{j} THEN 1 END)")
                pairs.append((a, names[j]))
        if not exprs:
            return []
        trimmed = ", ".join(f"pystrip({self.col(f)}) AS s{i}" for i, f in enumerate(names))
        row = self._all(f"SELECT {', '.join(exprs)} FROM (SELECT {trimmed} FROM t)")[0]
        return [p for p, hit in zip(pairs, row, strict=True) if hit == n]

    def duplicates(self, key_fields: Sequence[str], normalise: bool = True) -> list[dict[str, Any]]:
        """Groups of rows with the same key where at least one part is non-blank, as
        `[{"key": "a | b", "rows": [src, ...]}]` in order of first appearance; only groups of two or more."""
        if not key_fields:
            return []
        parts = [f"pynorm({self.col(f)})" if normalise else f"pystrip({self.col(f)})" for f in key_fields]
        keys = ", ".join(parts)
        anyfilled = " OR ".join(f"pystrip({self.col(f)}) <> ''" for f in key_fields)
        joined = " || ' | ' || ".join(parts)
        sql = (f"SELECT {joined} AS k, list({SRC} ORDER BY {POS}) AS srcs, min({POS}) AS first "
               f"FROM t WHERE {anyfilled} GROUP BY {keys} HAVING count(*) > 1 ORDER BY first")
        return [{"key": r[0], "rows": list(r[1])} for r in self._all(sql)]

    def later_duplicates(self, key_fields: Sequence[str]) -> set[str]:
        """The `_src` of every row after the first of its (normalised) key group."""
        if not key_fields:
            return set()
        parts = [f"pynorm({self.col(f)})" for f in key_fields]
        keys = ", ".join(parts)
        anyfilled = " OR ".join(f"pystrip({self.col(f)}) <> ''" for f in key_fields)
        sql = (f"SELECT {SRC} FROM (SELECT {SRC}, row_number() OVER (PARTITION BY {keys} ORDER BY {POS}) AS r "
               f"FROM t WHERE {anyfilled}) WHERE r > 1")
        return {r[0] for r in self._all(sql)}

    def duplicate_ids(self, key_fields: Sequence[str], limit: int = 50) -> tuple[int, int, list[str]]:
        """(distinct duplicated keys, rows in them, first `limit` `_src`) for keys whose every part is non-blank,
        a key is the parts joined with " | " after whitespace is collapsed and lower-cased."""
        joined = " || ' | ' || ".join(f"pynorm({self.col(f)})" for f in key_fields)
        allfilled = " AND ".join(f"pystrip({self.col(f)}) <> ''" for f in key_fields)
        self._con.execute(
            f"CREATE OR REPLACE TEMP TABLE dup_keys AS SELECT k, count(*) AS n, min({POS}) AS first FROM "
            f"(SELECT {POS}, {joined} AS k FROM t WHERE {allfilled}) GROUP BY k HAVING count(*) > 1")
        try:
            groups, rows = self._con.execute("SELECT count(*), coalesce(sum(n), 0) FROM dup_keys").fetchone()
            if not groups:
                return 0, 0, []
            # the first `limit` rows of duplicated keys all sit in the `limit` groups that start earliest
            srcs = [r[0] for r in self._all(
                f"SELECT {SRC} FROM t WHERE {allfilled} AND {joined} IN "
                f"(SELECT k FROM dup_keys ORDER BY first LIMIT {int(limit)}) ORDER BY {POS} LIMIT {int(limit)}")]
        finally:
            self._con.execute("DROP TABLE dup_keys")
        return int(groups), int(rows), srcs

    def unknown_values(self, field: str, known: Iterable[str], shown: int = 200, srcs: int = 50
                       ) -> tuple[int, int, list[tuple[str, int]], list[str]]:
        """The distinct values of a field (lower-cased, trimmed, non-blank) that are not in `known`, in order of
        first appearance. Returns (how many distinct values, how many rows hold them, the first `shown` as
        (first spelling, rows), the `_src` of the first `srcs` rows in value order)."""
        c = self.col(field)
        keys = pa.array(sorted({str(k) for k in known}), type=pa.string())
        self._con.register("known", pa.table({"k": keys}))
        try:
            self._con.execute(
                f"CREATE OR REPLACE TEMP TABLE missing AS SELECT lower(pystrip({c})) AS k, "
                f"first(pystrip({c}) ORDER BY {POS}) AS shown, count(*) AS n, min({POS}) AS first FROM t "
                f"WHERE pystrip({c}) <> '' AND lower(pystrip({c})) NOT IN (SELECT k FROM known) GROUP BY k")
            groups, rows = self._con.execute("SELECT count(*), coalesce(sum(n), 0) FROM missing").fetchone()
            head = self._con.execute(
                f"SELECT shown, n FROM missing ORDER BY first LIMIT {int(shown)}").fetchall()
            found = self._con.execute(
                f"SELECT t.{SRC} FROM t JOIN missing m ON lower(pystrip(t.{c})) = m.k "
                f"ORDER BY m.first, t.{POS} LIMIT {int(srcs)}").fetchall()
        finally:
            self._con.unregister("known")
        return int(groups), int(rows), [(r[0], int(r[1])) for r in head], [r[0] for r in found]

    def values_of(self, fields: Sequence[str]) -> set[str]:
        """Distinct lower-cased trimmed non-blank values over the given fields."""
        out: set[str] = set()
        for f in fields:
            if f not in self._index:
                continue
            c = self.col(f)
            out.update(r[0] for r in self._all(
                f"SELECT DISTINCT lower(pystrip({c})) FROM t WHERE pystrip({c}) <> ''"))
        return out

    def group_counts(self, fields: Sequence[str]) -> list[tuple[tuple[str, ...], int]]:
        """The distinct combinations of these fields' texts with their row counts, in order of first appearance."""
        cols = ", ".join(self.col(f) for f in fields)
        return [(tuple(r[:-1]), int(r[-1])) for r in self._all(
            f"SELECT {cols}, count(*) FROM t GROUP BY {cols} ORDER BY min({POS})")]

    def exact_sum(self, field: str) -> tuple[Decimal, int]:
        """(exact sum, rows summed) of a field's non-blank cells. A cell that is not a plain number, or has more than
        12 fraction digits, or does not fit 38 digits, raises `PrecisionError`; nothing is rounded or skipped.

        The distinct texts are counted first, so each is parsed once, not once per row; a text becomes a scaled
        integer (its digits padded with zeros to 12 fraction digits, an exact HUGEINT) and the total is
        `sum(scaled * rows)`, so the work grows with the distinct values and no float or DECIMAL cast is involved."""
        c = self.col(field)
        zeros = "12 - CASE WHEN contains(s, '.') THEN length(s) - strpos(s, '.') ELSE 0 END"
        try:
            bad, total, n = self._all(
                f"WITH d AS (SELECT pystrip(r) AS s, sum(n) AS n FROM (SELECT {c} AS r, count(*) AS n FROM t GROUP BY r) "
                f"GROUP BY 1), v AS (SELECT n, TRY_CAST(replace(s, '.', '') || repeat('0', {zeros}) AS HUGEINT) AS x, "
                f"regexp_full_match(s, '[+-]?[0-9]+(\\.[0-9]{{1,{MONEY_SCALE}}})?') AS ok FROM d WHERE s <> '') "
                f"SELECT coalesce(sum(n) FILTER (WHERE NOT ok OR x IS NULL), 0), "
                f"coalesce(sum(x * n::HUGEINT) FILTER (WHERE ok), 0), coalesce(sum(n), 0) FROM v")[0]
        except duckdb.OutOfRangeException as e:
            raise PrecisionError(f"the total of {field} does not fit 38 digits") from e
        if bad:
            raise PrecisionError(f"{bad} value(s) of {field} are not an exact decimal of at most "
                                 f"{MONEY_SCALE} fraction digits")
        return Decimal(total).scaleb(-MONEY_SCALE, Context(prec=60)), int(n)

    def match_keys(self, key_fields: Sequence[str], keys: Sequence[tuple[str, ...]]) -> list[tuple[int, int]]:
        """Which rows hold which key: [(row position, index into `keys`)] for the rows whose normalised key
        (whitespace collapsed, lower-cased) equals one of `keys` (already normalised). A hash join, in row order."""
        if not keys or not key_fields:
            return []
        sep = "\x1f"
        parts = " || chr(31) || ".join(f"pynorm({self.col(f)})" for f in key_fields)
        cur = self._con
        cur.register("keys_t", pa.table({
            "k": pa.array([sep.join(k) for k in keys], type=pa.string()),
            "n": pa.array(range(len(keys)), type=pa.int64())}))
        try:
            return [(int(i), int(n)) for i, n in cur.execute(
                f"SELECT t.{POS}, keys_t.n FROM t JOIN keys_t ON {parts} = keys_t.k ORDER BY t.{POS}").fetchall()]
        finally:
            cur.unregister("keys_t")

    def column(self, field: str) -> pa.ChunkedArray:
        """A field's texts as an Arrow column (empty text for a field the rows never had)."""
        return self._con.execute(f"SELECT {self.col(field)} FROM t ORDER BY {POS}").to_arrow_table().column(0)

    def paired_mismatches(self, target: RowTable, pairs: Sequence[tuple[str, str]], where: str = ""
                          ) -> dict[str, Any]:
        """Join this table (the source) to `target` on `_src` and compare each (source field, target field) pair
        after trimming. `where` is a condition on the target side (column names of the target through
        `target.col`). Returns the rows joined, the target rows with no source row, the rows that differ per pair
        and the (count, checksum) of each side per pair."""
        cur = self._con
        cur.register("u", target.table)
        try:
            cond = f"WHERE {where}" if where else ""
            cur.execute(f"CREATE OR REPLACE TEMP TABLE tg AS SELECT * FROM u {cond}")
            joined = int(cur.execute(f"SELECT count(*) FROM tg JOIN t ON t.{SRC} = tg.{SRC}").fetchone()[0])
            unmatched = int(cur.execute(
                f"SELECT count(*) FROM tg WHERE NOT EXISTS (SELECT 1 FROM t WHERE t.{SRC} = tg.{SRC})").fetchone()[0])
            mismatches: dict[tuple[str, str], int] = {}
            checksums: dict[tuple[str, str], tuple[tuple[int, int], tuple[int, int]]] = {}
            for s_field, t_field in pairs:
                sc, tc = self.col(s_field), target.col(t_field)
                mismatches[(s_field, t_field)] = int(cur.execute(
                    f"SELECT count(*) FROM tg JOIN t ON t.{SRC} = tg.{SRC} "
                    f"WHERE pystrip(t.{sc}) <> pystrip(tg.{tc})").fetchone()[0])
                src_sum = cur.execute(
                    f"SELECT count(*), coalesce(sum(CAST(hash(pystrip(t.{sc})) % 4611686018427387904 AS HUGEINT)), 0)"
                    f" FROM t WHERE t.{SRC} IN (SELECT {SRC} FROM tg)").fetchone()
                tgt_sum = cur.execute(
                    f"SELECT count(*), coalesce(sum(CAST(hash(pystrip(tg.{tc})) % 4611686018427387904 AS HUGEINT)), 0)"
                    f" FROM tg WHERE tg.{SRC} IN (SELECT {SRC} FROM t)").fetchone()
                checksums[(s_field, t_field)] = ((int(src_sum[0]), int(src_sum[1])),
                                                 (int(tgt_sum[0]), int(tgt_sum[1])))
        finally:
            cur.unregister("u")
        return {"joined": joined, "unmatched": unmatched, "mismatches": mismatches, "checksums": checksums}

class ColumnMapper:
    """Confirmed mappings applied to one text column as a join, never as a per-row lookup.

    The column is dictionary-encoded once: `values` (its distinct non-null texts) and `counts` (rows per value).
    A mapping is a hash join of the distinct values against the mapping table, so a question is asked per distinct
    value, not per row. `fold` compares whitespace-collapsed,
    lower-cased text (`pynorm`); otherwise outer spaces are ignored (`pystrip`)."""

    def __init__(self, column: pa.Array | pa.ChunkedArray, fold: bool = False) -> None:
        col = column.combine_chunks() if isinstance(column, pa.ChunkedArray) else column
        if not (pa.types.is_string(col.type) or pa.types.is_large_string(col.type)):
            col = col.cast(pa.string())
        enc = pc.dictionary_encode(col)
        self.fold = fold
        self.values: pa.Array = enc.dictionary
        counts = pc.value_counts(enc.indices.drop_null())
        by_code = pa.table({"c": counts.field("values"), "n": counts.field("counts")})
        self._counts = by_code.take(pc.sort_indices(by_code["c"]))

    def _key(self, mapping_key: str) -> str:
        return " ".join(str(mapping_key).split()).lower() if self.fold else str(mapping_key).strip()

    def distinct(self, mapping: Mapping[str, str]) -> list[tuple[str, int, str | None]]:
        """[(value, rows, its mapped result or None)] for every distinct value, in order of first appearance."""
        con = database().cursor()
        norm = "pynorm" if self.fold else "pystrip"
        con.register("dv", pa.table({"c": pa.array(range(len(self.values)), type=pa.int64()), "v": self.values}))
        con.register("dn", self._counts)
        keys = list(mapping)
        con.register("mp", pa.table({"k": pa.array([self._key(k) for k in keys], type=pa.string()),
                                     "r": pa.array([mapping[k] for k in keys], type=pa.string())}))
        return [(v, int(n), r) for v, n, r in con.execute(
            f"SELECT dv.v, coalesce(dn.n, 0), mp.r FROM dv LEFT JOIN dn ON dn.c = dv.c "
            f"LEFT JOIN mp ON mp.k = {norm}(dv.v) ORDER BY dv.c").fetchall()]

def _batched(rows: Iterable[Mapping[str, Any]], size: int) -> Iterator[list[Mapping[str, Any]]]:
    batch: list[Mapping[str, Any]] = []
    for r in rows:
        batch.append(r)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch

