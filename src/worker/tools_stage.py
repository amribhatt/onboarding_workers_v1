"""Tools that look at a file, stage a table from it and check it: the file tools, the lifts, the joins, coverage,
and the data-quality scan with the human's supplied values and the marked files."""

from __future__ import annotations

import re
from typing import Any

from langchain_core.tools import BaseTool, tool

from worker import persist, quality
from worker import workbook as wb
from worker.inbox import list_inbox
from worker.templates import build_coverage, sponsor_levels
from worker.tools_kit import Kit, cap, derive_stage, norm_text


def stage_tools(k: Kit) -> list[BaseTool]:
    ctx, run, resolve, summary = k.ctx, k.run, k.resolve, k.summary
    constants_by_stage, specs_by_stage = k.state.constants, k.state.specs

    @tool
    def list_files(why: str) -> str:
        """List the files in the inbox (names relative to the inbox, bytes, kind). Use these names in other tools."""
        def go() -> Any:
            files = list_inbox(ctx.inbox_roots)
            return {"count": len(files), "files": files}

        return run("list_files", why, {}, go)

    @tool
    def list_sheets(path: str, why: str) -> str:
        """Describe a file: its kind and, for workbooks, each sheet (dimensions, hidden, merged, pivot)."""
        return run("list_sheets", why, {"path": path}, lambda: wb.list_sheets(resolve(path)), [path])

    @tool
    def read_range(path: str, sheet: str, a1_range: str, why: str) -> str:
        """Non-empty cells (value, formula, indent, bold) of an A1 range, capped."""
        return run("read_range", why, {"path": path, "sheet": sheet, "range": a1_range},
                   lambda: wb.read_range(resolve(path), sheet, a1_range, max_cells=150), [path])

    @tool
    def look(
        path: str, sheet: str, why: str, first_row: int = 1, last_row: int = 40, first_col: int = 1,
        last_col: int = 12,
    ) -> str:
        """A text grid of a window of the sheet, as a person would see it."""
        inputs = {"path": path, "sheet": sheet, "rows": [first_row, last_row], "cols": [first_col, last_col]}
        return run("look", why, inputs,
                   lambda: wb.look(resolve(path), sheet, first_row, last_row, first_col, last_col), [path])

    @tool
    def find(path: str, sheet: str, text: str, why: str, limit: int = 20) -> str:
        """Find cells containing `text` (case-insensitive) on a sheet."""
        return run("find", why, {"path": path, "sheet": sheet, "text": text},
                   lambda: wb.find(resolve(path), sheet, text, limit), [path])

    @tool
    def column_profile(
        path: str, sheet: str, col: str, why: str, first_row: int = 1, last_row: int | None = None
    ) -> str:
        """Count, blanks, distinct values, five samples and the likely shape of one column."""
        return run("column_profile", why,
                   {"path": path, "sheet": sheet, "col": col, "first_row": first_row, "last_row": last_row},
                   lambda: wb.column_profile(resolve(path), sheet, col, first_row, last_row), [path])

    @tool
    def lift(
        path: str, sheet: str, template: str, columns: dict[str, str], header_row: int | None, first_row: int,
        why: str, last_row: int | None = None, constants: dict[str, str] | None = None,
    ) -> str:
        """Copy source columns ({template field: column letter}) into a staged table; returns its summary."""
        consts = dict(constants or {})

        def go() -> Any:
            sid = ctx.stage.lift(
                resolve(path), sheet, columns, header_row, first_row, last_row, consts, template=template
            )
            constants_by_stage[sid] = consts
            specs_by_stage[sid] = {
                "kind": "lift", "path": str(resolve(path)), "sheet": sheet, "columns": dict(columns),
                "header_row": header_row, "first_row": first_row, "last_row": last_row, "constants": consts,
            }
            return summary(sid)

        inputs = {"path": path, "sheet": sheet, "template": template, "columns": columns,
                  "header_row": header_row, "first_row": first_row, "last_row": last_row, "constants": consts}
        return run("lift", why, inputs, go, [path])

    @tool
    def lift_blocks(
        path: str, sheet: str, template: str, columns: dict[str, str], blocks: list[list[int | None]],
        why: str, constants: dict[str, str] | None = None,
    ) -> str:
        """Like lift for stacked tables; each block is [header_row, first_row, last_row_or_null]."""
        consts = dict(constants or {})

        def go() -> Any:
            parsed = [(b[0], int(b[1] or 1), b[2]) for b in blocks]
            sid = ctx.stage.lift_blocks(resolve(path), sheet, columns, parsed, consts, template=template)
            constants_by_stage[sid] = consts
            specs_by_stage[sid] = {
                "kind": "lift_blocks", "path": str(resolve(path)), "sheet": sheet, "columns": dict(columns),
                "blocks": [list(b) for b in blocks], "constants": consts,
            }
            return summary(sid)

        inputs = {"path": path, "sheet": sheet, "template": template, "columns": columns, "blocks": blocks,
                  "constants": consts}
        return run("lift_blocks", why, inputs, go, [path])

    @tool
    def lift_gl_lines(
        path: str, why: str, sheet: str = "", fund_id: str = "", fund_name: str = "", header_row: int = 1,
        columns: dict[str, str] | None = None, offsets: bool = False,
    ) -> str:
        """Stage the lines of a GL source file as GL rows, all in code: Debit and Credit become one signed
        Total_Amount, Event_ID is Journal-Line, GP_ID is the run's, investors are looked up in the sponsor's
        existing records. Account_Number stays blank until map_accounts. `columns` maps line parts (j, ln, acct,
        an, tt, dr, cr, desc, inv, date) to the file's header text where it is not the standard one. `offsets`
        adds the offsetting line of every investor line (GL LP). Never type amounts yourself: use this tool."""
        def go() -> Any:
            from worker.glio import read_gl_lines

            file = resolve(path)
            names = dict(columns or {})
            known: dict[str, tuple[str, str]] = {}
            if ctx.registry is not None:
                for e in ctx.registry.entries("Investors"):
                    ident, label = str(e.get("key") or e.get("name") or ""), str(e.get("name", ""))
                    for ref in (ident, str(e.get("source_key") or e.get("key") or e.get("name") or ""), label):
                        if ref:
                            known.setdefault(ref, (ident, label))
            referenced = {ln.inv for _n, ln in read_gl_lines(file, sheet or None, names, header_row) if ln.inv}
            investors = {r: known[r] for r in sorted(referenced) if r in known}
            sid = ctx.stage.lift_gl_lines(
                file, sheet or None, names, header_row, gp_id=ctx.gp_id, fund_id=fund_id, fund_name=fund_name,
                investors=investors, offsets=offsets,
            )
            constants_by_stage[sid] = {"GP_ID": ctx.gp_id}
            specs_by_stage[sid] = {
                "kind": "gl_lines", "path": str(file), "sheet": ctx.stage.meta(sid)["sheet"], "columns": names,
                "header_row": header_row, "gp_id": ctx.gp_id, "fund_id": fund_id, "fund_name": fund_name,
                "investors": {k: list(v) for k, v in investors.items()}, "offsets": offsets,
            }
            return summary(sid)

        inputs = {"path": path, "sheet": sheet, "fund_id": fund_id, "fund_name": fund_name,
                  "header_row": header_row, "columns": columns or {}, "offsets": offsets}
        return run("lift_gl_lines", why, inputs, go, [path])

    @tool
    def flatten_compact(
        path: str, sheet: str, template: str, levels: dict[str, str], first_row: int, why: str,
        last_row: int | None = None, column: str = "A",
    ) -> str:
        """Flatten a compact pivot: indent level N (as a string key) fills template field `levels[N]`."""
        def go() -> Any:
            sid = ctx.stage.flatten_compact(
                resolve(path), sheet, {int(k): v for k, v in levels.items()}, first_row,
                template=template, last_row=last_row, column=column,
            )
            constants_by_stage[sid] = {}
            specs_by_stage[sid] = {
                "kind": "flatten_compact", "path": str(resolve(path)), "sheet": sheet, "levels": dict(levels),
                "first_row": first_row, "last_row": last_row, "column": column,
            }
            return summary(sid)

        inputs = {"path": path, "sheet": sheet, "template": template, "levels": levels, "first_row": first_row}
        return run("flatten_compact", why, inputs, go, [path])

    @tool
    def join_stages(
        left_stage_id: str, right_stage_id: str, left_key: str, right_key: str, take: list[str], why: str,
        how: str = "left",
    ) -> str:
        """Join two staged tables on a key: bring the `take` fields of the right table onto left rows with
        the same key (for side-by-side blocks listed in a different order). how = "left" | "outer".
        Returns the new stage with matched / left_only / right_only / conflicts counts."""
        def go() -> Any:
            left_spec = specs_by_stage.get(left_stage_id)
            right_spec = specs_by_stage.get(right_stage_id)
            sid = ctx.stage.join(left_stage_id, right_stage_id, left_key, right_key, take, how)
            constants_by_stage[sid] = {
                **constants_by_stage.get(right_stage_id, {}),
                **constants_by_stage.get(left_stage_id, {}),
            }
            if left_spec is not None and right_spec is not None:
                specs_by_stage[sid] = {
                    "kind": "join", "path": left_spec["path"], "left": left_spec, "right": right_spec,
                    "right_template": ctx.stage.summary(right_stage_id)["template"],
                    "left_key": left_key, "right_key": right_key, "take": list(take), "how": how,
                }
            return summary(sid)

        inputs = {"left": left_stage_id, "right": right_stage_id, "keys": [left_key, right_key], "take": take,
                  "how": how}
        return run("join_stages", why, inputs, go)

    @tool
    def distinct_rows(stage_id: str, key_fields: list[str], why: str) -> str:
        """Reduce a staged table to one row per distinct key, first-seen order. Use it to build an entity list
        (investors, vendors, affiliates) out of a transaction file or ledger that repeats them on every line.
        Rows with a blank key are not entities and are counted. Returns the new stage with source_rows / kept /
        collapsed / blank_key / conflicts (one ID under two different names is a conflict, never merged)."""
        def go() -> Any:
            sid = ctx.stage.distinct(stage_id, key_fields)
            derive_stage(k, stage_id, sid, "distinct", key_fields=list(key_fields))
            return summary(sid)

        return run("distinct_rows", why, {"stage_id": stage_id, "key_fields": key_fields}, go)

    @tool
    def coverage_report(
        stage_id: str, why: str, assumed: dict[str, str] | None = None,
        value_checks: dict[str, str] | None = None,
    ) -> str:
        """Per-field coverage of a staged table against its template, as markdown (MISSING first)."""
        def go() -> str:
            meta = ctx.stage._meta[stage_id]
            rows = ctx.stage._get(stage_id)
            template = meta["template"]
            staged = {f: [r.get(f, "") for r in rows] for f in rows[0] if f != "_src"} if rows else {}
            sources = dict(meta.get("sources", {}))
            assumed_all = {f: f"constant supplied: {v}" for f, v in constants_by_stage.get(stage_id, {}).items()}
            assumed_all.update(assumed or {})
            assumed_all = {f: n for f, n in assumed_all.items() if f not in sources}
            return build_coverage(
                template, staged, sources, assumed_all, value_checks or {}, ctx.stage.templates
            ).to_markdown()

        return run("coverage_report", why, {"stage_id": stage_id}, go, render=cap)

    return [list_files, list_sheets, read_range, look, find, column_profile, lift, lift_blocks, lift_gl_lines,
            flatten_compact, join_stages, coverage_report, distinct_rows]


