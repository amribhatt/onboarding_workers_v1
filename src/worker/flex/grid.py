"""The canonical typed grid a layout program reads, and the shape signature that decides reuse.

Trusted host code turns a workbook (or rows) into `{"sheets": [{"name", "cells": [[row, col, type, value]],
"merged": [[r1, c1, r2, c2]]}]}`, non-blank cells only. Types: `s` text, `n` number (a Decimal as a string),
`d` ISO date, `b` boolean. No parser runs in the sandbox; the grid's sha256 is the replay input.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from itertools import groupby
from pathlib import Path
from typing import Any

from worker.engine import Refused
from worker.hashing import canonical_strict
from worker.learnindex import header_norm

MAX_CELLS = 2_000_000
SIG_VERSION = 1


def canonical_json(obj: Any) -> bytes:
    return canonical_strict(obj).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _typed(value: Any) -> tuple[str, str] | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        return "b", "true" if value else "false"
    if isinstance(value, int | Decimal):
        return "n", str(value)
    if isinstance(value, float):
        return ("n", str(Decimal(repr(value)))) if math.isfinite(value) else ("s", repr(value))
    if isinstance(value, datetime | date):
        return "d", value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    return "s", str(value)


def grid_from_rows(sheets: Mapping[str, Sequence[Sequence[Any]]],
                   merged: Mapping[str, Sequence[Sequence[int]]] | None = None) -> dict[str, Any]:
    """`{sheet name: rows}` (row 1 is the first list) to the grid."""
    out: list[dict[str, Any]] = []
    for name, rows in sheets.items():
        cells = [[r, c, t[0], t[1]] for r, row in enumerate(rows, 1) for c, v in enumerate(row, 1)
                 if (t := _typed(v)) is not None]
        out.append({"name": name, "cells": cells, "merged": [list(m) for m in (merged or {}).get(name, [])]})
    n = sum(len(s["cells"]) for s in out)
    if n > MAX_CELLS:
        raise Refused("too_large", f"{n} cells is over the {MAX_CELLS} a layout program may read.")
    return {"sheets": out}


def grid_from_workbook(path: str | Path) -> dict[str, Any]:
    """Cached values of every sheet, merged ranges included (formulas are their last saved value)."""
    import openpyxl

    wb = openpyxl.load_workbook(str(path), data_only=True)
    try:
        rows = {ws.title: [list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets}
        merged = {ws.title: [[m.min_row, m.min_col, m.max_row, m.max_col] for m in ws.merged_cells.ranges]
                  for ws in wb.worksheets}
    finally:
        wb.close()
    return grid_from_rows(rows, merged)


def col_letters(col: int) -> str:
    out = ""
    while col:
        col, rem = divmod(col - 1, 26)
        out = chr(65 + rem) + out
    return out


def ref(sheet: str, row: int, col: int) -> str:
    return f"{sheet}!{col_letters(col)}{row}"


_REF = re.compile(r"^(.+)!([A-Z]{1,3})([1-9][0-9]*)$")


def parse_ref(text: str) -> tuple[str, int, int] | None:
    m = _REF.match(text) if isinstance(text, str) else None
    if not m:
        return None
    col = 0
    for ch in m.group(2):
        col = col * 26 + ord(ch) - 64
    return m.group(1), int(m.group(3)), col


def cell_index(grid: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """Every non-blank cell: `Sheet!A1` to (type, value)."""
    return {ref(s["name"], r, c): (t, v) for s in grid["sheets"] for r, c, t, v in s["cells"]}


# ---- the shape signature -------------------------------------------------------------------------------
def _mask(text: str) -> str:
    return re.sub(r"\d+", "9", header_norm(text))


def _edge(texts: Sequence[str]) -> list[str]:
    return [m + "+" if len(list(g)) > 1 else m for m, g in groupby(_mask(t) for t in texts)]


def layout_sig(grid: Mapping[str, Any]) -> str:
    """`layout_sig/v1`: per sheet, the masked first non-blank row and first non-blank column (text cells only, digit
    runs as 9, equal neighbours collapsed). Data rows do not move it; compared exactly, never fuzzily."""
    sheets: list[dict[str, Any]] = []
    for s in grid["sheets"]:
        cells = s["cells"]
        if not cells:
            sheets.append({"name": _mask(s["name"]), "top": [], "left": [], "merged_top": 0})
            continue
        first_row, first_col = min(c[0] for c in cells), min(c[1] for c in cells)
        top = [str(c[3]) for c in sorted(cells, key=lambda c: (c[1], c[0])) if c[0] == first_row and c[2] == "s"]
        left = [str(c[3]) for c in sorted(cells) if c[1] == first_col and c[2] == "s"]
        rows5 = sorted({c[0] for c in cells})[:5]
        top5 = sum(1 for m in s.get("merged", []) if any(m[0] <= r <= m[2] for r in rows5))
        sheets.append({"name": _mask(s["name"]), "top": _edge(top), "left": _edge(left), "merged_top": top5})
    return sha256_bytes(canonical_json({"sig_version": SIG_VERSION, "sheets": sheets}))


# ---- what the Layout Drafter is shown ------------------------------------------------------------------
def _cut(text: str, n: int) -> str:
    one = " ".join(str(text).split())
    return one if len(one) <= n else one[: n - 1] + "…"


def profile_grid(grid: Mapping[str, Any], *, sample_rows: int = 14, sample_cols: int = 14, text_len: int = 40,
                 runs: int = 40) -> dict[str, Any]:
    """A bounded, host-built description of a grid for the model: sizes, merged ranges, per column counts, runs of
    rows with the same shape, the first rows and the rows that read as totals. A number is shown only as `#` and a
    date as `d`: the model decides where cells go, never what they say, so no amount reaches it. Text is cut to
    `text_len` and is untrusted (it travels inside a FILE DATA block)."""
    out: list[dict[str, Any]] = []
    for s in grid["sheets"]:
        by_row: dict[int, dict[int, tuple[str, str]]] = {}
        per_col: dict[int, dict[str, Any]] = {}
        for r, c, t, v in s["cells"]:
            by_row.setdefault(r, {})[c] = (t, v)
            col = per_col.setdefault(c, {"col": col_letters(c), "text": 0, "number": 0, "other": 0, "first_row": r,
                                         "last_row": r})
            col["text" if t == "s" else "number" if t == "n" else "other"] += 1
            col["first_row"], col["last_row"] = min(col["first_row"], r), max(col["last_row"], r)

        def shape(row: Mapping[int, tuple[str, str]]) -> str:
            text = ",".join(col_letters(c) for c, (t, _v) in sorted(row.items()) if t == "s")
            nums = [col_letters(c) for c, (t, _v) in sorted(row.items()) if t == "n"]
            return f"text@{text or '-'} number@{','.join(nums[:4]) + ('+' if len(nums) > 4 else '') or '-'}"

        shapes: list[dict[str, Any]] = []
        for r in sorted(by_row):
            sh = shape(by_row[r])
            if shapes and shapes[-1]["shape"] == sh:
                shapes[-1]["to"] = r
            else:
                shapes.append({"from": r, "to": r, "shape": sh})
        first = [{"row": r, "cells": {col_letters(c): _cut(v, text_len) if t == "s" else "#" if t == "n" else t
                                      for c, (t, v) in sorted(by_row[r].items())[:sample_cols]}}
                 for r in sorted(by_row)[:sample_rows]]
        totals = [{"row": r, "label": _cut(" | ".join(v for t, v in by_row[r].values() if t == "s"), text_len)}
                  for r in sorted(by_row)
                  if any(t == "s" and re.search(r"\btotal\b", v, re.IGNORECASE) for t, v in by_row[r].values())]
        out.append({
            "name": s["name"], "cells": len(s["cells"]), "rows": max(by_row, default=0),
            "columns_used": max(per_col, default=0),
            "merged": [f"{col_letters(m[1])}{m[0]}:{col_letters(m[3])}{m[2]}" for m in sorted(s.get("merged", []))][:20],
            "columns": [per_col[c] for c in sorted(per_col)][:40], "row_shapes": shapes[:runs],
            "row_shapes_cut": len(shapes) > runs, "first_rows": first, "total_like_rows": totals[:30]})
    return {"layout_sig": layout_sig(grid), "sheets": out}


def unplaced_reasons(grid: Mapping[str, Any]) -> list[str]:
    """Why a header-in-row-1 reader cannot place the first sheet, in plain words (empty: it can). Three tells: row 1
    is not a header (a title, or blank), headers merged across columns, one header label repeated across columns
    (a transposed or wide sheet). Code only; it reads no value."""
    if not grid["sheets"] or not grid["sheets"][0]["cells"]:
        return []
    s = grid["sheets"][0]
    first = min(c[0] for c in s["cells"])
    head = [c for c in s["cells"] if c[0] == 1]
    texts = [str(c[3]) for c in head if c[2] == "s"]
    out: list[str] = []
    if first != 1 or len(texts) < 2:
        out.append("Row 1 is not a row of column headers (it is a title, a note or empty).")
    if any(m[0] <= 6 for m in s.get("merged", [])):
        out.append("Headers are merged across several columns, so one column has more than one header line.")
    masked = [m for m in (_mask(t) for t in texts) if m]
    if len(masked) >= 3 and max(masked.count(m) for m in set(masked)) * 2 > len(masked):
        out.append("One header label repeats across the columns, as in a transposed or wide sheet.")
    return out
