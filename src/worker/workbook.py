"""General, format-agnostic ways to look at a workbook (no per-layout parsers).

Every function is pure, returns JSON-serialisable data, and is bounded in size.
Formulas are reported as text and never evaluated.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import column_index_from_string, range_boundaries

LOOK_MAX_ROWS = 80
LOOK_MAX_COLS = 20
LOOK_CLIP = 24
CSV_MAX_ROWS = 200_000

_IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "png",
    b"\xff\xd8\xff": "jpeg",
}


@dataclass
class _Sheet:
    name: str
    max_row: int
    max_column: int
    hidden: bool = False
    has_pivot: bool = False
    merged: list[str] = field(default_factory=list)
    hidden_rows: set[int] = field(default_factory=set)
    hidden_cols: set[int] = field(default_factory=set)
    # (value, formula, indent, bold) lookup
    cells: dict[tuple[int, int], tuple[Any, str | None, int, bool]] = field(default_factory=dict)

    def get(self, row: int, col: int) -> tuple[Any, str | None, int, bool] | None:
        return self.cells.get((row, col))


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    return str(value)


def _sniff(path: Path) -> str:
    """Return xlsx | csv | image | unknown, from content first, extension second."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return "unknown"
    for magic in _IMAGE_MAGIC:
        if head.startswith(magic):
            return "image"
    if head.startswith(b"PK\x03\x04"):
        return "xlsx"
    ext = path.suffix.lower()
    if not head:
        return "unknown"
    if b"\x00" in head:
        return "unknown"
    try:
        head.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            head.decode("cp1252")
        except UnicodeDecodeError:
            return "unknown"
    if ext in (".csv", ".tsv", ".txt") or b"," in head or b"\t" in head:
        return "csv"
    return "unknown"


def _load_csv(path: Path) -> _Sheet:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    delim = "\t" if path.suffix.lower() == ".tsv" else ","
    if path.suffix.lower() != ".tsv":
        try:
            delim = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            delim = ","
    sheet = _Sheet(name=path.stem, max_row=0, max_column=0)
    for r, row in enumerate(csv.reader(io.StringIO(text), delimiter=delim), start=1):
        if r > CSV_MAX_ROWS:
            break
        sheet.max_row = r
        for c, val in enumerate(row, start=1):
            sheet.max_column = max(sheet.max_column, c)
            if val != "":
                sheet.cells[(r, c)] = (val, None, 0, False)
    return sheet


def _load_xlsx(path: Path) -> list[_Sheet]:
    wb = openpyxl.load_workbook(path, data_only=False)
    try:
        wb_vals = openpyxl.load_workbook(path, data_only=True)
    except Exception:  # noqa: BLE001  cached values are a convenience only
        wb_vals = None
    sheets: list[_Sheet] = []
    for ws in wb.worksheets:
        vals_ws = wb_vals[ws.title] if wb_vals is not None and ws.title in wb_vals.sheetnames else None
        sh = _Sheet(
            name=ws.title,
            max_row=ws.max_row or 0,
            max_column=ws.max_column or 0,
            hidden=ws.sheet_state != "visible",
            has_pivot=bool(getattr(ws, "_pivots", None)),
            merged=[str(r) for r in ws.merged_cells.ranges],
        )
        for idx, dim in ws.row_dimensions.items():
            if dim.hidden:
                sh.hidden_rows.add(idx)
        for key, dim in ws.column_dimensions.items():
            if dim.hidden:
                try:
                    lo = column_index_from_string(key)
                except ValueError:
                    continue
                hi = dim.max if dim.max else lo
                sh.hidden_cols.update(range(lo, min(hi, lo + 1000) + 1))
        for row in ws.iter_rows():
            for cell in row:
                value = cell.value
                if value is None:
                    continue
                formula = None
                if isinstance(value, str) and value.startswith("=") and len(value) > 1:
                    formula = value
                    cached = vals_ws[cell.coordinate].value if vals_ws is not None else None
                    value = cached
                indent = int(getattr(cell.alignment, "indent", 0) or 0)
                bold = bool(cell.font.b) if cell.font is not None else False
                sh.cells[(cell.row, cell.column)] = (value, formula, indent, bold)
        sheets.append(sh)
    return sheets


_CACHE: dict[tuple[str, int, int], tuple[str, list[_Sheet]]] = {}


