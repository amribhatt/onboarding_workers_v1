"""Decision tables: typed inputs, first matching row wins, and the row that fired comes back with the answer.

A table is data (YAML), so a business user can change a rule without a developer, and a change replays the same
way every time. The model never evaluates a table; it may read one to explain a decision.

    id: coa-cascade
    hit: first
    inputs: {historic: bool, pinned: bool, score: number}
    rows:
      - {id: L0, when: {historic: true}, then: {tier: L0, action: apply}}
      - {id: L3, when: {score: ">=0.5"}, then: {tier: L3, action: suggest}}
      - {id: L5, then: {tier: L5, action: needs_you}}        # no `when` matches anything

A condition value is a literal (equality), a comparison string (">=0.5", "<0.5", "!=x"), or {in: [a, b]}.
A sponsor override is a table with the same id: its rows replace the organisation's rows with the same row id in
place, and rows with a new id are tried first.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from worker import rulefiles

_TYPES = {"bool": bool, "number": (int, float), "string": str}
_CMP = re.compile(r"^(>=|<=|!=|>|<|==)\s*(.+)$")


class TableError(ValueError):
    """A table that is malformed or an input that does not fit it."""


@dataclass(frozen=True)
class Decision:
    table: str
    row: str
    output: dict[str, Any]
    why: str


def _infer_outputs(rows: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for r in rows:
        for k, v in (r.get("then") or {}).items():
            out.setdefault(k, {"type": "bool" if isinstance(v, bool) else
                               "number" if isinstance(v, (int, float)) else "string"})
    return out


def _validated(table_id: str, inputs: Mapping[str, str], outputs: Mapping[str, Any], rows: Any) -> None:
    """Run the strict table schema over an in-memory table: every condition, every output value and the catch-all
    row are checked here, when the table is built, never at the first decision."""
    doc = {"schema_version": 1, "kind": "table", "id": table_id, "hit": "first", "inputs": dict(inputs),
           "outputs": dict(outputs), "rows": [dict(r) for r in rows]}
    try:
        text = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
        rulefiles.load_text(f"tables/{table_id}.yaml", text)
    except rulefiles.SchemaError as exc:
        raise TableError(f"table {table_id}: " + "; ".join(f"{p.message} ({p.pointer or '$'})"
                                                          for p in exc.problems)) from exc
    except yaml.YAMLError as exc:
        raise TableError(f"table {table_id}: its rows hold a value a rule file cannot hold ({exc})") from exc


@dataclass(frozen=True)
class Table:
    id: str
    inputs: dict[str, str]
    rows: tuple[dict[str, Any], ...]
    description: str = ""
    hit: str = "first"
    outputs: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, partial: bool = False) -> Table:
        """A table from its parts. Conditions, output values and the catch-all are checked now. With `partial`, the
        rows are an override of another table: they are checked against that table when merged, and `inputs` is
        not required (and, if given, never merged)."""
        for key in ("id", "rows") if partial else ("id", "inputs", "rows"):
            if key not in data:
                raise TableError(f"decision table is missing {key!r}")
        if data.get("hit", "first") != "first":
            raise TableError("only hit policy 'first' is supported")
        inputs = {} if partial else dict(data["inputs"])
        bad = [n for n, t in inputs.items() if t not in _TYPES]
        if bad:
            raise TableError(f"table {data['id']}: unknown input type for {bad}; use bool, number or string")
        rows = tuple(dict(r) for r in data["rows"])
        seen: set[str] = set()
        for r in rows:
            if "id" not in r or "then" not in r:
                raise TableError(f"table {data['id']}: every row needs an id and a then")
            if r["id"] in seen:
                raise TableError(f"table {data['id']}: duplicate row id {r['id']!r}")
            seen.add(r["id"])
            unknown = [k for k in (r.get("when") or {}) if k not in inputs] if not partial else []
            if unknown:
                raise TableError(f"table {data['id']} row {r['id']}: unknown inputs {unknown}")
        outputs = dict(data.get("outputs") or {}) or _infer_outputs(rows)
        if not partial:
            if not rows or rows[-1].get("when"):
                raise TableError(f"table {data['id']}: the last row must match anything (leave its conditions "
                                 "empty): add a catch-all row")
            _validated(str(data["id"]), inputs, outputs, rows)
        return cls(str(data["id"]), inputs, rows, str(data.get("description", "")), "first", outputs)

    @classmethod
    def from_loaded(cls, loaded: rulefiles.Loaded) -> Table:
        """A table from a file the strict loader accepted."""
        d = loaded.data
        return cls(str(d["id"]), dict(d["inputs"]), tuple(dict(r) for r in d["rows"]), str(d.get("description", "")),
                   "first", {k: dict(v) for k, v in d["outputs"].items()})

    @classmethod
    def load(cls, path: str | Path) -> Table:
        """A table from a rule file, read by the strict loader. A refused file raises TableError listing every
        problem."""
        try:
            return cls.from_loaded(rulefiles.load_file(path))
        except rulefiles.SchemaError as exc:
            raise TableError("; ".join(exc.lines)) from exc

    def with_override(self, override: Mapping[str, Any] | Table) -> Table:
        """Rows of `override` replace rows with the same id in place; new ids go first. The organisation's inputs
        and outputs stand: an override's `inputs` are not merged. The merged table is checked in full."""
        other = override if isinstance(override, Table) else Table.from_dict(override, partial=True)
        if other.id != self.id:
            raise TableError(f"override is for table {other.id!r}, not {self.id!r}")
        unknown = sorted({k for r in other.rows for k in (r.get("when") or {}) if k not in self.inputs})
        if unknown:
            raise TableError(f"table {self.id}: unknown inputs {unknown}")
        mine = {r["id"] for r in self.rows}
        by_id = {r["id"]: r for r in other.rows}
        merged = [by_id.get(r["id"], r) for r in self.rows]
        added = [r for r in other.rows if r["id"] not in mine]
        rows = tuple(added + merged)
        outputs = dict(self.outputs) or _infer_outputs(rows)
        _validated(self.id, self.inputs, outputs, rows)
        return Table(self.id, self.inputs, rows, self.description, "first", outputs)

    def decide(self, values: Mapping[str, Any]) -> Decision:
        for name, kind in self.inputs.items():
            if name not in values:
                raise TableError(f"table {self.id}: input {name!r} was not given")
            v = values[name]
            if kind != "bool" and isinstance(v, bool) or not isinstance(v, _TYPES[kind]):
                raise TableError(f"table {self.id}: input {name!r} must be {kind}, got {v!r}")
        for row in self.rows:
            when = row.get("when") or {}
            if all(_holds(values[k], cond) for k, cond in when.items()):
                return Decision(self.id, row["id"], dict(row["then"]), _why(row, values))
        raise TableError(f"table {self.id}: no row matched {dict(values)}; add a catch-all row")


def _holds(value: Any, cond: Any) -> bool:
    if isinstance(cond, Mapping):
        if "in" in cond:
            return value in cond["in"]
        raise TableError(f"unknown condition {dict(cond)!r}")
    if isinstance(cond, str):
        m = _CMP.match(cond.strip())
        if m:
            op, rhs = m.group(1), m.group(2).strip()
            try:
                left, right = float(value), float(rhs)
            except (TypeError, ValueError):
                left, right = str(value), rhs
            return {
                ">=": left >= right, "<=": left <= right, ">": left > right, "<": left < right,
                "!=": left != right, "==": left == right,
            }[op]
    return value == cond


def _why(row: Mapping[str, Any], values: Mapping[str, Any]) -> str:
    when = row.get("when") or {}
    if not when:
        return f"row {row['id']}: nothing earlier matched"
    return f"row {row['id']}: " + ", ".join(f"{k} {c if isinstance(c, str) and _CMP.match(c) else '= ' + str(c)}"
                                            f" (was {values[k]})" for k, c in when.items())
