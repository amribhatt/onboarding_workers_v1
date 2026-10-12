"""Agent tools: thin, audited wrappers over the workbook, staging and sandbox modules.

Every tool takes a required `why`. File arguments go through `guard.check_path`; a refusal or error is
audited and returned to the model as text. Responses are capped near 6 kB; no tool returns full tables.
`build_tools` owns the audited wrapper (`run`); the tool families live in `tools_*.py` and are given a `Kit`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from worker.audit import AuditError
from worker.checks import stage_warnings
from worker.context import RunContext
from worker.guard import PathRefused, check_path
from worker.inbox import list_inbox
from worker.jobstate import JobState
from worker.memory import RULE_ID_PATTERN
from worker.tools_engine import engine_tools
from worker.tools_flex import flex_tools, memory_tools
from worker.tools_kit import Kit, UnknownFile, dump
from worker.tools_records import accounts_tools, recipe_tools, records_tools
from worker.tools_stage import quality_tools, stage_tools

MAX_REPEAT_FAILURES = 3
ERROR_NUDGE_AFTER = 8
REPEAT_MSG = (
    "STOP: this exact call has failed 3 times. Do not repeat it. Change the approach, or tell the human "
    "what is blocking you and ask."
)
BUDGET_MSG = (
    "STOP: the tool-call budget for this request ({n} calls) is used up. Summarise what is done, what is "
    "MISSING and what you were about to do, then wait for the human."
)
NUDGE_MSG = (
    "NOTE: many calls have failed in this run. Stop trying variations; say plainly what is blocking you "
    "and ask the human."
)

# Migration M21: why each stage tool stays registered (tests/test_tool_list.py keeps its own copy and compares).
# A stage tool leaves the list only when the engine says the same thing for the same unit.
KEPT_STAGE_TOOLS: dict[str, str] = {
    "lift": "`write_upload` takes a stage_id and only a lift makes one; `run_job` by file lifts a flat table only",
    "lift_blocks": "block layouts have no `file_to_spec` step, so `run_job` by file cannot read them",
    "lift_gl_lines": "stages a GL file; the golden lock's agent chains name it and `write_upload` needs its stage",
    "flatten_compact": "compact layouts have no `file_to_spec` step, so `run_job` by file cannot read them",
    "join_stages": "the engine cannot say a join of another record type (`agentspec.NotTranslatable`)",
    "map_accounts": "records the account picks that `write_upload` and the golden lock's chains are built from",
    "check_data_quality": "records the check the upload's spec is built from; the lock's chains name it",
    "write_quality_files": "the engine writes no quality files; they are made from the staged check",
    "assign_ids": "the ID reservation is made on the staged table; the engine cannot know `existing_ids`",
    "resolve_references": "swaps source IDs on the staged table; the lock's static chains name it",
    "match_existing": "`run_job` by file does not match existing records; the staged chain does",
    "resolve_conflicts": "the engine's exclude choice needs a reason that a staged edit chain does not keep; the "
                         "old writer still writes these tables",
    "find_recipe": "reads the sponsor's saved workflows (the Mapper's read-only tool as well)",
    "apply_recipe": "replays a saved workflow as staged steps; there is no engine form of it",
    "coverage_report": "the service shows its table in the run view; the digest has no per-field coverage",
    "distinct_rows": "distinct keys of a staged table, as cell text, for the review",
}


def build_tools(
    ctx: RunContext, *, role_tools: bool = False, manifest_in_record: bool | None = None,
    explainer: Any = None, sandbox: Any = None, layout_roles: Any = None,
) -> list[BaseTool]:
    """The Deep Agent's tools. `role_tools=True` adds the two read-only role tools (`propose_binding`,
    `ask_question`, migration M14); no runtime path asks for them until the adapters switch (W4).

    `explainer` is a `roles.RoleRunner`: with it, `run_job` hands each non-PASS cause (not each row) to the
    Explainer and shows the verified explanation beside the finding (migration M21). Without it nothing is asked.
    `KEPT_STAGE_TOOLS` says why each stage tool is registered; `duplicates` and `reconcile` are read-only.
    `layout_roles` is the `roles.RoleRunner` the Layout Drafter runs on (tests pass a scripted one).

    The job's manifest, the approver and the approval id go in the run's own `write_upload` record by default (M19;
    the golden lock's decision digest was regenerated for it). `manifest_in_record=False` (or
    `WORKER_AUDIT_MANIFEST=0`) leaves them only on the engine's log (`<run>/engine/engine_audit.jsonl`).
    """
    if manifest_in_record is None:
        manifest_in_record = os.environ.get("WORKER_AUDIT_MANIFEST", "1").strip().lower() in ("1", "true", "yes")
    state = JobState(ctx.out_dir)  # what survives a restart: see worker.jobstate
    if ctx.restore:
        state.restore(ctx)
    failures: dict[str, int] = {}

    def resolve(path: str) -> Path:
        p = Path(path)
        if not p.is_absolute():
            for root in ctx.inbox_roots:
                if (Path(root) / p).exists():
                    return check_path(Path(root) / p, ctx.inbox_roots)
            if ctx.inbox_roots:
                check_path(Path(ctx.inbox_roots[0]) / p, ctx.inbox_roots)  # refusals win over "unknown"
            names = ", ".join(f["name"] for f in list_inbox(ctx.inbox_roots, 40)) or "(none)"
            raise UnknownFile(f"no file named {path!r} in the inbox. Available names: {names}")
        return check_path(p, ctx.inbox_roots)

    def cite(action: str, why: str) -> None:
        """Audit an approved rule id named in `why`, so an applied rule leaves its own record."""
        if ctx.memory is None:
            return
        active = ctx.memory.active_ids()
        for rid in dict.fromkeys(RULE_ID_PATTERN.findall(why)):
            if rid in active:
                ctx.audit.record("rule.cited", why, {"rule_id": rid, "action": action})

    def summary(sid: str) -> dict[str, Any]:
        """Stage summary plus deterministic warnings (e.g. GP_ID lifted from a source column)."""
        out = ctx.stage.summary(sid)
        found = stage_warnings(out["template"], ctx.stage.rows(sid), ctx.gp_id, ctx.stage.templates)
        if found:
            out["warnings"] = found
        return out

    def run(
        action: str, why: str, inputs: dict[str, Any], fn: Callable[[], Any], paths: list[str] | None = None,
        render: Callable[[Any], str] = dump,
    ) -> str:
        if not str(why).strip():
            return "ERROR: `why` is required and must not be empty."
        key = f"{action}:{json.dumps(inputs, sort_keys=True, default=str)}"
        if failures.get(key, 0) >= MAX_REPEAT_FAILURES:
            return _blocked(action, why, inputs, REPEAT_MSG)
        if ctx.tool_calls >= ctx.max_tool_calls:
            return _blocked(action, why, inputs, BUDGET_MSG.format(n=ctx.max_tool_calls))
        ctx.tool_calls += 1
        out = _execute(action, why, inputs, fn, paths, render)
        state.save(ctx)
        if out.startswith(("ERROR", "REFUSED")):
            failures[key] = failures.get(key, 0) + 1
            ctx.tool_errors += 1
            if ctx.tool_errors >= ERROR_NUDGE_AFTER:
                out += "\n" + NUDGE_MSG
        return out

    def _audited(event: str, why: str, inputs: dict[str, Any], outputs: dict[str, Any]) -> str | None:
        """Write the record; the error text when the record cannot be written (the action then does not happen)."""
        try:
            ctx.audit.record(event, why, inputs, outputs)
        except AuditError as exc:
            return f"ERROR: audit unavailable, action not performed: {exc}"
        return None

    def _blocked(action: str, why: str, inputs: dict[str, Any], message: str) -> str:
        return _audited(f"{action}.blocked", why, inputs, {"reason": message[:200]}) or message

    def _execute(
        action: str, why: str, inputs: dict[str, Any], fn: Callable[[], Any], paths: list[str] | None,
        render: Callable[[Any], str],
    ) -> str:
        try:
            for p in paths or []:
                resolve(p)
        except PathRefused as exc:
            return _audited(f"{action}.refused", why, inputs, {"reason": str(exc)}) or f"REFUSED: {exc}"
        except UnknownFile as exc:
            return _audited(f"{action}.error", why, inputs, {"error": f"ERROR: {exc}"[:500]}) or f"ERROR: {exc}"
        error: str | None = None
        result: Any = None
        try:
            result = fn()
        except PathRefused as exc:
            error = f"REFUSED: {exc}"
        except Exception as exc:  # noqa: BLE001  returned to the model, not raised
            error = f"ERROR: {type(exc).__name__}: {exc}"
        try:
            if error is not None:
                ctx.audit.record(f"{action}.error", why, inputs, {"error": error[:500]})
                return error
            text = render(result)
            summary: dict[str, Any] = {"chars": len(text)}
            if isinstance(result, dict):
                scalars = {k: v for k, v in result.items() if isinstance(v, int | str | bool)}
                summary.update({k: v for k, v in scalars.items() if k != "stdout" and not k.startswith("_")})
                summary.update(result.get("_audit") or {})  # what the record keeps and the model is not shown
            ctx.audit.record(action, why, inputs, summary)
            cite(action, why)
            return text
        except AuditError as exc:
            return f"ERROR: audit unavailable: {exc}"

    kit = Kit(ctx, run, resolve, summary, state, sandbox, explainer, manifest_in_record, layout_roles)
    families = (stage_tools, engine_tools, flex_tools, memory_tools, quality_tools, accounts_tools, recipe_tools,
                records_tools)
    by_name = {t.name: t for family in families for t in family(kit)}
    tools: list[BaseTool] = [by_name[n] for n in (
        "list_files", "list_sheets", "read_range", "look", "find", "column_profile", "lift", "lift_blocks",
        "lift_gl_lines", "flatten_compact", "join_stages", "coverage_report", "distinct_rows", "duplicates",
        "reconcile", "ask_job", "propose_layout_program", "approve_layout_program", "note_correction",
        "check_data_quality", "supply_value", "write_quality_files", "use_skill", "map_accounts",
        "confirm_account_mappings", "find_recipe", "apply_recipe", "match_existing", "resolve_conflicts",
        "import_existing_records", "add_existing_record", "assign_ids", "resolve_references", "set_id_scheme",
        "write_upload", "propose_rule", "run_job",
    )]
    if role_tools:
        tools += [by_name["propose_binding"], by_name["ask_question"]]
    return tools
