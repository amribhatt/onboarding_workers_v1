"""The conservation checks a layout program's output must pass, in exact Decimal code. Nothing here is a model.

A program returns cell REFERENCES. `verify` checks them against the grid the host built, then copies the values
itself (`materialise`): the program never types a number. A BLOCK has no override, a controller included; a WARN
is shown and each one must be ticked by the person who approves.

A subtotal is proven when it equals the exact sum of what it covers, or that sum rounded half-up to the decimals the
subtotal itself is printed with (a sheet that prints 5714.85 for 5714.8516 is still a sum); the second case is a
WARN, `subtotal_rounded`. There is no other tolerance.

BLOCK: bad_output, part_on_number, split_no_part, measure_not_number, double_count, unaccounted_cells,
double_accounted, subtotal_unproven, number_dropped, control_total, nondeterministic, explode_too_wide, limits.
WARN: label_numbers_dropped, subtotal_rounded, misaligned, possible_subtotal_kept, key_distinct_changed, explode_used.
(`conformance_below_total` needs the sponsor's Tier 1 recipe and is added by the caller that has one.)
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Any

from worker.flex.grid import canonical_json, cell_index, parse_ref, sha256_bytes

CASES = ("transposed", "header_encoded", "fill_down", "explode", "subtotals", "nested_pivot", "multi_block", "other")
REASONS = ("blank", "title", "header", "note", "subtotal", "grand_total", "label")
ROLES = ("dimension", "measure", "key")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9 _#%/().-]{0,63}$")
FLEX_DEFAULTS: dict[str, int] = {"label_cells": 20, "label_share_pct": 1, "label_floor": 3, "explode_max": 50,
                                 "max_rows_out": 200_000, "examples": 5, "kept_window": 60}

# ---- the usability lint (not the security boundary; the container is) ------------------------------------
ALLOWED_IMPORTS = {"re", "decimal", "itertools", "collections", "functools", "operator", "string", "typing",
                   "dataclasses", "math"}
FORBIDDEN_NAMES = {"eval", "exec", "compile", "__import__", "open", "globals", "locals", "breakpoint", "input",
                   "getattr", "setattr", "delattr", "vars"}


def lint_program(source: str) -> list[str]:
    """Reasons the program is refused before any run (empty: it may run)."""
    problems: list[str] = []
    if len(source.splitlines()) > 400:
        problems.append("more than 400 lines")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"syntax error: {exc.msg} (line {exc.lineno})"]
    has_build = any(isinstance(n, ast.FunctionDef) and n.name == "build" for n in tree.body)
    if not has_build:
        problems.append("no `def build(book)` at module level")
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            problems += [f"import {a.name} is not allowed" for a in n.names if a.name.split(".")[0] not in ALLOWED_IMPORTS]
        elif isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] not in ALLOWED_IMPORTS:
            problems.append(f"import from {n.module} is not allowed")
        elif isinstance(n, ast.Name) and n.id in FORBIDDEN_NAMES:
            problems.append(f"`{n.id}` is not allowed")
        elif isinstance(n, ast.Attribute) and n.attr.startswith("__") and n.attr.endswith("__"):
            problems.append(f"attribute {n.attr} is not allowed")
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) > 200:
            problems.append("a string literal over 200 characters")
    return list(dict.fromkeys(problems))


# ---- the result ------------------------------------------------------------------------------------------
@dataclass
class Verified:
    report: dict[str, Any]
    table: dict[str, Any] | None
    blocks: list[dict[str, Any]] = field(default_factory=list)
    warns: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocks

    @property
    def report_sha256(self) -> str:
        return sha256_bytes(canonical_json(self.report))

    @property
    def table_sha256(self) -> str:
        return sha256_bytes(canonical_json(self.table)) if self.table is not None else ""


def _d(text: str) -> Decimal:
    return Decimal(text)


def _total(values: Iterable[Decimal]) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 80
        return sum(values, Decimal(0))


def _issue(code: str, detail: str, examples: Sequence[Any] = (), n: int | None = None) -> dict[str, Any]:
    return {"code": code, "detail": detail, "count": n if n is not None else len(examples),
            "examples": list(examples)[:5]}


def _structure(doc: Any) -> list[str]:
    """Strict shape of the program's output; every violation, in words."""
    if not isinstance(doc, dict) or set(doc) - {"columns", "rows", "dropped", "case"} or \
            not {"columns", "rows", "dropped"} <= set(doc):
        return ["the output must be an object with exactly columns, rows, dropped (and case)"]
    out: list[str] = []
    cols, rows, dropped = doc["columns"], doc["rows"], doc["dropped"]
    if not isinstance(cols, list) or not cols or not isinstance(rows, list) or not isinstance(dropped, list):
        return ["columns, rows and dropped must be lists, with at least one column"]
    names = [c.get("name") if isinstance(c, dict) else None for c in cols]
    if any(not isinstance(c, dict) or set(c) != {"name", "role"} or c["role"] not in ROLES for c in cols):
        out.append("each column is {name, role} and role is dimension, measure or key")
    if any(not isinstance(n, str) or not _NAME.match(n) for n in names) or len(set(names)) != len(names):
        out.append("column names must be unique headers (letters first, 64 characters at most)")
    if doc.get("case", "other") not in CASES:
        out.append(f"case must be one of {', '.join(CASES)}")
    for i, r in enumerate(rows):
        cells = r.get("cells") if isinstance(r, dict) and set(r) == {"cells"} else None
        if not isinstance(cells, list) or len(cells) != len(cols):
            out.append(f"row {i} must be {{cells: [one per column]}}")
            break
        for c in cells:
            ok = isinstance(c, dict) and (
                (set(c) == {"blank"} and c["blank"] is True) or
                (set(c) == {"ref"} and isinstance(c["ref"], str)) or
                (set(c) == {"ref", "part"} and isinstance(c["ref"], str) and isinstance(c["part"], dict)
                 and set(c["part"]) == {"sep", "index"} and isinstance(c["part"]["sep"], str)
                 and 0 < len(c["part"]["sep"]) <= 10 and isinstance(c["part"]["index"], int)
                 and not isinstance(c["part"]["index"], bool) and c["part"]["index"] >= 0))
            if not ok:
                out.append(f"row {i}: a cell is only {{ref}}, {{ref, part: {{sep, index}}}} or {{blank: true}}; "
                           "literal values are refused")
                return out
    for d in dropped:
        if not (isinstance(d, dict) and isinstance(d.get("ref"), str) and d.get("reason") in REASONS
                and set(d) <= {"ref", "reason", "covers"}
                and (not d.get("covers") or (isinstance(d["covers"], list)
                                              and all(isinstance(c, str) for c in d["covers"])))):
            out.append("each dropped cell is {ref, reason (closed list), covers?}")
            break
    return out


