"""The review of a staged table before approval: findings by level, what was acknowledged, and a reconciliation.

Plain code over the staged rows. The gate (`worker.agent.decide`) refuses an approval while an ERR is open or a
WARN is not acknowledged with a reason; nothing here changes a row.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from worker.checks import ERR, WARN, as_table, conflict_findings, finding, output_findings, stage_findings
from worker.context import RunContext
from worker.setops import RowTable
from worker.templates import TemplateSpec, sponsor_levels


def review(
    ctx: RunContext,
    stage_id: str,
    drop_duplicates: bool = False,
    duplicate_key: list[str] | None = None,
    acknowledged: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The review of one staged table: the stage's rows are loaded once as a table and every check is a query."""
    stage = ctx.stage
    meta = stage.meta(stage_id)

    def batch(target: str) -> Iterable[RowTable]:
        return (RowTable.from_rows(stage.rows(sid)) for sid in stage.stage_ids(target))

    out = review_table(ctx, RowTable.from_rows(stage.rows(stage_id)), meta, stage.templates, drop_duplicates,
                       duplicate_key, acknowledged, batch)
    return {**out, "stage_id": stage_id}


def review_table(
    ctx: Any,
    table: RowTable,
    meta: Mapping[str, Any],
    templates: Mapping[str, TemplateSpec],
    drop_duplicates: bool = False,
    duplicate_key: list[str] | None = None,
    acknowledged: Mapping[str, str] | None = None,
    batch: Callable[[str], Iterable[RowTable]] | None = None,
) -> dict[str, Any]:
    """The review of a staged table given as rows in a `RowTable`, at any size. `batch(record_type)` gives the
    tables of this run that already hold records of that type (what a reference may point at)."""
    ack = {k: str(v).strip() for k, v in (acknowledged or {}).items() if str(v).strip()}
    template = meta["template"]
    spec = templates[template]
    levels = {**spec.levels, **sponsor_levels(ctx.sponsor)}
    kept = table
    if drop_duplicates:
        if not duplicate_key:
            raise ValueError("drop_duplicates needs a duplicate_key")
        kept = table.without(table.later_duplicates(duplicate_key))
    found = stage_findings(template, kept, ctx.gp_id, templates, levels)
    found += quality_findings(ctx, meta)
    found += conflict_findings(meta.get("conflicts") or [], levels)
    if len(kept):
        found += output_findings(template, kept, templates, levels)
        found += dependency_findings(ctx, template, kept, levels, templates, batch)
        found += account_findings(meta.get("account_mapping"), kept, levels)
    for f in found:
        f["acknowledged"] = ack.get(f["id"]) if f["severity"] == WARN else None
    open_err = [f["id"] for f in found if f["severity"] == ERR]
    pending_warn = [f["id"] for f in found if f["severity"] == WARN and not f["acknowledged"]]
    return {
        "stage_id": meta.get("stage_id"),
        "template": template,
        "rows_read": meta.get("input_rows"),
        "rows_out": len(kept),
        "err_count": len(open_err),
        "warn_count": sum(1 for f in found if f["severity"] == WARN),
        "findings": found,
        "open_err": open_err,
        "unacknowledged_warn": pending_warn,
        "can_approve": not open_err and not pending_warn,
        "reconciliation": reconciliation(meta, len(table), len(kept)),
    }


