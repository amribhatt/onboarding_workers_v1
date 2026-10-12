"""Confirmed mappings are applied as a join and give what a per-row dictionary lookup gave."""

from __future__ import annotations

import random

import pyarrow as pa

from worker.setops import ColumnMapper as Mapper
from worker.setops import RowTable


def _column(seed: int, n: int) -> list[str | None]:
    rnd = random.Random(seed)
    pool = [f"ACC-{i}" for i in range(40)] + ["  acc-3 ", "ACC-3", "", None, "x y"]
    return [rnd.choice(pool) for _ in range(n)]


def test_distinct_equals_a_dictionary_lookup() -> None:
    col = _column(1, 500)
    mapping = {f"ACC-{i}": f"{1000 + i}" for i in range(0, 40, 2)}
    got = Mapper(pa.array(col, type=pa.string())).distinct(mapping)
    assert [v for v, _n, _r in got] == list(dict.fromkeys(v for v in col if v is not None))
    assert all(r == mapping.get(v.strip()) for v, _n, r in got)
    assert {v: n for v, n, _r in got} == {v: col.count(v) for v in set(col) if v is not None}


def test_fold_ignores_case_and_outer_space() -> None:
    col = ["ACC-3", "  acc-3 ", "acc-4", None]
    got = Mapper(pa.array(col, type=pa.string()), fold=True).distinct({"Acc-3": "3000"})
    assert [(v, r) for v, _n, r in got] == [("ACC-3", "3000"), ("  acc-3 ", "3000"), ("acc-4", None)]


def test_unmapped_values_come_grouped_with_their_row_counts() -> None:
    col = ["a", "b", "b", "c", "c", "c", "", None, " "]
    got = Mapper(pa.array(col, type=pa.string())).distinct({"a": "1"})
    assert [(v, n) for v, n, r in got if r is None and v.strip()] == [("b", 2), ("c", 3)]


def test_match_keys_equals_the_row_loop() -> None:
    rows = [{"_src": f"S!{i}", "A": f" x{i % 4} ", "B": "Y  z"} for i in range(30)]
    keys = [("x1", "y z"), ("x3", "y z"), ("x9", "y z")]
    hits = RowTable.from_rows(rows).match_keys(["A", "B"], keys)
    want = [(i, keys.index(k)) for i, r in enumerate(rows)
            if (k := (" ".join(r["A"].split()).lower(), " ".join(r["B"].split()).lower())) in keys]
    assert hits == want
