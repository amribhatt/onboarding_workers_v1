"""Fitness: no new Python loop over job rows in `src/worker` (a ratchet).

The engine's data path is set-based (Arrow columns queried by DuckDB: `columnar`, `setops`, `setrecipe`, `mapping`,
`accuracy`). A `for ... in rows` loop (or comprehension) over job data is the shape that does not scale to a
million rows, so this lint counts them per file and compares the count with `tests/rowloops_baseline.yaml`:

* a file with MORE loops than its baseline fails (a new row loop must be written as a query instead);
* a file with FEWER fails too, until the baseline is lowered, so the list only ever shrinks.

A loop is flagged when the iterable's source text names job data (rows, records, lines, batches, spools, cells,
streams of a reader). Each baseline entry says why those loops are still loops: the spool is the persistence
boundary (a record is encoded to, or decoded from, a line of the store), or the loop is a sequential decision (a
row's outcome depends on rows before it), or it walks a small, bounded collection.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml

SRC = Path(__file__).resolve().parents[1] / "src" / "worker"
BASELINE = Path(__file__).with_name("rowloops_baseline.yaml")
JOB_DATA = re.compile(
    r"(?<![A-Za-z_])(rows|recs|records|lines|batch|run|written|kept|src_rows|raw|_rows_of|iter_rows|cells|spool"
    r"|read_gl_lines|_row_batches)(?![A-Za-z_])|\.rows\(|\.stream\(|(?<![A-Za-z_.])read\(")


def row_loops(source: str) -> list[tuple[int, str]]:
    """(line, iterable text) of each `for` loop or comprehension whose iterable names job data."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            text = ast.unparse(node.iter)
            if JOB_DATA.search(text):
                found.append((getattr(node, "lineno", getattr(node.iter, "lineno", 0)), text))
    return sorted(found)


def counts() -> dict[str, int]:
    out = {}
    for path in sorted(SRC.glob("*.py")):
        n = len(row_loops(path.read_text(encoding="utf-8")))
        if n:
            out[path.name] = n
    return out


def test_the_lint_flags_a_loop_over_rows_and_not_a_loop_over_config() -> None:
    bad = "def f(rows):\n    for r in rows:\n        pass\n    return [x for x in records]\n"
    good = "def f(spec):\n    for name in spec.fields:\n        pass\n    return [x for x in range(3)]\n"
    assert [t for _n, t in row_loops(bad)] == ["rows", "records"]
    assert row_loops(good) == []
    assert row_loops("for r in self._rows_of(cv):\n    pass\n")


def test_no_module_gained_a_row_loop_and_the_baseline_only_shrinks() -> None:
    base = {k: int(v["count"]) for k, v in (yaml.safe_load(BASELINE.read_text(encoding="utf-8")) or {}).items()}
    for name, entry in yaml.safe_load(BASELINE.read_text(encoding="utf-8")).items():
        assert str(entry.get("reason", "")).strip(), f"{name} has no reason in {BASELINE.name}"
    now = counts()
    grew = {f: (base.get(f, 0), n) for f, n in now.items() if n > base.get(f, 0)}
    shrank = {f: (n, now.get(f, 0)) for f, n in base.items() if now.get(f, 0) < n}
    assert not grew, f"new loops over job rows (write a query instead): {grew}"
    assert not shrank, f"lower tests/rowloops_baseline.yaml (file: baseline, now): {shrank}"


@pytest.mark.parametrize("module", ["columnar.py", "setops.py", "setrecipe.py", "accuracy.py"])
def test_the_set_based_modules_have_no_row_loop_beyond_their_boundary(module: str) -> None:
    """These modules are the engine's compute layer; their only loops are over small plans and part files."""
    allowed = {"columnar.py": 6, "setops.py": 4, "setrecipe.py": 6, "accuracy.py": 2}
    assert len(row_loops((SRC / module).read_text(encoding="utf-8"))) <= allowed[module]
