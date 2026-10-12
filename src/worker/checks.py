"""Deterministic checks on staged rows. They flag loudly and classify (ERR / WARN); they never change a value.

Levels: ERR blocks the approval until it is resolved, WARN needs an explicit acknowledgement with a reason. The
default level of each check is in `DEFAULT_LEVELS`; a template sidecar (`"levels"`) sets the organisation's, and a
sponsor's `levels.json` overrides it for that sponsor (see `worker.templates.check_levels`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from worker.rulefiles import SchemaError
from worker.setops import RowTable
from worker.skills import find_skill
from worker.templates import TEMPLATES, TemplateSpec

ERR, WARN = "ERR", "WARN"
DEFAULT_LEVELS: dict[str, str] = {
    "no_rows": ERR,
    "gp_id_differs": WARN,
    "identical_columns": WARN,
    "id_too_long": ERR,
    "duplicate_id": ERR,
    "required_blank": WARN,
    "conflict": ERR,
    "missing_dependency": ERR,
    "account_unmapped": ERR,
    "account_not_allowed": ERR,
    "transaction_type_unknown": WARN,
    "data_quality": ERR,
    "quality_not_checked": WARN,
}


# the codes whose floor is a control of the map-accounts skill (skills/map-accounts/controls.yaml)
_CONTROLLED = frozenset({"account_unmapped", "account_not_allowed", "transaction_type_unknown"})


def stricter(a: str, b: str) -> str:
    """The stricter of two levels: ERR over WARN. A level that is not one of the two never wins."""
    return ERR if ERR in (a, b) else (a if a == WARN else b)


def control_floors() -> dict[str, str]:
    """The severity floor of each code that a skill's `controls.yaml` holds (map-accounts), else the code's own
    default. A sponsor's levels may raise a level above its floor, never below it."""
    floors = {c: lv for c, lv in DEFAULT_LEVELS.items()}
    try:
        skill = find_skill("map-accounts")
        floors.update(skill.controls().get("severity_floor", {}) if skill is not None else {})
    except (SchemaError, ValueError, OSError):
        pass  # a refused skill never loosens: the defaults in this file stand (the job does not start anyway)
    return floors


def _level(code: str, levels: Mapping[str, str] | None) -> str:
    chosen = (levels or {}).get(code) or DEFAULT_LEVELS[code]
    floor = control_floors().get(code) if code in _CONTROLLED else None
    return stricter(chosen, floor) if floor else chosen


def finding(code: str, message: str, levels: Mapping[str, str] | None, rows: Sequence[str] = (), **extra: Any) -> dict[str, Any]:
    digest = hashlib.sha256(f"{code}|{extra.get('field', '')}|{message}".encode()).hexdigest()[:8]
    return {
        "id": f"{code}:{digest}",
        "code": code,
        "severity": _level(code, levels),
        "message": message,
        "rows": list(rows)[:50],
        "count": extra.pop("count", len(rows)),
        **extra,
    }


def as_table(rows: Sequence[Mapping[str, Any]] | RowTable) -> RowTable:
    """The rows as a `RowTable` (a list of dicts is loaded once; a table passes through)."""
    return rows if isinstance(rows, RowTable) else RowTable.from_rows(rows)


