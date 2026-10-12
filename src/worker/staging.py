"""Deterministic staging: copy cells from a source workbook into template-shaped rows.

Nothing here uses a model. Values are copied as-is (strings are stripped of outer whitespace);
every staged row remembers where it came from in `_src` (`Sheet!Row`). Functions return small
plain dicts, never the whole table.
"""

from __future__ import annotations

import csv
import datetime as dt
import difflib
import io
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.cell import WriteOnlyCell
from openpyxl.utils.cell import column_index_from_string

from worker import columnar
from worker import workbook as wb
from worker.registry import row_key
from worker.setops import RowTable
from worker.templates import TEMPLATES, TemplateSpec

SRC = "_src"
BLANK_MARKERS = frozenset({"n/a", "-", "--", "tbd"})
BLANK_RUN_STOP = 3
SAMPLE_ROWS = 3
_TOTAL_RE = re.compile(r"^\s*(grand\s*total|sub\s*total|total)\b", re.IGNORECASE)
_FURNITURE = {"row labels", "grand total"}


def _norm(value: Any) -> str:
    return " ".join(str(value).split()).lower()


def _is_marker(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in BLANK_MARKERS


def _clean(value: Any) -> tuple[Any, bool]:
    """Return (value, was_marker). Blank markers and empty text become ''."""
    if value is None:
        return "", False
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return "", False
        if _is_marker(text):
            return "", True
        return text, False
    return value, False


def _name_key(value: Any) -> str:
    """A name for matching: case, punctuation and spacing do not make a different name."""
    return " ".join(re.sub(r"[^\w\s]", " ", str(value).lower()).split())


def _similar(name: str, known: Mapping[str, Any]) -> str | None:
    """A known name that is a probable spelling variant of `name` (never an exact match), else None."""
    pool = [k for k in known if k[:1] == name[:1] and k != name and abs(len(k) - len(name)) <= 3]
    close = difflib.get_close_matches(name, pool, n=1, cutoff=0.9)
    return close[0] if close else None


def _brief_existing(info: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if info is None:
        return None
    out = {k: v for k, v in info.items() if k != "left_out"}
    out["left_out_examples"] = list(info.get("left_out", []))[:5]
    return out


def _match(rec: Mapping[str, Any]) -> dict[str, str]:
    return {k: str(rec.get(k, "")) for k in ("key", "name", "source", "fund", "batch")}


def _csv_text(value: Any) -> str:
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    return "" if value is None else str(value)


def _col(letter: str) -> int:
    return column_index_from_string(letter.strip().upper())


def _enc(value: Any) -> Any:
    """json `default`: the cell types a workbook gives (dates, decimals) keep their type across a restart."""
    if isinstance(value, dt.datetime):
        return {"__t": "datetime", "v": value.isoformat()}
    if isinstance(value, dt.date):
        return {"__t": "date", "v": value.isoformat()}
    if isinstance(value, dt.time):
        return {"__t": "time", "v": value.isoformat()}
    if isinstance(value, Decimal):
        return {"__t": "decimal", "v": str(value)}
    if isinstance(value, set | frozenset):
        return {"__t": "set", "v": sorted(value, key=str)}
    raise TypeError(f"cannot persist {type(value).__name__}")


def _dec(obj: dict[str, Any]) -> Any:
    kind = obj.get("__t")
    if kind is None or "v" not in obj or len(obj) != 2:
        return obj
    if kind == "datetime":
        return dt.datetime.fromisoformat(obj["v"])
    if kind == "date":
        return dt.date.fromisoformat(obj["v"])
    if kind == "time":
        return dt.time.fromisoformat(obj["v"])
    if kind == "decimal":
        return Decimal(obj["v"])
    if kind == "set":
        return set(obj["v"])
    return obj


def duplicate_groups(rows: Sequence[Mapping[str, Any]], key_fields: Sequence[str],
                     normalise: bool = True) -> list[dict[str, Any]]:
    """Groups of rows sharing a key, as `{key, rows: [source row ids]}`; a row with every key field blank has no key.
    The one grouping: `drop_duplicates` and the analysis tools call it."""
    return RowTable.from_rows(rows).duplicates(key_fields, normalise)


def value_overlap(left: Sequence[Mapping[str, Any]], right: Sequence[Mapping[str, Any]], left_field: str,
                  right_field: str) -> dict[str, int]:
    """Counts of distinct non-blank values only in left, only in right, in both (exact, trimmed)."""
    a = {str(r.get(left_field, "")).strip() for r in left} - {""}
    b = {str(r.get(right_field, "")).strip() for r in right} - {""}
    return {"left_distinct": len(a), "right_distinct": len(b), "only_left": len(a - b), "only_right": len(b - a),
            "both": len(a & b)}


class Stage:
    """Staged tables in memory, mirrored to `<out_dir>/staged/<id>.csv` for people and to `<id>.json` (rows and how
    they were built, types kept) so a restarted process can `restore` them."""

    def __init__(self, out_dir: str | Path, templates: Mapping[str, TemplateSpec] | None = None) -> None:
        self.out_dir = Path(out_dir)
        self.templates: Mapping[str, TemplateSpec] = templates if templates is not None else TEMPLATES
        self._rows: dict[str, list[dict[str, Any]]] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._counter = 0

    # ---- helpers -------------------------------------------------------------------------
    def _fields(self, template: str) -> tuple[str, ...]:
        if template not in self.templates:
            raise ValueError(f"unknown template {template!r}; known: {sorted(self.templates)}")
        return self.templates[template].fields

    def _check_fields(self, template: str, names: Sequence[str]) -> None:
        fields = self._fields(template)
        bad = [n for n in names if n not in fields]
        if bad:
            raise ValueError(f"fields not in template {template}: {bad}")

    def _store(self, template: str, rows: list[dict[str, Any]], meta: dict[str, Any]) -> str:
        self._counter += 1
        sid = f"s{self._counter}"
        self._rows[sid] = rows
        meta.update(template=template, stage_id=sid)
        self._meta[sid] = meta
        self._mirror(sid)
        self._persist(sid)
        return sid

    def _persist(self, sid: str) -> None:
        """Write the table and its meta atomically; a stage that cannot be written is an error, not a surprise later."""
        target = self.out_dir / "staged" / f"{sid}.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"rows": self._rows[sid], "meta": self._meta[sid]}, default=_enc, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(target)

    def restore(self) -> list[str]:
        """Load the staged tables of an earlier process from `<out_dir>/staged/*.json`; returns their ids."""
        found: dict[int, str] = {}
        for path in (self.out_dir / "staged").glob("s*.json"):
            m = re.fullmatch(r"s(\d+)", path.stem)
            if m:
                found[int(m.group(1))] = path.stem
        for number in sorted(found):
            sid = found[number]
            data = json.loads((self.out_dir / "staged" / f"{sid}.json").read_text(encoding="utf-8"), object_hook=_dec)
            self._rows[sid], self._meta[sid] = data["rows"], data["meta"]
            self._counter = max(self._counter, number)
        return [found[n] for n in sorted(found)]

    def _mirror(self, sid: str) -> None:
        directory = self.out_dir / "staged"
        directory.mkdir(parents=True, exist_ok=True)
        fields = [*self._fields(self._meta[sid]["template"]), SRC]
        with open(directory / f"{sid}.csv", "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(fields)
            for row in self._rows[sid]:
                writer.writerow([_csv_text(row.get(f, "")) for f in fields])

    def rows(self, sid: str) -> list[dict[str, Any]]:
        """A copy of the staged rows."""
        return [dict(r) for r in self._get(sid)]

    def _get(self, sid: str) -> list[dict[str, Any]]:
        if sid not in self._rows:
            raise KeyError(f"unknown stage id {sid!r}")
        return self._rows[sid]

    # ---- lifting -------------------------------------------------------------------------
    def _lift_range(
        self,
        sh: Any,
        cols: dict[str, int],
        header_row: int | None,
        first_row: int,
        last_row: int | None,
        constants: Mapping[str, str],
        acc: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Lift one block. Fills counters in `acc` and returns the rows."""
        header_text: dict[str, str] = {}
        if header_row is not None:
            for field_name, c in cols.items():
                cell = sh.get(header_row, c)
                header_text[field_name] = _norm(cell[0]) if cell and cell[0] is not None else ""
        first_col = min(min(cols.values()), 1)
        last = min(last_row if last_row is not None else sh.max_row, sh.max_row)
        rows: list[dict[str, Any]] = []
        blank_run = 0
        for r in range(max(1, first_row), last + 1):
            first_cell = sh.get(r, 1) or sh.get(r, first_col)
            lead = first_cell[0] if first_cell else None
            mapped_lead = sh.get(r, min(cols.values()))
            for candidate in (lead, mapped_lead[0] if mapped_lead else None):
                if isinstance(candidate, str) and _TOTAL_RE.match(candidate):
                    acc["stopped_at"] = f"{sh.name}!{r}"
                    acc["reason"] = f"total row: {candidate.strip()[:40]!r}"
                    return rows
            values: dict[str, Any] = {}
            any_filled = False
            for field_name, c in cols.items():
                cell = sh.get(r, c)
                raw = cell[0] if cell else None
                if cell and raw is None and cell[1] is not None:
                    acc["uncached_formulas"] += 1
                value, marker = _clean(raw)
                if marker:
                    acc["blank_markers"] += 1
                    acc["blank_markers_by_field"][field_name] = (
                        acc["blank_markers_by_field"].get(field_name, 0) + 1
                    )
                values[field_name] = value
                any_filled = any_filled or value != ""
            raw_texts = {f: _norm(v) for f, v in values.items()}
            if not any_filled:
                # an all-marker/blank row counts toward the stop run
                blank_run += 1
                acc["blank_rows"] += 1
                if blank_run >= BLANK_RUN_STOP:
                    acc["stopped_at"] = f"{sh.name}!{r}"
                    acc["reason"] = f"{BLANK_RUN_STOP} blank rows in a row"
                    return rows
                continue
            blank_run = 0
            if header_text and any(header_text.values()) and raw_texts == header_text:
                acc["repeated_headers"] += 1
                continue
            row: dict[str, Any] = {name: "" for name in acc["fields"]}
            row.update(values)
            for name, const in constants.items():
                row[name] = const
            row[SRC] = f"{sh.name}!{r}"
            rows.append(row)
        return rows

    def _new_acc(self, template: str) -> dict[str, Any]:
        return {
            "fields": self._fields(template),
            "blank_markers": 0,
            "blank_markers_by_field": {},
            "blank_rows": 0,
            "repeated_headers": 0,
            "uncached_formulas": 0,
            "stopped_at": None,
            "reason": None,
        }

    def _finish(self, template: str, rows: list[dict[str, Any]], acc: dict[str, Any], **extra: Any) -> str:
        meta = {k: v for k, v in acc.items() if k != "fields"}
        meta["input_rows"] = len(rows)
        meta.update(extra)
        return self._store(template, rows, meta)

    def _columns(
        self, template: str, columns: Mapping[str, str], constants: Mapping[str, str]
    ) -> dict[str, int]:
        self._check_fields(template, [*columns, *constants])
        return {f: _col(letter) for f, letter in columns.items()}

    def lift(
        self,
        path: str | Path,
        sheet: str,
        columns: dict[str, str],
        header_row: int | None,
        first_row: int,
        last_row: int | None = None,
        constants: dict[str, str] | None = None,
        *,
        template: str,
    ) -> str:
        """Copy `columns` ({template field: column letter}) from `first_row` down; returns a stage id."""
        consts = dict(constants or {})
        cols = self._columns(template, columns, consts)
        sh = wb._sheet(path, sheet)
        acc = self._new_acc(template)
        rows = self._lift_range(sh, cols, header_row, first_row, last_row, consts, acc)
        return self._finish(template, rows, acc, sources=dict(columns), sheet=sh.name)

    def lift_blocks(
        self,
        path: str | Path,
        sheet: str,
        columns: dict[str, str],
        blocks: Sequence[tuple[int, int, int | None]],
        constants: dict[str, str] | None = None,
        *,
        template: str,
    ) -> str:
        """Like `lift` for stacked tables: `blocks` is [(header_row, first_row, last_row), ...]."""
        consts = dict(constants or {})
        cols = self._columns(template, columns, consts)
        sh = wb._sheet(path, sheet)
        acc = self._new_acc(template)
        rows: list[dict[str, Any]] = []
        stops: list[dict[str, str | None]] = []
        for header_row, first_row, last_row in blocks:
            acc["stopped_at"] = None
            acc["reason"] = None
            rows.extend(self._lift_range(sh, cols, header_row, first_row, last_row, consts, acc))
            if acc["stopped_at"]:
                stops.append({"stopped_at": acc["stopped_at"], "reason": acc["reason"]})
        return self._finish(
            template, rows, acc, sources=dict(columns), sheet=sh.name, blocks=len(blocks), stops=stops
        )

    def lift_gl_lines(
        self,
        path: str | Path,
        sheet: str | None,
        columns: Mapping[str, str],
        header_row: int,
        *,
        gp_id: str,
        fund_id: str,
        fund_name: str,
        investors: Mapping[str, tuple[str, str]],
        offsets: bool = False,
        template: str = "GL",
    ) -> str:
        """Stage the lines of a GL source file as template rows, in code: Debit and Credit become one signed
        Total_Amount, the Event_ID is Journal-Line, an LP offset repeats a line with its sides swapped (`-OFF`).
        `columns` maps line parts (j, ln, acct, an, tt, dr, cr, desc, inv, date) to the file's headers where they
        differ from the standard ones; `investors` maps a reference in the file to (Ledgerline ID, name). The
        Account_Number stays blank here: the accounts come from the chart (map_accounts)."""
        from worker.glio import GlRows, offset_line, read_gl_lines  # local: glio imports this module

        def investor(ref: str) -> tuple[str, str]:
            found = investors.get(ref)
            return (found[0], found[1]) if found else ("", "")

        mk = GlRows(self.templates[template], gp_id, fund_id, fund_name, lambda _a, _t: "", investor)
        acc = self._new_acc(template)
        sheet_name = sheet or _first_sheet(path)
        rows: list[dict[str, Any]] = []
        extra: list[dict[str, Any]] = []
        read = 0
        for n, ln in read_gl_lines(path, sheet or None, columns, header_row):
            read += 1
            where = f"{sheet_name}!{n}"
            row = mk.row(ln)
            row[SRC] = where
            rows.append(row)
            if offsets and ln.inv:
                off = mk.row(offset_line(ln), offset=True)
                off[SRC] = where
                extra.append(off)
        rows.extend(extra)
        return self._finish(template, rows, acc, sources=dict(columns), sheet=sheet_name, input_rows=read,
                            generated_rows=len(extra), source_file=str(path))

    def flatten_compact(
        self,
        path: str | Path,
        sheet: str,
        levels: dict[int, str],
        first_row: int,
        *,
        template: str,
        last_row: int | None = None,
        column: str = "A",
    ) -> str:
        """Flatten a compact pivot: indent level N fills template field `levels[N]`."""
        self._check_fields(template, list(levels.values()))
        sh = wb._sheet(path, sheet)
        c = _col(column)
        deepest = max(levels)
        acc = self._new_acc(template)
        last = min(last_row or sh.max_row, sh.max_row)
        items: list[tuple[int, int, Any]] = []  # (row, level, value)
        for r in range(max(1, first_row), last + 1):
            cell = sh.get(r, c)
            if cell is None or cell[0] is None or (isinstance(cell[0], str) and not cell[0].strip()):
                continue
            text = cell[0]
            if isinstance(text, str) and text.strip().lower() in _FURNITURE:
                acc["furniture_rows"] = acc.get("furniture_rows", 0) + 1
                continue
            level = cell[2]
            if level not in levels:
                continue
            if isinstance(text, str) and text.strip().lower() == "(blank)":
                acc["blank_markers"] += 1
                acc["blank_markers_by_field"][levels[level]] = (
                    acc["blank_markers_by_field"].get(levels[level], 0) + 1
                )
                text = ""
            items.append((r, level, text.strip() if isinstance(text, str) else text))
        rows: list[dict[str, Any]] = []
        context: dict[int, Any] = {}
        for i, (r, level, value) in enumerate(items):
            context = {k: v for k, v in context.items() if k < level}
            context[level] = value
            next_level = items[i + 1][1] if i + 1 < len(items) else -1
            if level == deepest or next_level <= level:
                row: dict[str, Any] = {name: "" for name in acc["fields"]}
                for lv, val in context.items():
                    row[levels[lv]] = val
                row[SRC] = f"{sh.name}!{r}"
                rows.append(row)
        return self._finish(
            template, rows, acc, sources={v: f"{column} indent {k}" for k, v in levels.items()}, sheet=sh.name
        )

    def join(
        self,
        left_id: str,
        right_id: str,
        left_key: str,
        right_key: str,
        take: Sequence[str],
        how: str = "left",
    ) -> str:
        """Bring the `take` fields of the right table onto the left rows that share the key.

        Keys compare trimmed, case- and whitespace-insensitively. A left value that is already filled is
        never overwritten (a differing right value is counted as a conflict). `how="left"` keeps only left
        rows; `how="outer"` also appends right rows with no left match. A key that repeats on the right is
        an error: the human must say which row wins.
        """
        if how not in ("left", "outer"):
            raise ValueError(f"how must be 'left' or 'outer', not {how!r}")
        if not take:
            raise ValueError("take must name at least one field to bring across")
        left, right = self._get(left_id), self._get(right_id)
        lmeta, rmeta = self._meta[left_id], self._meta[right_id]
        template = lmeta["template"]
        self._check_fields(template, [left_key, *take])
        if rmeta["template"] != template:
            self._check_fields(rmeta["template"], [right_key, *take])
        else:
            self._check_fields(template, [right_key])

        index: dict[str, dict[str, Any]] = {}
        repeated: dict[str, list[str]] = {}
        for row in right:
            key = _norm(row.get(right_key, ""))
            if not key:
                continue
            if key in index:
                repeated.setdefault(key, [index[key][SRC]]).append(row[SRC])
            else:
                index[key] = row
        if repeated:
            shown = [f"{k!r} in {', '.join(v)}" for k, v in list(repeated.items())[:5]]
            raise ValueError(
                f"{len(repeated)} key(s) repeat in the right table ({right_key}); cannot join without "
                f"choosing a row: {'; '.join(shown)}"
            )

        used: set[str] = set()
        out: list[dict[str, Any]] = []
        conflicts: list[dict[str, str]] = []
        left_only: list[str] = []
        for row in left:
            new = dict(row)
            key = _norm(row.get(left_key, ""))
            match = index.get(key) if key else None
            if match is None:
                left_only.append(row[SRC])
            else:
                used.add(key)
                new[SRC] = f"{row[SRC]}+{match[SRC]}"
                for f in take:
                    theirs = match.get(f, "")
                    if theirs == "":
                        continue
                    if new.get(f, "") == "":
                        new[f] = theirs
                    elif _norm(new[f]) != _norm(theirs):
                        conflicts.append({"key": str(row.get(left_key, "")), "field": f})
            out.append(new)
        right_only = [k for k in index if k not in used]
        if how == "outer":
            for key in right_only:
                match = index[key]
                new = {left_key: match.get(right_key, ""), SRC: match[SRC]}
                new.update({f: match.get(f, "") for f in take})
                out.append(new)

        sources = dict(lmeta.get("sources", {}))
        for f in take:
            if f in rmeta.get("sources", {}):
                sources.setdefault(f, f"{rmeta['sources'][f]} (joined on {right_key})")
        stats = {
            "how": how,
            "left_key": left_key,
            "right_key": right_key,
            "matched": len(used),
            "left_only": len(left_only),
            "right_only": len(right_only),
            "right_only_kept": how == "outer",
            "conflicts": len(conflicts),
            "left_only_src": left_only[:10],
            "right_only_src": [index[k][SRC] for k in right_only[:10]],
            "conflict_samples": conflicts[:5],
        }
        meta = {k: v for k, v in lmeta.items() if k not in ("stage_id", "template")}
        meta.update(sources=sources, join=stats, sheet=lmeta.get("sheet"))
        return self._store(template, out, meta)

    def assign_ids(
        self,
        stage_id: str,
        id_field: str,
        next_id: Callable[[], str],
        source_key_field: str | None = None,
    ) -> tuple[str, dict[str, str]]:
        """Give every row a new ID from the sponsor's counter; the same old key always gets the same new ID.

        The old ID is copied to `source_key_field` when given (never over a value that is already there).
        A row with no old ID gets its own new one. Returns the new stage and the crosswalk old -> new.
        """
        rows = self._get(stage_id)
        meta = self._meta[stage_id]
        template = meta["template"]
        names = [id_field] + ([source_key_field] if source_key_field else [])
        self._check_fields(template, names)
        crosswalk: dict[str, str] = {}
        by_key: dict[str, str] = {}
        out: list[dict[str, Any]] = []
        for row in rows:
            new_row = dict(row)
            old = str(row.get(id_field, "")).strip()
            key = _norm(old)
            if key and key in by_key:
                new_id = by_key[key]
            else:
                new_id = next_id()
                if key:
                    by_key[key] = new_id
                    crosswalk[old] = new_id
            if source_key_field and old and new_row.get(source_key_field, "") == "":
                new_row[source_key_field] = old
            new_row[id_field] = new_id
            out.append(new_row)
        new_meta = {k: v for k, v in meta.items() if k not in ("template", "stage_id")}
        new_meta["id_assignment"] = {"field": id_field, "assigned": len(by_key) + sum(1 for r in rows if not str(r.get(id_field, "")).strip()),
                                     "source_key_field": source_key_field}
        return self._store(template, out, new_meta), crosswalk

    # ---- inspection ----------------------------------------------------------------------
    def distinct(self, stage_id: str, key_fields: Sequence[str]) -> str:
        """One row per distinct key, in first-seen order (the entity list inside a transaction file).

        Rows whose key is blank are not entities and are counted, not kept. Later rows of the same key may
        fill a blank field of the kept row but never overwrite a value; a differing non-blank value is a
        conflict (e.g. one ID under two names) and is reported, never resolved here.
        """
        if not key_fields:
            raise ValueError("key_fields must name at least one field")
        rows = self._get(stage_id)
        meta = self._meta[stage_id]
        template = meta["template"]
        self._check_fields(template, list(key_fields))
        kept: dict[tuple[str, ...], dict[str, Any]] = {}
        blank_key = collapsed = 0
        conflicts: list[dict[str, Any]] = []
        for row in rows:
            key = tuple(_norm(row.get(f, "")) for f in key_fields)
            if not all(key):
                blank_key += 1
                continue
            first = kept.get(key)
            if first is None:
                kept[key] = dict(row)
                continue
            collapsed += 1
            for name, value in row.items():
                if name == SRC or value == "":
                    continue
                current = first.get(name, "")
                if current == "":
                    first[name] = value
                elif _norm(current) != _norm(value):
                    conflicts.append({"key": " | ".join(key), "field": name, "kept": current, "other": value})
        out = list(kept.values())
        new = {k: v for k, v in meta.items() if k not in ("template", "stage_id")}
        new["distinct"] = {
            "key_fields": list(key_fields),
            "source_rows": len(rows),
            "kept": len(out),
            "collapsed": collapsed,
            "blank_key": blank_key,
            "conflicts": len(conflicts),
            "conflict_examples": conflicts[:5],
        }
        return self._store(template, out, new)

    def edit_rows(
        self,
        stage_id: str,
        drop: Sequence[str] = (),
        edits: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> str:
        """A new stage without the rows at source addresses `drop`, with `edits` ({src: {field: value}}) applied.

        This is what replay uses to reapply recorded human decisions; the decisions themselves are made in
        `resolve_conflicts`.
        """
        meta = self._meta[stage_id]
        template = meta["template"]
        gone = set(drop)
        edits = edits or {}
        for fields in edits.values():
            self._check_fields(template, list(fields))
        out = []
        for row in self._get(stage_id):
            src = str(row.get(SRC, ""))
            if src in gone:
                continue
            out.append({**row, **edits[src]} if src in edits else dict(row))
        new = {k: v for k, v in meta.items() if k not in ("template", "stage_id")}
        return self._store(template, out, new)

    def set_by_key(
        self,
        stage_id: str,
        key_fields: Sequence[str],
        values: Mapping[tuple[str, ...], Mapping[str, Any]],
        *,
        only_blank: bool = True,
    ) -> tuple[str, int]:
        """Fill fields on every row whose key (normalised values of `key_fields`) is in `values`.

        `values` maps a key tuple to {field: value}. With `only_blank` a field that already has a value is kept.
        Returns the new stage and the number of rows changed. Keys not in `values` are left exactly as they are.
        """
        rows = self._get(stage_id)
        meta = self._meta[stage_id]
        template = meta["template"]
        self._check_fields(template, list(key_fields))
        for fields in values.values():
            self._check_fields(template, list(fields))
        out = list(rows)
        changed = 0
        keys = list(values)
        # the lookup is one hash join of the rows' keys against the mapping; only matched rows are touched
        for at, k in RowTable.from_rows(rows, list(key_fields)).match_keys(list(key_fields), keys):
            given = values[keys[k]]
            new_row = dict(rows[at])
            hit = False
            for name, value in given.items():
                if not only_blank or new_row.get(name, "") == "":
                    new_row[name] = value
                    hit = True
            changed += 1 if hit else 0
            out[at] = new_row
        new = {k: v for k, v in meta.items() if k not in ("template", "stage_id")}
        return self._store(template, out, new), changed

    def annotate(self, stage_id: str, key: str, value: Any) -> None:
        """Attach a record of how a stage was built (e.g. the account mapping) to it; later stages inherit it."""
        self._get(stage_id)
        self._meta[stage_id][key] = value
        self._persist(stage_id)

    def stage_ids(self, template: str | None = None) -> list[str]:
        """Ids of the staged tables in this run, optionally only those of one record type."""
        return [s for s, m in self._meta.items() if template is None or m["template"] == template]

    def meta(self, stage_id: str) -> dict[str, Any]:
        """A copy of everything recorded about how a stage was built (lift, distinct, existing, resolutions...)."""
        self._get(stage_id)
        return dict(self._meta[stage_id])

    def left_out(self, stage_id: str) -> list[dict[str, Any]]:
        """Rows `match_existing` left out of this stage as already onboarded, each with its match."""
        return list((self._meta[stage_id].get("existing") or {}).get("left_out", []))

    def _row_identity(
        self, row: Mapping[str, Any], key_fields: Sequence[str], name_field: str
    ) -> tuple[str, str, str]:
        """(normalised key, normalised name, key kind). Without an ID the normalised name is the key."""
        key = _norm(row_key(row, key_fields))
        name = _name_key(row.get(name_field, "")) if name_field else ""
        return key, name, "source id" if key else "name"

    def _check_existing(
        self,
        rows: Sequence[dict[str, Any]],
        existing: Sequence[Mapping[str, Any]],
        key_fields: Sequence[str],
        name_field: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
        """(kept rows, left out as already onboarded, conflicts, key kinds) for `rows` against `existing`."""
        by_key: dict[str, dict[str, Any]] = {}
        by_name: dict[str, dict[str, Any]] = {}
        for rec in existing:
            k, n = _norm(rec.get("key", "")), _name_key(rec.get("name", ""))
            if k:
                by_key.setdefault(k, dict(rec))
            sk = _norm(rec.get("source_key", ""))
            if sk:
                by_key.setdefault(sk, dict(rec))
            if n:
                by_name.setdefault(n, dict(rec))
        kept: list[dict[str, Any]] = []
        left_out: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        kinds = {"source id": 0, "name": 0}
        for row in rows:
            key, name, kind = self._row_identity(row, key_fields, name_field)
            kinds[kind] += 1
            item = {
                "src": str(row.get(SRC, "")),
                "key": row_key(row, key_fields),
                "name": str(row.get(name_field, "")) if name_field else "",
                "key_kind": kind,
            }
            hit_key = by_key.get(key) if key else None
            hit_name = by_name.get(name) if name else None
            if hit_key is not None:
                known = _name_key(hit_key.get("name", ""))
                if not name or not known or known == name:
                    left_out.append({**item, "reason": "already onboarded", "matched": _match(hit_key)})
                    continue
                conflicts.append({**item, "kind": "same_key_other_name", "matched": _match(hit_key)})
            elif hit_name is not None:
                if key and _norm(hit_name.get("key", "")) and _norm(hit_name["key"]) != key:
                    conflicts.append({**item, "kind": "same_name_other_key", "matched": _match(hit_name)})
                elif not key:  # no ID in the source: the name is the key
                    left_out.append({**item, "reason": "already onboarded", "matched": _match(hit_name)})
                    continue
            elif not key and name:
                close = _similar(name, by_name)
                if close is not None:
                    conflicts.append({**item, "kind": "probable_merge", "matched": _match(by_name[close])})
            kept.append(row)
        return kept, left_out, conflicts, kinds

    def match_existing(
        self,
        stage_id: str,
        existing: Sequence[Mapping[str, Any]],
        key_fields: Sequence[str],
        name_field: str = "",
    ) -> str:
        """Check staged rows against the sponsor's existing records (entries of `Registry`).

        Same key and same name: the row is left out as `already onboarded` (counted and listed with where the
        match came from). Same key, other name; same name, other key; or, with no key, a name that is only
        similar to a known one: the row stays in the stage and is an ERR to resolve (`resolve_conflicts`).
        `write_upload` refuses while any is unresolved. Nothing is dropped silently.
        """
        if not key_fields:
            raise ValueError("key_fields must name at least the ID field")
        rows = self._get(stage_id)
        meta = self._meta[stage_id]
        template = meta["template"]
        self._check_fields(template, list(key_fields) + ([name_field] if name_field else []))
        kept, left_out, conflicts, kinds = self._check_existing(rows, existing, key_fields, name_field)
        new = {k: v for k, v in meta.items() if k not in ("template", "stage_id")}
        by_source = Counter(x["matched"]["source"] for x in left_out)
        new["existing"] = {
            "checked_against": len(existing),
            "key_fields": list(key_fields),
            "name_field": name_field,
            "staged": len(rows),
            "kept": len(kept),
            "already_onboarded": len(left_out),
            "conflicts": len(conflicts),
            "key_kinds": kinds,
            "matched_by_source": dict(by_source),
            "left_out": left_out,
        }
        new["conflicts"] = conflicts
        new.setdefault("resolutions", [])
        return self._store(template, kept, new)

    def resolve_conflicts(
        self,
        stage_id: str,
        decisions: Mapping[str, Mapping[str, Any]],
        existing: Sequence[Mapping[str, Any]],
    ) -> str:
        """Apply human decisions to conflict rows, by source row address.

        Each decision is {"action": "exclude_duplicate" | "exclude_incorrect" | "correct", "reason": str,
        "values": {field: value}}. `correct` edits the row and checks it again against `existing`; a row that
        still conflicts stays unresolved. A decision without a reason is refused.
        """
        meta = self._meta[stage_id]
        template = meta["template"]
        checked = meta.get("existing")
        spec = self.templates[template]
        info = checked or {"key_fields": list(spec.key), "name_field": spec.name_field}
        open_by_src = {c["src"]: c for c in meta.get("conflicts", [])}
        name_f = info.get("name_field", "")
        for row in self._get(stage_id):  # any staged row can be corrected or excluded, not only conflict rows
            src = str(row.get(SRC, ""))
            if src in decisions and src not in open_by_src:
                open_by_src[src] = {
                    "src": src, "key": row_key(row, info["key_fields"]),
                    "name": str(row.get(name_f, "")) if name_f else "", "kind": "flagged_row", "matched": {},
                }
        for src, dec in decisions.items():
            if src not in open_by_src:
                raise ValueError(f"row {src} is not in this stage")
            if dec.get("action") not in ("exclude_duplicate", "exclude_incorrect", "correct"):
                raise ValueError(f"row {src}: action must be exclude_duplicate, exclude_incorrect or correct")
            if not str(dec.get("reason", "")).strip():
                raise ValueError(f"row {src}: a reason is required")
            if dec["action"] == "correct":
                if not dec.get("values"):
                    raise ValueError(f"row {src}: `correct` needs values")
                self._check_fields(template, list(dec["values"]))
        key_fields, name_field = info["key_fields"], info["name_field"]
        resolutions = list(meta.get("resolutions", []))
        out: list[dict[str, Any]] = []
        corrected: list[dict[str, Any]] = []
        for row in self._get(stage_id):
            src = str(row.get(SRC, ""))
            dec = decisions.get(src)
            if dec is None:
                out.append(row)
                continue
            conflict = open_by_src[src]
            record = {
                "src": src, "key": conflict["key"], "name": conflict["name"], "kind": conflict["kind"],
                "matched": conflict["matched"], "action": dec["action"], "reason": str(dec["reason"]).strip(),
            }
            if dec["action"] == "correct":
                new_row = dict(row)
                record["before"] = {f: row.get(f, "") for f in dec["values"]}
                new_row.update(dec["values"])
                record["after"] = dict(dec["values"])
                corrected.append(new_row)
                out.append(new_row)
            resolutions.append(record)
        still = [c for c in meta.get("conflicts", []) if c["src"] not in decisions]
        if corrected:  # a corrected row is checked again: it may be clean, an exact match, or still in conflict
            _, left, again, _ = self._check_existing(corrected, existing, key_fields, name_field)
            still += again
            gone = {x["src"] for x in left}
            out = [r for r in out if str(r.get(SRC, "")) not in gone]
            for rec in resolutions:
                if rec["src"] in gone and rec["action"] == "correct":
                    rec["now"] = "already onboarded"
        new = {k: v for k, v in meta.items() if k not in ("template", "stage_id")}
        new["conflicts"] = still
        new["resolutions"] = resolutions
        if checked is not None:
            new["existing"] = {**info, "kept": len(out), "conflicts": len(still)}
        return self._store(template, out, new)

    def duplicates(
        self, stage_id: str, key_fields: list[str], normalise: bool = True
    ) -> list[dict[str, Any]]:
        return duplicate_groups(self._get(stage_id), key_fields, normalise)

    def summary(self, stage_id: str) -> dict[str, Any]:
        rows = self._get(stage_id)
        meta = self._meta[stage_id]
        fields = self._fields(meta["template"])
        filled = {f: sum(1 for r in rows if r.get(f, "") != "") for f in fields}
        return {
            "stage_id": stage_id,
            "template": meta["template"],
            "rows": len(rows),
            "filled": filled,
            "blank_markers": meta.get("blank_markers", 0),
            "blank_markers_by_field": dict(meta.get("blank_markers_by_field", {})),
            "blank_rows": meta.get("blank_rows", 0),
            "repeated_headers": meta.get("repeated_headers", 0),
            "stopped_at": meta.get("stopped_at"),
            "reason": meta.get("reason"),
            "input_rows": meta.get("input_rows"),
            "stops": meta.get("stops", []),
            "sources": meta.get("sources", {}),
            "join": meta.get("join"),
            "distinct": meta.get("distinct"),
            "id_assignment": meta.get("id_assignment"),
            "existing": _brief_existing(meta.get("existing")),
            "conflicts": meta.get("conflicts", [])[:SAMPLE_ROWS * 5],
            "conflict_count": len(meta.get("conflicts", [])),
            "resolutions": meta.get("resolutions", [])[:SAMPLE_ROWS * 5],
            "first_src": rows[0][SRC] if rows else None,
            "last_src": rows[-1][SRC] if rows else None,
            "samples": [{k: _csv_text(v) for k, v in r.items() if v != ""} for r in rows[:SAMPLE_ROWS]],
        }

    def reconcile(self, left_id: str, right_id: str, left_field: str, right_field: str) -> dict[str, int]:
        """Counts of distinct non-blank values only in left, only in right, in both (exact, trimmed)."""
        return value_overlap(self._get(left_id), self._get(right_id), left_field, right_field)

    # ---- output --------------------------------------------------------------------------
    def rows_for_upload(
        self, stage_id: str, drop_duplicates: bool = False, duplicate_key: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """The rows `write_upload` would write: the staged rows minus later duplicates when asked."""
        rows = self._get(stage_id)
        if not drop_duplicates:
            return list(rows)
        if not duplicate_key:
            raise ValueError("drop_duplicates needs a duplicate_key")
        drop = RowTable.from_rows(rows).later_duplicates(duplicate_key)
        return [r for r in rows if r[SRC] not in drop]

    def write_upload(
        self,
        stage_id: str,
        template: str,
        out_dir: str | Path,
        drop_duplicates: bool = False,
        duplicate_key: list[str] | None = None,
    ) -> Path:
        """Write `<out_dir>/<Template>_Upload.<xlsx|csv>` in exact template header order; never overwrites."""
        rows = self._get(stage_id)
        fields = self._fields(template)
        open_err = self._meta[stage_id].get("conflicts") or []
        if open_err:
            raise ValueError(
                f"{len(open_err)} row(s) still conflict with existing records (e.g. {open_err[0]['src']}: "
                f"{open_err[0]['kind']}); resolve each: exclude as duplicate, exclude as incorrect, or correct"
            )
        extra = [
            f for f in {k for r in rows for k, v in r.items() if v != "" and k != SRC} if f not in fields
        ]
        if extra:
            raise ValueError(f"staged fields not in template {template}: {sorted(extra)}")
        kept = self.rows_for_upload(stage_id, drop_duplicates, duplicate_key)
        return write_rows(template, fields, kept, out_dir, self.templates[template].output)[0]


XLSX_MAX_ROWS = 1_048_576
DATE_FIELD_SUFFIX = "_Date"  # a template field with this ending is a date column
DATE_NUMBER_FORMAT = "yyyy-mm-dd"  # how Excel shows the date cells the writer emits
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _xlsx_value(ws: Any, field_name: str, value: Any) -> Any:
    """An ISO date in a date field is a true Excel date cell; every other value is written as it is."""
    if value == "":
        return None
    if field_name.endswith(DATE_FIELD_SUFFIX) and isinstance(value, str) and _ISO_DATE.match(value.strip()):
        try:
            day = dt.date.fromisoformat(value.strip())
        except ValueError:
            return value
        cell = WriteOnlyCell(ws, value=day)
        cell.number_format = DATE_NUMBER_FORMAT
        return cell
    return value


def _first_sheet(path: str | Path) -> str:
    """The sheet a GL source is read from when none is named: the first one (a CSV has none, so its file stem)."""
    return columnar.first_sheet(path)


def write_rows(
    template: str,
    fields: Sequence[str],
    rows: Iterable[Mapping[str, Any]],
    out_dir: str | Path,
    output: str = "xlsx",
    stem: str = "",
) -> tuple[Path, int]:
    """The one writer of upload files: `<out_dir>/<stem or Template_Upload>.<xlsx|csv>` in exact template header
    order, rows streamed as they come (a write-only workbook or CSV, never the whole table in memory).

    Blank values stay blank. A row holding a field the template does not have is refused. Never overwrites an
    existing file (`_v2`, `_v3`...); a failed write leaves no partial file. Returns (path, rows written).
    """
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    as_csv = output == "csv"
    ext = "csv" if as_csv else "xlsx"
    base = stem or f"{template}_Upload"
    allowed = set(fields)
    n = 1
    while True:
        target = directory / (f"{base}.{ext}" if n == 1 else f"{base}_v{n}.{ext}")
        try:
            fh = open(target, "xb")  # noqa: SIM115  closed below; exclusive create is the no-overwrite guarantee
        except FileExistsError:
            n += 1
            continue
        break
    count = 0
    try:
        with fh:
            if as_csv:
                with io.TextIOWrapper(fh, newline="", encoding="utf-8-sig", write_through=True) as text:
                    writer = csv.writer(text)
                    writer.writerow(fields)
                    for row in rows:
                        _check_row(row, allowed, template)
                        writer.writerow([_csv_text(row.get(f, "")) for f in fields])
                        count += 1
            else:
                book = openpyxl.Workbook(write_only=True)
                ws = book.create_sheet(template)
                ws.append(list(fields))
                for row in rows:
                    _check_row(row, allowed, template)
                    count += 1
                    if count >= XLSX_MAX_ROWS:
                        raise ValueError(
                            f"{template}: more rows than one Excel sheet holds ({XLSX_MAX_ROWS - 1:,}); "
                            "set the template's output to csv"
                        )
                    ws.append([_xlsx_value(ws, f, row.get(f, "")) for f in fields])
                book.save(fh)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target, count


def _check_row(row: Mapping[str, Any], allowed: set[str], template: str) -> None:
    extra = sorted(k for k, v in row.items() if v != "" and k != SRC and k not in allowed)
    if extra:
        raise ValueError(f"staged fields not in template {template}: {extra}")