def _split(text: str, sep: str, index: int) -> str | None:
    parts = [p.strip() for p in text.split(sep)]
    return parts[index] if index < len(parts) else None


def verify(grid: Mapping[str, Any], raw: bytes, raw_second: bytes | None = None,
           flex: Mapping[str, int] | None = None) -> Verified:
    """Check one program output against the grid. `raw_second` is the output of the second run: a different
    digest is `nondeterministic`."""
    cfg = {**FLEX_DEFAULTS, **(flex or {})}
    blocks: list[dict[str, Any]] = []
    warns: list[dict[str, Any]] = []
    if raw_second is not None and sha256_bytes(raw) != sha256_bytes(raw_second):
        blocks.append(_issue("nondeterministic", "two runs with different hash seeds gave different outputs"))
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return _finish(blocks + [_issue("bad_output", "the output is not valid JSON")], warns, {}, None)
    bad = _structure(doc)
    if bad:
        return _finish(blocks + [_issue("bad_output", bad[0], bad)], warns, {}, None)

    index = cell_index(grid)
    cols, rows = doc["columns"], doc["rows"]
    roles = [c["role"] for c in cols]
    if len(rows) > cfg["max_rows_out"]:
        blocks.append(_issue("limits", f"{len(rows)} rows is over the {cfg['max_rows_out']} allowed"))

    missing: list[str] = []
    not_number: list[str] = []
    part_bad: list[str] = []
    part_range: list[str] = []
    used: set[str] = set()
    measure_cols: dict[str, set[int]] = {}
    measure_uses: Counter[str] = Counter()
    explode: Counter[str] = Counter()
    out_rows: list[list[str]] = []
    src_rows: list[list[str]] = []
    for r in rows:
        values: list[str] = []
        sources: list[str] = []
        for ci, c in enumerate(r["cells"]):
            if c.get("blank"):
                values.append("")
                sources.append("")
                continue
            hit = index.get(c["ref"])
            if hit is None:
                missing.append(c["ref"])
                values.append("")
                sources.append("")
                continue
            used.add(c["ref"])
            sources.append(c["ref"])
            value = hit[1]
            if "part" in c:
                if hit[0] != "s" or roles[ci] == "measure":
                    part_bad.append(c["ref"])
                else:
                    got = _split(hit[1], c["part"]["sep"], c["part"]["index"])
                    if got is None:
                        part_range.append(c["ref"])
                    value = got or ""
                    explode[c["ref"]] += 1
            elif roles[ci] == "measure":
                measure_uses[c["ref"]] += 1
                measure_cols.setdefault(c["ref"], set()).add(ci)
                if hit[0] != "n":
                    not_number.append(c["ref"])
            values.append(value)
        out_rows.append(values)
        src_rows.append(sources)
    if missing:
        blocks.append(_issue("bad_output", "a reference names a cell that is not there or is blank", missing,
                             len(missing)))
    if part_bad:
        blocks.append(_issue("part_on_number", "a part of a non-text cell, or a part in a measure column", part_bad))
    if part_range:
        blocks.append(_issue("split_no_part", "a part index beyond the parts of its cell", part_range))
    if not_number:
        blocks.append(_issue("measure_not_number", "a measure column draws from a cell that is not a number",
                             not_number, len(not_number)))
    twice = [r for r, n in measure_uses.items() if n > 1] + [r for r, c in measure_cols.items() if len(c) > 1]
    if twice:
        blocks.append(_issue("double_count", "a number is used more than once as a measure", sorted(set(twice)),
                             len(set(twice))))
    wide = [r for r, n in explode.items() if n > cfg["explode_max"]]
    if wide:
        blocks.append(_issue("explode_too_wide", f"one cell split into more than {cfg['explode_max']} rows", wide))

    drops = {d["ref"]: d for d in doc["dropped"]}
    unknown_drop = [r for r in drops if r not in index]
    if unknown_drop:
        blocks.append(_issue("bad_output", "a dropped reference names a cell that is not there", unknown_drop,
                             len(unknown_drop)))
    both = sorted(used & set(drops))
    if both:
        blocks.append(_issue("double_accounted", "a cell is both used and dropped", both, len(both)))
    unaccounted = sorted(set(index) - used - set(drops))
    if unaccounted:
        blocks.append(_issue("unaccounted_cells", "non-blank cells neither used nor dropped", unaccounted,
                             len(unaccounted)))

    # subtotals: a dropped number that claims to be a sum must be exactly the sum of what it covers
    proven: dict[str, Decimal] = {}
    unproven: list[str] = []
    rounded: list[str] = []
    ordered = sorted((d for d in drops.values() if d["reason"] in ("subtotal", "grand_total")
                      and d["ref"] in index and index[d["ref"]][0] == "n"),
                     key=lambda d: (d["reason"] == "grand_total", d["ref"]))
    for d in ordered:
        covers = d.get("covers") or []
        ok = bool(covers) and all(c in index and index[c][0] == "n" for c in covers)
        if ok:
            cols_used = {next(iter(measure_cols[c])) for c in covers if c in measure_cols}
            from_measure = [c for c in covers if c in measure_cols]
            from_proven = [c for c in covers if c in proven]
            ok = (len(from_measure) + len(from_proven) == len(covers) and len(cols_used) <= 1
                  and not (from_proven and d["reason"] != "grand_total"))
        if ok:
            value, summed = _d(index[d["ref"]][1]), _total(_d(index[c][1]) for c in covers)
            if summed != value:  # a printed total is the exact sum rounded to the decimals it is printed with
                ok = summed.quantize(Decimal(1).scaleb(value.as_tuple().exponent), rounding=ROUND_HALF_UP) == value
                rounded.append(d["ref"])
        if ok:
            proven[d["ref"]] = _d(index[d["ref"]][1])
        else:
            unproven.append(d["ref"])
    if unproven:
        blocks.append(_issue("subtotal_unproven",
                             "a dropped subtotal is not exactly the sum of the numbers it covers", unproven))

    rounded = [r for r in rounded if r in proven]
    if rounded:
        warns.append(_issue("subtotal_rounded", "a printed subtotal equals the exact sum only after rounding to its "
                            "own decimals", rounded, len(rounded)))
    label_numbers = [r for r, d in drops.items() if d["reason"] == "label" and r in index and index[r][0] == "n"]
    lost = [r for r, d in drops.items() if r in index and index[r][0] == "n" and r not in proven
            and r not in unproven and d["reason"] != "label"]
    n_numbers = sum(1 for t, _ in index.values() if t == "n")
    allowed_labels = min(cfg["label_cells"], max(cfg["label_floor"], n_numbers * cfg["label_share_pct"] // 100))
    if len(label_numbers) > allowed_labels:
        lost += label_numbers
    elif label_numbers:
        warns.append(_issue("label_numbers_dropped", "numbers dropped as labels (a year in a title, say)",
                            sorted(label_numbers), len(label_numbers)))
    if lost:
        blocks.append(_issue("number_dropped", "a number was dropped without being a proven subtotal",
                             sorted(set(lost)), len(set(lost))))

    # control total: every number in is a number out, a proven subtotal, a label or a number used as a dimension
    dim_numbers = [r for r in used if index[r][0] == "n" and r not in measure_uses]
    measure_idx = [i for i, role in enumerate(roles) if role == "measure"]
    out_total = _total(_d(v[i]) for v in out_rows for i in measure_idx if v[i] and _is_num(v[i]))
    in_total = _total(_d(v) for t, v in index.values() if t == "n")
    expected = in_total - _total(proven.values()) - _total(_d(index[r][1]) for r in label_numbers) \
        - _total(_d(index[r][1]) for r in dim_numbers)
    if not blocks_for(blocks, "number_dropped", "unaccounted_cells", "double_count") and out_total != expected:
        blocks.append(_issue("control_total", f"numbers in minus subtotals and labels is {expected}, the table "
                             f"holds {out_total}", [str(expected), str(out_total)], 1))

    table = {"columns": [{"name": c["name"], "role": c["role"]} for c in cols], "rows": out_rows,
             "sources": src_rows}
    if not blocks:
        warns += _alignment(rows, roles, cols)
        warns += _kept_subtotals(out_rows, measure_idx, cfg, roles)
        warns += _key_distinct(table, index, roles, cols, rows)
    if explode:
        warns.append(_issue("explode_used", f"{sum(explode.values())} rows came from splitting "
                            f"{len(explode)} cells", sorted(explode)[:5], len(explode)))
    info = {"input_cells": len(index), "used": len(used), "dropped": len(drops), "rows_out": len(out_rows),
            "numbers_in": str(in_total), "numbers_out": str(out_total), "subtotals_proven": len(proven),
            "case": doc.get("case", "other"), "case_counts": dict(Counter(d["reason"] for d in drops.values()))}
    return _finish(blocks, warns, info, table)


def _is_num(text: str) -> bool:
    try:
        Decimal(text)
    except ArithmeticError:
        return False
    return True


def blocks_for(blocks: Sequence[Mapping[str, Any]], *codes: str) -> bool:
    return any(b["code"] in codes for b in blocks)


def _alignment(rows: Sequence[Mapping[str, Any]], roles: Sequence[str], cols: Sequence[Mapping[str, Any]]
               ) -> list[dict[str, Any]]:
    """A row whose dimension cell shares neither the row nor the column of its measure cell, and is not above it
    (a block label), is a likely mis-pairing."""
    bad: list[str] = []
    for i, r in enumerate(rows):
        cells = r["cells"]
        m = next((parse_ref(c["ref"]) for c, role in zip(cells, roles, strict=True)
                  if role == "measure" and "ref" in c), None)
        if m is None:
            continue
        for c, role in zip(cells, roles, strict=True):
            d = parse_ref(c["ref"]) if role != "measure" and "ref" in c else None
            if d and d[0] == m[0] and d[1] != m[1] and d[2] != m[2] and not d[1] < m[1]:
                bad.append(f"row {i}: {c['ref']}")
                break
    return [_issue("misaligned", "a dimension does not share a row, a column or a block with its measure", bad,
                   len(bad))] if bad else []


def _kept_subtotals(out_rows: Sequence[Sequence[str]], measure_idx: Sequence[int], cfg: Mapping[str, int],
                    roles: Sequence[str]) -> list[dict[str, Any]]:
    """A measure that equals the exact sum of 2 or more rows just above it (in the same first-column group)."""
    found: list[str] = []
    for mi in measure_idx:
        vals = [_d(r[mi]) if r[mi] and _is_num(r[mi]) else None for r in out_rows]
        for i, v in enumerate(vals):
            if v is None or v == 0:
                continue
            run = Decimal(0)
            for k in range(i - 1, max(-1, i - 1 - cfg["kept_window"]), -1):
                if vals[k] is None or out_rows[k][0] != out_rows[i][0]:
                    break
                run += vals[k]
                if i - k >= 2 and run == v:
                    found.append(f"row {i} (column {mi})")
                    break
    return [_issue("possible_subtotal_kept", "a value equals the sum of the rows above it", found, len(found))] \
        if found else []


def _key_distinct(table: Mapping[str, Any], index: Mapping[str, tuple[str, str]], roles: Sequence[str],
                  cols: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """For a key column: distinct values out against distinct text cells in the columns it draws from."""
    out: list[dict[str, Any]] = []
    for ci, role in enumerate(roles):
        if role != "key":
            continue
        refs = [parse_ref(r["cells"][ci]["ref"]) for r in rows if "ref" in r["cells"][ci]]
        refs = [p for p in refs if p]
        if not refs:
            continue
        lo, hi = min(p[1] for p in refs), max(p[1] for p in refs)
        sheets_cols = {(p[0], p[2]) for p in refs}
        region = {v.strip().casefold() for k, (t, v) in index.items() if t == "s" and (p := parse_ref(k))
                  and (p[0], p[2]) in sheets_cols and lo <= p[1] <= hi}
        got = {r[ci].strip().casefold() for r in table["rows"] if r[ci]}
        if len(got) != len(region):
            out.append(_issue("key_distinct_changed", f"key {cols[ci]['name']}: {len(got)} distinct out, "
                              f"{len(region)} in the cells it draws from", [len(got), len(region)], 1))
    return out


def _finish(blocks: list[dict[str, Any]], warns: list[dict[str, Any]], info: dict[str, Any],
            table: dict[str, Any] | None) -> Verified:
    report = {"version": 1, "blocks": blocks, "warns": warns, "info": info}
    return Verified(report=report, table=table if not blocks else None, blocks=blocks, warns=warns)