def quality_findings(ctx: RunContext, meta: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What the last data-quality check of the stage's source file found, with where each problem is and the
    marked copy that shows them. A file that was never checked is a WARN: the person says why it is fine."""
    source = meta.get("source_file")
    if not source:
        return []
    report = ctx.quality_reports.get(str(source))
    name = Path(str(source)).name
    if report is None:
        return [finding(
            "quality_not_checked",
            f"{name} was not run through check_data_quality, so its problems are not known.",
            None, count=0, offer={"tool": "check_data_quality"},
        )]
    out = []
    for c in report["causes"]:
        f = finding(
            "data_quality",
            f"{c['message']} ({c['field']}): {c['count']:,} line(s) at {c['locator']['where']}; "
            f"filter: {c['locator']['filter']}. A marked copy shows them: write_quality_files kind marked.",
            None, count=c["count"], field=c["field"], cause=c["id"], rule=c["rule"], ranges=c["ranges_text"],
            locator=c["locator"], offer={"tool": "write_quality_files", "kind": "marked", "cause_id": c["id"]},
        )
        f["severity"] = c["severity"]
        out.append(f)
    return out


def account_findings(
    info: Mapping[str, Any] | None, rows: Sequence[Mapping[str, Any]] | RowTable, levels: Mapping[str, str]
) -> list[dict[str, Any]]:
    """What the map-accounts skill leaves open: blank accounts, disallowed combinations, unknown types."""
    if not info:
        return []
    target = info["target_field"]
    n_blank, blank = as_table(rows).blank(target)
    out: list[dict[str, Any]] = []
    decided = {x["key"]: x for x in info["keys"]}
    blocked = [x for x in decided.values() if x["action"] == "blocked"]
    pending = [x for x in decided.values() if x["action"] in ("suggest", "needs_you")]
    if blocked:
        out.append(finding(
            "account_not_allowed",
            f"{len(blocked)} account key(s) map to an account the transaction type does not allow "
            f"(e.g. {blocked[0]['key']}: {'; '.join(blocked[0]['flags'])}).",
            levels, blank, field=target, count=sum(int(x["rows"]) for x in blocked),
        ))
    if pending:
        out.append(finding(
            "account_unmapped",
            f"{len(pending)} account key(s) have no account yet ({sum(1 for x in pending if x['action'] == 'suggest')}"
            f" suggested, {sum(1 for x in pending if x['action'] == 'needs_you')} need you); "
            f"{n_blank} line(s) have a blank account.",
            levels, blank, field=target, count=n_blank,
        ))
    unknown = [x for x in decided.values() if "transaction type not in the tagging reference" in x["flags"]]
    if unknown:
        out.append(finding(
            "transaction_type_unknown",
            f"{len(unknown)} transaction type(s) are not in the tagging reference "
            f"(e.g. {unknown[0]['transaction_type']!r}).",
            levels, [], field=type_of(info), count=sum(int(x["rows"]) for x in unknown),
        ))
    return out


def type_of(info: Mapping[str, Any]) -> str:
    return str(info.get("type_field", "Transaction_Type"))


def dependency_findings(
    ctx: Any,
    template: str,
    rows: Sequence[Mapping[str, Any]] | RowTable,
    levels: Mapping[str, str],
    templates: Mapping[str, TemplateSpec],
    batch: Callable[[str], Iterable[RowTable]] | None = None,
) -> list[dict[str, Any]]:
    """Every reference to a record that is neither in the sponsor's existing records nor in this batch."""
    table = as_table(rows)
    out: list[dict[str, Any]] = []
    for field, target in templates[template].depends:
        known: set[str] = set()
        registry = getattr(ctx, "registry", None)
        if registry is not None:
            for e in registry.entries(target):
                known.update(str(e.get(k, "")).strip().lower() for k in ("key", "source_key"))
        tspec = templates.get(target)
        ident = list(tspec.key) if tspec else []
        for sibling in (batch(target) if batch is not None else ()):  # this batch: its IDs and the source IDs
            known.update(sibling.values_of([f for f in sibling.fields if f in ident or f.startswith("Source_")]))
        groups, n_rows, head, srcs = table.unknown_values(field, known)
        if not groups:
            continue
        shown = [name for name, _n in head[:5]]
        f = finding(
            "missing_dependency",
            f"{groups} {field} value(s) are not in the {target} the sponsor has or in this batch "
            f"({', '.join(shown)}{', ...' if groups > 5 else ''}). Load {target} first, or add each as an "
            f"existing record with add_existing_record.",
            levels,
            srcs,
            field=field,
            count=n_rows,
        )
        f["missing"] = [{"value": name, "rows": n} for name, n in head]
        f["record_type"] = target
        out.append(f)
    return out


def reconciliation(meta: Mapping[str, Any], staged: int, written: int) -> dict[str, Any]:
    """input = output + collapsed + left out as existing + excluded by the analyst + dropped duplicates."""
    distinct = meta.get("distinct") or {}
    existing = meta.get("existing") or {}
    analyst = sum(1 for r in meta.get("resolutions") or [] if str(r.get("action", "")).startswith("exclude"))
    out = {
        "input_rows": meta.get("input_rows"),
        "collapsed_by_dedup": int(distinct.get("collapsed", 0)) + int(distinct.get("blank_key", 0)),
        "excluded_existing": int(existing.get("already_onboarded", 0)),
        "excluded_by_analyst": analyst,
        "dropped_duplicates": staged - written,
        "output_rows": written,
    }
    if out["input_rows"] is not None:
        explained = sum(out[k] for k in ("collapsed_by_dedup", "excluded_existing", "excluded_by_analyst",
                                         "dropped_duplicates"))
        made = int(meta.get("generated_rows", 0))  # rows the code added (an LP offset), not read from the file
        if made:
            out["generated"] = made
        out["unexplained"] = out["input_rows"] - (written - made) - explained
        out["balanced"] = out["unexplained"] == 0
    return out