def _quality_references(k: Kit, record_type: str) -> dict[str, set[str]]:
    ctx = k.ctx
    refs: dict[str, set[str]] = {"investors": set(), "existing": set(), "coa_sources": set()}
    if ctx.registry is not None:
        for e in ctx.registry.entries("Investors"):
            refs["investors"].update({norm_text(e.get("key", "")), norm_text(e.get("source_key", ""))} - {""})
        for e in ctx.registry.entries(record_type):
            refs["existing"].add(norm_text(e.get("key", "")))
    if ctx.accounts_root is not None:
        for store_entries in (
            persist.accounts(ctx.accounts_root, ctx.sponsor).entries(),
            persist.golden(ctx.accounts_root).current(),
        ):
            for key in store_entries:
                refs["coa_sources"].add(norm_text(key.split("|")[1] if key.count("|") >= 2 else key))
    return refs


def quality_tools(k: Kit) -> list[BaseTool]:
    ctx, run, resolve = k.ctx, k.run, k.resolve
    quality_supplied = k.state.supplied
    quality_reports = ctx.quality_reports  # shared with the gate: an open ERR here refuses the approval

    @tool
    def check_data_quality(
        path: str, record_type: str, columns: dict[str, str], why: str, sheet: str = "", header_row: int = 1,
        generated: list[str] | None = None,
    ) -> str:
        """Find every problem in a source file for any flow, streaming the file (skill check-data-quality).
        `columns` maps each field (a template field, or for GL: Account, Deal, Journal, Debit, Credit) to the
        file's header text. Returns one cause per rule and field with severity, count, the row ranges and the
        Excel filter that finds the rows, plus a breakdown when several values are involved. Copy the ranges and
        filters exactly; never write them yourself. `generated` lists ID fields the worker will fill (not errors)."""
        def go() -> Any:
            file = resolve(path)
            spec = ctx.stage.templates.get(record_type)
            groups = quality.groups_from_spec(spec) if spec is not None else {}
            if spec is not None:  # a constant from setup (GP_ID) is not a column of the file
                groups["required"] = [f for f in groups["required"] if f != "GP_ID"]
                if record_type == "GL":  # code makes the GL's template fields; the file is checked by its columns
                    groups["required"] = []
            levels = {**(spec.levels if spec else {}), **sponsor_levels(ctx.sponsor)}
            report = quality.scan(
                file, quality.load_rules(), columns=columns, record_type=record_type, sheet=sheet or None,
                header_row=header_row, groups=groups, references=_quality_references(k, record_type),
                levels=levels, supplied=quality_supplied.rows, supplied_all=quality_supplied.all,
                default_breakdown="Account" if record_type == "GL" else "", generated=generated or [],
            )
            report["_call"] = {"record_type": record_type, "columns": dict(columns), "sheet": sheet or "",
                               "header_row": header_row, "generated": list(generated or [])}
            quality_reports[str(file)] = report
            slim = [
                {key: c[key] for key in ("id", "rule", "family", "severity", "field", "message", "fix", "count",
                                         "ranges_text", "suggestion", "copy_values")}
                | {"where": c["locator"]["where"], "filter": c["locator"]["filter"],
                   "breakdown": [{"value": b["value"], "count": b["count"], "ranges": b["ranges_text"],
                                  "filter": b["filter"], "copy_filter": b["copy_filter"]}
                                 for b in (c["breakdown"] or [])][:20],
                   "sample_rows": c["sample_rows"][:5]}
                for c in report["causes"]
            ]
            if report["causes"]:
                ctx.audit.record_many("quality.cause", [
                    (f"{c['family']}: {c['message']}", {"cause": c["id"], "file": file.name},
                     {"severity": c["severity"], "count": c["count"], "ranges": c["ranges_text"]})
                    for c in report["causes"]
                ])
            return {"file": report["file"], "sheet": report["sheet"], "rows": report["rows"], "err": report["err"],
                    "warn": report["warn"], "causes": slim}

        return run("check_data_quality", why, {"path": path, "record_type": record_type, "columns": columns},
                   go, [path])

    @tool
    def supply_value(path: str, field: str, value: str, why: str, row: int = 0, rule_id: str = "required_blank") -> str:
        """Record a value the human typed for a blank (one source `row`, or every blank of the cause when `row`
        is 0). Needs human approval. It is kept as supplied by hand, never inferred, and the check re-runs.
        Only pass a value the human gave you; never a guess and never a suggestion they did not accept."""
        def go() -> Any:
            file = resolve(path)
            if row:
                quality_supplied.set_row(row, field, value, who="human via agent", reason=why)
            else:
                quality_supplied.set_all(rule_id, field, value, who="human via agent", reason=why)
            ctx.audit.record(
                "value.supplied",
                f"supplied by hand, never inferred: {why}",
                {"file": file.name, "field": field, "row": row or "all blank rows", "rule": rule_id},
                {"value": value, "inferred": False},
            )
            return {"recorded": True, "field": field, "scope": f"row {row}" if row else "all blank rows",
                    "next": "call check_data_quality again; the check runs with this value"}

        return run("supply_value", why, {"path": path, "field": field, "value": value, "row": row}, go, [path])

    @tool
    def write_quality_files(
        path: str, kind: str, why: str, cause_id: str = "", breakdown_field: str = "", breakdown_value: str = ""
    ) -> str:
        """Write a file that shows the problems of the last check_data_quality run on `path`: kind `marked`
        (the file untouched, plus a copy with a "Worker check" column, affected rows highlighted, filter on) or
        `rows` (only the affected rows, each with its original row number). Optionally limit it to one cause
        and/or one breakdown value (e.g. one GL account). Streams; safe on a million rows."""
        def go() -> Any:
            file = resolve(path)
            report = quality_reports.get(str(file))
            if report is None:
                raise ValueError("run check_data_quality on this file first")
            if kind not in ("marked", "rows"):
                raise ValueError("kind must be 'marked' or 'rows'")
            only = (breakdown_field, breakdown_value) if breakdown_value else None
            ids = [cause_id] if cause_id else None
            suffix = f"_{re.sub(r'[^A-Za-z0-9_-]', '', breakdown_value)}" if breakdown_value else ""
            stem, ext = file.stem, (".csv" if file.suffix.lower() == ".csv" else ".xlsx")
            target = ctx.out_dir / (f"{stem}_marked{suffix}{ext}" if kind == "marked" else f"{stem}_just_rows{suffix}{ext}")
            ctx.out_dir.mkdir(parents=True, exist_ok=True)
            writer = quality.marked_copy if kind == "marked" else quality.just_these_rows
            out = writer(report, target, cause_ids=ids, only=only)
            return {"file": out.name, "path": str(out), "kind": kind, "original_untouched": True}

        return run("write_quality_files", why, {"path": path, "kind": kind, "cause_id": cause_id,
                                                "breakdown_value": breakdown_value}, go, [path])

    return [check_data_quality, supply_value, write_quality_files]