def _load(path: str | Path) -> tuple[str, list[_Sheet], int]:
    p = Path(path)
    st = p.stat()
    key = (str(p.resolve()), st.st_mtime_ns, st.st_size)
    if key in _CACHE:
        kind, sheets = _CACHE[key]
        return kind, sheets, st.st_size
    kind = _sniff(p)
    sheets: list[_Sheet] = []
    if kind == "xlsx":
        try:
            sheets = _load_xlsx(p)
        except Exception:  # noqa: BLE001  a corrupt workbook is just "unknown"
            kind = "unknown"
    elif kind == "csv":
        sheets = [_load_csv(p)]
    if len(_CACHE) >= 8:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = (kind, sheets)
    return kind, sheets, st.st_size


def _sheet(path: str | Path, name: str) -> _Sheet:
    kind, sheets, _ = _load(path)
    if kind not in ("xlsx", "csv"):
        raise ValueError(f"not a readable workbook: kind={kind}")
    for sh in sheets:
        if sh.name == name:
            return sh
    if len(sheets) == 1 and kind == "csv":
        return sheets[0]
    raise KeyError(f"no sheet named {name!r}; sheets: {[s.name for s in sheets]}")


def _dimensions(sh: _Sheet) -> str:
    if sh.max_row == 0 or sh.max_column == 0:
        return "A1:A1"
    return f"A1:{get_column_letter(sh.max_column)}{sh.max_row}"


def list_sheets(path: str | Path) -> dict[str, Any]:
    """Describe the file: its kind and, for workbooks, one entry per sheet."""
    kind, sheets, size = _load(path)
    if kind == "image":
        return {"kind": "image", "bytes": size}
    if kind == "unknown":
        return {"kind": "unknown"}
    return {
        "kind": kind,
        "sheets": [
            {
                "name": s.name,
                "dimensions": _dimensions(s),
                "max_row": s.max_row,
                "max_column": s.max_column,
                "hidden": s.hidden,
                "merged_count": len(s.merged),
                "has_pivot": s.has_pivot,
            }
            for s in sheets
        ],
    }


def read_range(path: str | Path, sheet: str, a1_range: str, max_cells: int = 1500) -> dict[str, Any]:
    """Non-empty cells in `a1_range`, row by row, capped at `max_cells`."""
    sh = _sheet(path, sheet)
    min_col, min_row, max_col, max_row = range_boundaries(a1_range)
    min_row = min_row or 1
    min_col = min_col or 1
    max_row = min(max_row or sh.max_row, sh.max_row)
    max_col = min(max_col or sh.max_column, sh.max_column)
    out: list[dict[str, Any]] = []
    truncated = False
    for r in range(min_row, max_row + 1):
        for c in range(min_col, max_col + 1):
            cell = sh.get(r, c)
            if cell is None:
                continue
            if len(out) >= max_cells:
                truncated = True
                break
            value, formula, indent, bold = cell
            item: dict[str, Any] = {"addr": f"{get_column_letter(c)}{r}", "value": _jsonable(value)}
            if formula is not None:
                item["formula"] = formula
            if indent:
                item["indent"] = indent
            if bold:
                item["bold"] = True
            out.append(item)
        if truncated:
            break
    return {"sheet": sh.name, "range": a1_range, "cells": out, "truncated": truncated}


def _display(cell: tuple[Any, str | None, int, bool] | None) -> str:
    if cell is None:
        return ""
    value, formula, indent, _ = cell
    if value is None and formula is not None:
        text = formula
    else:
        v = _jsonable(value)
        text = "" if v is None else str(v)
    text = " ".join(text.split())
    text = "." * (2 * indent) + text if indent else text
    return text if len(text) <= LOOK_CLIP else text[: LOOK_CLIP - 1] + "~"