def stage_findings(
    template: str,
    rows: Sequence[Mapping[str, Any]] | RowTable,
    gp_id: str,
    specs: Mapping[str, TemplateSpec] | None = None,
    levels: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Structured findings on staged rows; empty when nothing looks wrong. Every question is one query over the
    table, so the cost is the same for a hundred rows and for a million."""
    spec = (specs or TEMPLATES).get(template)
    table = as_table(rows)
    n = len(table)
    if not n:
        return [finding("no_rows", "no rows were staged", levels)]
    enabled = set(spec.checks) if spec is not None and spec.checks else None

    def on(code: str) -> bool:
        return enabled is None or code in enabled

    out: list[dict[str, Any]] = []
    if on("gp_id_differs") and spec is not None and "GP_ID" in spec.fields and gp_id:
        count, first = table.differs_from("GP_ID", gp_id)
        if count:
            example = table.first_value("GP_ID", f"lower(pystrip({table.col('GP_ID')})) NOT IN ('', ?)",
                                        (gp_id.lower(),)).strip()
            out.append(
                finding(
                    "gp_id_differs",
                    f"GP_ID differs from this run's GP_ID {gp_id!r} in {count} of {n} rows "
                    f"(e.g. {example!r}). GP_ID is normally the run constant, not a source column.",
                    levels, first, field="GP_ID", count=count,
                )
            )
    if on("id_too_long") and spec is not None and spec.id_max_len:
        for name in (f for f in spec.fields if f.endswith("_ID")):
            long = f"length(pystrip({table.col(name)})) > ?"
            count, first = table.rows_where(long, (spec.id_max_len,))
            if count:
                example = table.first_value(name, long, (spec.id_max_len,)).strip()
                out.append(
                    finding(
                        "id_too_long",
                        f"{count} {name} value(s) are longer than the {spec.id_max_len} characters the target "
                        f"allows (e.g. {example!r} at {first[0]}). Never truncated; correct them.",
                        levels, first, field=name, count=count,
                    )
                )
    if on("identical_columns"):
        fields = [f for f in (spec.fields if spec else table.fields) if f != "_src"]
        for a, b in table.identical_columns(fields):
            out.append(
                finding(
                    "identical_columns",
                    f"{a} and {b} are identical in all {n} rows. A copied column is not a "
                    "separate source value; confirm the source really has both.",
                    levels, field=f"{a}+{b}", count=n,
                )
            )
    return out


def output_findings(
    template: str,
    rows: Sequence[Mapping[str, Any]] | RowTable,
    specs: Mapping[str, TemplateSpec] | None = None,
    levels: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Re-checks on the rows that would be written: ID uniqueness and required fields."""
    spec = (specs or TEMPLATES).get(template)
    out: list[dict[str, Any]] = []
    table = as_table(rows)
    n = len(table)
    if spec is None or not n:
        return out
    if spec.key:
        groups, bad, first = table.duplicate_ids(spec.key)
        if groups:
            out.append(
                finding(
                    "duplicate_id",
                    f"{groups} {' + '.join(spec.key)} value(s) appear on more than one row ({bad} rows); "
                    "each ID must be unique in the file.",
                    levels, first, field="+".join(spec.key), count=bad,
                )
            )
    for name in spec.required:
        count, first = table.blank(name)
        if count:
            out.append(
                finding(
                    "required_blank",
                    f"{name} is required and blank in {count} of {n} rows.",
                    levels, first, field=name, count=count,
                )
            )
    return out


def conflict_findings(conflicts: Sequence[Mapping[str, Any]], levels: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """One finding per kind of open existing-record conflict, listing its rows."""
    by_kind: dict[str, list[Mapping[str, Any]]] = {}
    for c in conflicts:
        by_kind.setdefault(str(c.get("kind", "conflict")), []).append(c)
    labels = {
        "same_key_other_name": "has the same key as an existing record under a different name",
        "same_name_other_key": "has the same name as an existing record under a different key",
        "probable_merge": "is a probable spelling variant of an existing name",
    }
    return [
        finding(
            "conflict",
            f"{len(items)} row(s) {labels.get(kind, 'conflict with existing records')} "
            f"(e.g. {items[0].get('key') or items[0].get('name')!r}).",
            levels,
            [str(c["src"]) for c in items],
            field=kind,
        )
        for kind, items in by_kind.items()
    ]


def stage_warnings(
    template: str,
    rows: Sequence[Mapping[str, Any]] | RowTable,
    gp_id: str,
    specs: Mapping[str, TemplateSpec] | None = None,
) -> list[str]:
    """Findings a reviewer must see before approving, as plain lines. ERR-level ones start with `ERR:`."""
    return [
        f"{f['severity']}: {f['message']}" if f["severity"] == ERR else f["message"]
        for f in stage_findings(template, rows, gp_id, specs)
    ]
