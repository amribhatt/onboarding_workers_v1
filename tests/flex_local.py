"""Shared by the layout-program tests: a real `build(book)` for the `investors_pivot.xlsx` fixture and an in-process
stand-in for the container (the same `Book` class the harness image uses, no Docker, no network)."""

from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PIVOT = ROOT / "tests" / "fixtures" / "messy" / "investors_pivot.xlsx"

PIVOT_PROGRAM = '''
def build(book):
    s = book.sheets()[0]
    cells = {}
    for r in book.rows(s):
        for c in book.cols(s):
            v = book.cell(s, r, c)
            if v is not None:
                cells[(r, c)] = v
    last = max(r for r, c in cells)
    status_cols = sorted(c for (r, c) in cells if r == 3 and c >= 3)

    def ref(r, c):
        return {"ref": book.ref(s, r, c)}

    def drop(r, c, why, covers=None):
        d = {"ref": book.ref(s, r, c), "reason": why}
        if covers:
            d["covers"] = covers
        return d

    rows, dropped = [], [drop(1, 1, "title"), drop(3, 1, "header"), drop(3, 2, "header"), drop(4, 12, "header")]
    group, group_measures, all_measures, used_header = None, [], [], set()
    for r in range(5, last + 1):
        label, name = cells.get((r, 1)), cells.get((r, 2))
        if name is not None:
            if label is not None:
                group = (r, 1)
            c = next(c for c in range(3, 12) if (r, c) in cells)
            sc = max(x for x in status_cols if x <= c)
            rows.append({"cells": [ref(*group), ref(r, 2), ref(3, sc), ref(4, c), ref(r, c)]})
            used_header.update([(3, sc), (4, c)])
            m = book.ref(s, r, c)
            group_measures.append(m)
            all_measures.append(m)
            dropped.append(drop(r, 12, "subtotal", [m]))
        elif label is not None and "total" in label[1].lower():
            dropped.append(drop(r, 1, "subtotal"))
            if label[1].startswith("Grand"):
                dropped.append(drop(r, 12, "grand_total", all_measures))
            else:
                dropped.append(drop(r, 12, "subtotal", group_measures))
                group_measures = []
    dropped += [drop(3, c, "header") for c in status_cols if (3, c) not in used_header]
    dropped += [drop(4, c, "header") for c in range(3, 12) if (4, c) not in used_header and (4, c) in cells]
    return {"columns": [{"name": "Investor type", "role": "dimension"}, {"name": "Investor", "role": "key"},
                        {"name": "Status", "role": "dimension"}, {"name": "Style", "role": "dimension"},
                        {"name": "Commitment", "role": "measure"}],
            "rows": rows, "dropped": dropped, "case": "nested_pivot"}
'''

# a transposed register: "Field | #1 | #2 | ..." down the first column, one investor per column
TRANSPOSED_PROGRAM = '''
def build(book):
    s = book.sheets()[0]
    rows = [r for r in book.rows(s) if r > 1]
    cols = [c for c in book.cols(s) if c > 1]
    names = [book.cell(s, r, 1)[1] for r in rows]
    out = [{"cells": [{"ref": book.ref(s, r, c)} for r in rows]} for c in cols]
    dropped = [{"ref": book.ref(s, 1, c), "reason": "header"} for c in [1] + cols]
    dropped += [{"ref": book.ref(s, r, 1), "reason": "header"} for r in rows]
    return {"columns": [{"name": n, "role": "key" if i == 0 else "dimension"} for i, n in enumerate(names)],
            "rows": out, "dropped": dropped, "case": "transposed"}
'''

# the same program with one cell left unaccounted: the Total header is neither used nor dropped
UNACCOUNTED = PIVOT_PROGRAM.replace('drop(4, 12, "header")', 'drop(3, 1, "header")', 1)


def _harness() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("layout_harness", ROOT / "sandbox" / "layout.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def local_layout(kind: str, program: bytes, inputs: dict[str, bytes], seed: int) -> bytes:
    """What `sandbox/layout.py` does with /in/grid.json and /prog/program.py, in this process."""
    assert kind == "layout"
    book = _harness().Book(json.loads(inputs["grid.json"]))
    ns: dict[str, Any] = {}
    exec(compile(program.decode("utf-8"), "program.py", "exec"), ns)  # noqa: S102  (the test's own program text)
    return json.dumps(ns["build"](book), sort_keys=True, separators=(",", ":")).encode("utf-8")