def look(
    path: str | Path,
    sheet: str,
    first_row: int = 1,
    last_row: int = 60,
    first_col: int = 1,
    last_col: int = 16,
) -> dict[str, Any]:
    """A text grid of a window of the sheet, as a person would see it."""
    sh = _sheet(path, sheet)
    first_row = max(1, first_row)
    first_col = max(1, first_col)
    truncated = False
    if last_row - first_row + 1 > LOOK_MAX_ROWS:
        last_row = first_row + LOOK_MAX_ROWS - 1
        truncated = True
    if last_col - first_col + 1 > LOOK_MAX_COLS:
        last_col = first_col + LOOK_MAX_COLS - 1
        truncated = True
    last_row = max(first_row, last_row)
    last_col = max(first_col, last_col)
    cols = range(first_col, last_col + 1)
    width = LOOK_CLIP
    gutter = len(str(last_row)) + 1

    def fmt(cells: list[str]) -> str:
        return " | ".join(c.ljust(width) for c in cells).rstrip()

    header = [get_column_letter(c) + ("(hidden)" if c in sh.hidden_cols else "") for c in cols]
    lines = [" " * gutter + " | " + fmt(header)]
    for r in range(first_row, last_row + 1):
        cells = [_display(sh.get(r, c)) for c in cols]
        marker = " (hidden)" if r in sh.hidden_rows else ""
        lines.append(str(r).rjust(gutter) + " | " + fmt(cells) + marker)
    notes = []
    for ref in sh.merged:
        min_col, min_row, max_col, max_row = range_boundaries(ref)
        if first_row <= max_row and min_row <= last_row and first_col <= max_col and min_col <= last_col:
            notes.append(f"[merged {ref}]")
    text = "\n".join(lines + notes)
    return {
        "sheet": sh.name,
        "first_row": first_row,
        "last_row": last_row,
        "first_col": first_col,
        "last_col": last_col,
        "hidden_sheet": sh.hidden,
        "truncated": truncated,
        "text": text,
    }


def find(path: str | Path, sheet: str, text: str, limit: int = 20) -> dict[str, Any]:
    """Addresses of cells whose text contains `text` (case-insensitive)."""
    sh = _sheet(path, sheet)
    needle = text.lower()
    hits: list[dict[str, Any]] = []
    truncated = False
    for (r, c), (value, formula, _, _) in sorted(sh.cells.items()):
        shown = value if value is not None else formula
        if shown is None:
            continue
        s = str(_jsonable(shown))
        if needle in s.lower():
            if len(hits) >= limit:
                truncated = True
                break
            hits.append({"addr": f"{get_column_letter(c)}{r}", "value": s[:80]})
    return {"sheet": sh.name, "text": text, "matches": hits, "truncated": truncated}


_DATE_RE = re.compile(
    r"^(\d{4}-\d{1,2}-\d{1,2}([ T].*)?|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2} [A-Za-z]{3,9} \d{2,4})$"
)
_NUM_RE = re.compile(r"^\(?[-+]?[$€£]?\s?\d[\d,]*(\.\d+)?\)?%?$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{1,23}$")


def _shape(values: list[Any]) -> str:
    if not values:
        return "empty"
    votes = {"date-like": 0, "number": 0, "id-like": 0, "name-like": 0, "free text": 0}
    for v in values:
        if isinstance(v, dt.datetime | dt.date):
            votes["date-like"] += 1
        elif isinstance(v, bool):
            votes["free text"] += 1
        elif isinstance(v, int | float | Decimal):
            votes["number"] += 1
        else:
            s = str(v).strip()
            if _DATE_RE.match(s):
                votes["date-like"] += 1
            elif _NUM_RE.match(s):
                votes["number"] += 1
            elif _ID_RE.match(s) and any(ch.isdigit() for ch in s):
                votes["id-like"] += 1
            elif len(s) <= 60 and len(s.split()) <= 6 and not any(ch.isdigit() for ch in s[:1]):
                votes["name-like"] += 1
            else:
                votes["free text"] += 1
    best = max(votes, key=lambda k: votes[k])
    if votes[best] * 2 < len(values):
        return "free text"
    return best


def column_profile(
    path: str | Path,
    sheet: str,
    col: int | str,
    first_row: int = 1,
    last_row: int | None = None,
) -> dict[str, Any]:
    """Count, blanks, distinct, five sample values and a guess of the shape."""
    sh = _sheet(path, sheet)
    c = column_index_from_string(col.upper()) if isinstance(col, str) else int(col)
    first_row = max(1, first_row)
    last = min(last_row or sh.max_row, sh.max_row)
    values: list[Any] = []
    blanks = 0
    for r in range(first_row, last + 1):
        cell = sh.get(r, c)
        v = None if cell is None else (cell[0] if cell[0] is not None else cell[1])
        if v is None or (isinstance(v, str) and not v.strip()):
            blanks += 1
        else:
            values.append(v)
    distinct: list[Any] = []
    seen: set[str] = set()
    for v in values:
        key = repr(_jsonable(v))
        if key not in seen:
            seen.add(key)
            distinct.append(v)
    return {
        "sheet": sh.name,
        "col": get_column_letter(c),
        "first_row": first_row,
        "last_row": last,
        "count": len(values),
        "blanks": blanks,
        "distinct": len(distinct),
        "samples": [_jsonable(v) for v in distinct[:5]],
        "shape": _shape(values),
    }
