"""Tools that run the engine over a unit: the read-only digest and analyses, and the gated upload write."""

from __future__ import annotations

import dataclasses
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from worker import persist, recipes
from worker.agentspec import (
    NotTranslatable,
    UnapprovedWrite,
    agent_ports,
    agent_to_spec,
    file_to_spec,
    job_name,
    released_by,
    same_rows,
)
from worker.audit import AuditError
from worker.engine import Approval, canonical
from worker.hashing import sha256_file
from worker.idgen import decision_refusal
from worker.registry import row_key
from worker.staging import duplicate_groups, value_overlap
from worker.templates import effective_settings, sponsor_levels, template_file
from worker.tools_kit import MAX_RESPONSE_CHARS, Kit, UploadRefused, cap, dump, evaluate_checked
from worker.trail import rules_markdown, write_trail

MAX_GROUPS = 25  # duplicate groups `duplicates` shows (the total is always given)


def _beside_findings(digest: str, findings: Sequence[Any], explanations: Mapping[str, Any]) -> str:
    """The digest with each cause's explanation on the line under its finding. The finding line is the engine's
    and is not changed; an explanation whose finding line was truncated away goes at the end."""
    under: dict[str, str] = {}
    for f in findings:
        e = explanations.get(str(f.id))
        if e is None:
            continue
        if e.status == "explained":
            under[str(f.id)] = f"  why (Explainer, cites {', '.join(e.cites)}): {e.text}"
        else:
            under[str(f.id)] = f"  why: {e.text}"
    prefixes = {str(f.id): f"{f.severity} {f.rule.rule_id} [{f.field}] x{f.count}" for f in findings}
    out: list[str] = []
    placed: set[str] = set()
    for line in digest.splitlines():
        out.append(line)
        for fid, prefix in prefixes.items():
            if fid in under and fid not in placed and line.startswith(prefix):
                out.append(under[fid])
                placed.add(fid)
                break
    rest = [v for key, v in under.items() if key not in placed]
    return "\n".join(out + rest)


def _engine_table(k: Kit, stage_id: str) -> list[dict[str, Any]]:
    """A staged unit's rows as the engine says them (`evaluate` to the check; nothing written)."""
    try:
        spec = agent_to_spec(k.ctx, stage_id, state=k.state)
    except NotTranslatable as exc:
        raise ValueError(f"the engine cannot say stage {stage_id} ({exc}), so it is not analysed here") from exc
    return list(evaluate_checked(spec, agent_ports(k.ctx), stop_after="check").staged(limit=10**9))


def _finalise_job(k: Kit, stage_id: str, template: str, why: str, target: Path) -> None:
    """Stamp the versions this output used and write the human-readable Rules and AuditTrail files."""
    ctx = k.ctx
    try:
        settings = effective_settings(ctx.sponsor, template)
    except ValueError:  # a record type that only the sponsor's overlay defines
        spec = ctx.stage.templates[template]
        settings = {n: {"value": getattr(spec, n), "source": "sponsor override"} for n in
                    ("fields", "required", "output", "id_max_len", "key", "name_field")}
    file = template_file(ctx.sponsor, template)
    resv = ctx.reservations.get(stage_id)
    versions = {
        "template": {"record_type": template, "file": file.name if file else "",
                     "sha256": sha256_file(file) if file else "",
                     "source": "sponsor override" if file and file.parent.name == ctx.sponsor else "organisation"},
        "recipe": dict(k.state.recipe) or None,
        "id_scheme": (
            {"scheme": resv[1].scheme, "counter_from": resv[1].first, "counter_to": resv[1].next}
            if resv is not None else None
        ),
        "levels": {**ctx.stage.templates[template].levels, **sponsor_levels(ctx.sponsor)},
        "skills": dict(k.state.skills),
        "golden_version": persist.golden(ctx.accounts_root).version if ctx.accounts_root else 0,
    }
    ctx.audit.record("job.versions", why, {"record_type": template, "output": target.name}, versions)
    job = {"job": job_name(ctx), "instruction": ctx.instruction, "output": target.name,
           "output_sha256": sha256_file(target)}
    entries = ctx.audit.read()
    write_trail(entries, ctx.out_dir, ctx.sponsor)
    (ctx.out_dir / f"{ctx.sponsor}_Rules.md").write_text(
        rules_markdown(ctx.sponsor, job, versions, settings, entries), encoding="utf-8"
    )


def _save_recipe(k: Kit, stage_id: str, template: str, why: str) -> None:
    """Keep the approved mapping as a variant of the sponsor's saved workflow (unchanged ones are not duplicated)."""
    ctx = k.ctx
    spec = k.state.specs.get(stage_id)
    if ctx.recipes is None or spec is None:
        return
    mapping_spec, post = recipes.split_spec(spec)
    variant = {
        "name": Path(mapping_spec["path"]).name if mapping_spec.get("path") else "",
        "spec": recipes.portable(mapping_spec), "layout": recipes.describe(mapping_spec), "post": post,
        "constants": dict(k.state.constants.get(stage_id, {})),
        "sources": dict(ctx.stage.summary(stage_id).get("sources", {})),
    }
    version, stored, new = ctx.recipes.save_variant(template, ctx.instruction, variant)
    k.state.recipe.update(version=version, variant=stored["variant_id"])
    ctx.audit.record(
        "recipe.saved" if new else "recipe.reused", why,
        {"record_type": template, "variant": stored["variant_id"]}, {"version": version},
    )


def _commit_approved(
    k: Kit, stage_id: str, template: str, why: str, table: list[dict[str, Any]] | None = None
) -> None:
    """After an approved upload: move the ID counter and add its rows (the engine's `table`, if a recipe changed it)
    to the existing-records store."""
    ctx = k.ctx
    resv = ctx.reservations.get(stage_id)
    if resv is not None:
        scheme, rsv, _id_field, _src_field = resv
        ctx.audit.record(
            "id.counter_committed", why,
            {"record_type": template, "from": rsv.first}, {"to": rsv.next, "issued": len(rsv.issued)},
        )
        scheme.commit(rsv)
    _save_recipe(k, stage_id, template, why)
    spec = ctx.stage.templates.get(template)
    if ctx.registry is None or spec is None or not spec.key:
        return
    src_field = resv[3] if resv is not None else None
    records = []
    for r in table if table is not None else ctx.stage.rows(stage_id):
        src_key = str(r.get(src_field, "")).strip() if src_field else ""
        records.append({
            "key": row_key(r, spec.key), "name": str(r.get(spec.name_field, "")) if spec.name_field else "",
            "source_key": src_key, "key_kind": "name" if resv is not None and not src_key else "source id",
            "fund": str(r.get("Fund_ID", "")),
        })
    batch = job_name(ctx)
    added = ctx.registry.add(template, records, source="app batch", batch=batch)
    ctx.audit.record(
        "registry.added", why, {"record_type": template, "batch": batch}, {"added": added, "rows": len(records)}
    )


def _preflight(k: Kit, stage_id: str, template: str) -> None:
    """What `Stage.write_upload` refuses before it writes, said the same way, before the engine runs."""
    ctx = k.ctx
    if ctx.id_root is not None:  # a type whose IDs are generated waits for the person's ID decision
        why = decision_refusal(ctx.sponsor, template, lambda: persist.id_scheme(
            ctx.id_root, ctx.sponsor, template, ctx.stage.templates[template].id_generation).decided)
        if why:
            ctx.audit.record("id.decision_missing", why, {"sponsor": ctx.sponsor, "record_type": template,
                                                          "stage_id": stage_id}, {})
            raise ValueError(why)
    meta = ctx.stage.meta(stage_id)
    open_err = meta.get("conflicts") or []
    if open_err:
        raise ValueError(
            f"{len(open_err)} row(s) still conflict with existing records (e.g. {open_err[0]['src']}: "
            f"{open_err[0]['kind']}); resolve each: exclude as duplicate, exclude as incorrect, or correct"
        )
    fields = ctx.stage._fields(template)
    extra = [f for f in {key for r in ctx.stage.rows(stage_id) for key, v in r.items() if v != "" and key != "_src"}
             if f not in fields]
    if extra:
        raise ValueError(f"staged fields not in template {template}: {sorted(extra)}")


def _engine_write(
    k: Kit, stage_id: str, template: str, drop_duplicates: bool, duplicate_key: list[str] | None,
    note: dict[str, Any],
) -> tuple[Path | None, list[dict[str, Any]] | None, int | None]:
    """The upload, written by `evaluate(stop_after="write")` under an Approval built from the gate decision.
    Returns (path, table, rows): `path` None when the engine cannot say the staged table (the caller then uses the
    old writer, under the same verified approval); `table` and `rows` are the engine's when a recipe changed it."""
    ctx = k.ctx
    entries = ctx.audit.read()
    try:  # the one verified approval: the engine's writer and the fallback writer are both released by it
        by = released_by(entries)
    except UnapprovedWrite as exc:
        raise UploadRefused(str(exc)) from exc
    ports = agent_ports(ctx)
    job = {"job": job_name(ctx), "stage_id": stage_id, "template": template}
    fetch = getattr(ports.world, "field_recipe", None)
    recipe = fetch(template) if callable(fetch) else None  # the sponsor's recipe step, or None
    recipe_fields = set((recipe or {}).get("recipe", {}).get("fields") or {})

    def fall_back(reason: str) -> None:
        if recipe is not None:  # the old writer does not apply a recipe: never write around one
            raise UploadRefused(f"recipe: {ctx.sponsor} has a field recipe for {template} and the engine cannot "
                                f"say this table ({reason[:200]}); it is not written around the recipe.")
        approval_id = "appr-" + uuid.uuid4().hex[:12]
        ports.audit.record("eval.legacy_write", reason[:300], {**job, "approved_by": by},  # record first
                           {"engine": False, "approval_id": approval_id})
        note.update(engine_fallback=reason[:240], approval_id=approval_id, approved_by=by)

    try:
        spec = agent_to_spec(ctx, stage_id, template=template, drop_duplicates=drop_duplicates,
                             duplicate_key=duplicate_key, state=k.state, entries=entries)
    except NotTranslatable as exc:
        fall_back(str(exc))
        return None, None, None
    ev = evaluate_checked(spec, ports, stop_after="check")
    table = list(ev.staged(limit=10**9))
    differs = same_rows(table, ctx.stage.rows(stage_id), recipe_fields)
    if differs:
        fall_back(f"the engine's table is not the staged table: {differs}")
        return None, None, None
    appr = Approval(
        approval_id="appr-" + uuid.uuid4().hex[:12], by=by, eval_key=ev.manifest.eval_key,
        findings_digest=ev.findings_digest,
        # the gate (agent.check_approval) has already refused an unacknowledged WARN with the same review
        acknowledged=tuple(f.id for f in ev.findings if f.severity == "WARN"), at=datetime.now(UTC).isoformat(),
    )
    done = evaluate_checked(spec, ports, approval=appr, stop_after="write")
    assert done.produced is not None
    manifest = json.loads(canonical(done.manifest))
    try:  # the job's manifest is on the engine's record before the upload is reported as written
        ports.audit.record("eval.manifest", f"upload {done.produced.path.name} written under {appr.approval_id}",
                           {**job, "eval_key": done.manifest.eval_key, "approved_by": appr.by},
                           {"manifest": manifest, "sha256": done.produced.sha256})
    except AuditError:
        done.produced.path.unlink(missing_ok=True)  # no record, so the write must not stand
        raise
    note.update(manifest=manifest, approval_id=appr.approval_id, approved_by=appr.by)
    if ev.manifest.field_recipes:
        return done.produced.path, table, done.produced.rows
    return done.produced.path, None, None


def engine_tools(k: Kit) -> list[BaseTool]:
    ctx, run, resolve = k.ctx, k.run, k.resolve
    state, explainer, manifest_in_record = k.state, k.explainer, k.manifest_in_record

    @tool
    def duplicates(stage_id: str, key_fields: list[str], why: str, normalise: bool = True) -> str:
        """Read-only. Groups of rows of a staged unit sharing a key (fields you name), on the engine's table."""
        def go() -> Any:
            groups = duplicate_groups(_engine_table(k, stage_id), key_fields, normalise)
            return {"groups": len(groups), "shown": groups[:MAX_GROUPS]}

        return run("duplicates", why, {"stage_id": stage_id, "key_fields": key_fields}, go)

    @tool
    def reconcile(stage_id_left: str, stage_id_right: str, left_field: str, right_field: str, why: str) -> str:
        """Read-only. Distinct values only in the left unit, only in the right and in both, on the engine's tables."""
        def go() -> Any:
            return value_overlap(_engine_table(k, stage_id_left), _engine_table(k, stage_id_right),
                                 left_field, right_field)

        inputs = {"left": stage_id_left, "right": stage_id_right, "fields": [left_field, right_field]}
        return run("reconcile", why, inputs, go)

    @tool
    def write_upload(
        stage_id: str, template: str, why: str, drop_duplicates: bool = False,
        duplicate_key: list[str] | None = None,
    ) -> str:
        """Write the upload file from a staged table. Needs human approval; never overwrites a file."""
        written: list[Path] = []
        engine_table: list[list[dict[str, Any]]] = []  # the engine's table, when a sponsor recipe changed it

        def go() -> Any:
            note: dict[str, Any] = {}
            _preflight(k, stage_id, template)
            target, table, n_rows = _engine_write(k, stage_id, template, drop_duplicates, duplicate_key, note)
            if target is None:  # a table the engine does not say: written as before, and the record says so
                target = ctx.stage.write_upload(stage_id, template, ctx.out_dir, drop_duplicates, duplicate_key)
            elif table is not None:
                engine_table.append(table)
            written.append(target)
            result: dict[str, Any] = {
                "file": target.name, "path": str(target),
                "rows": n_rows if n_rows is not None
                else len(ctx.stage.rows_for_upload(stage_id, drop_duplicates, duplicate_key)),
                "_audit": note if manifest_in_record else {},  # off by default: the golden lock's entries (M18)
            }
            spec = state.specs.get(stage_id)
            if ctx.memory is not None and spec is not None:
                try:
                    result["case_id"] = ctx.memory.save_case(
                        template, spec, dict(ctx.stage.summary(stage_id).get("sources", {})),
                        state.constants.get(stage_id, {}), target,
                        engine_table[0] if engine_table else ctx.stage.rows(stage_id), ctx.job_feedback,
                    )
                except OSError as exc:  # the upload stands; the case is only for replay
                    result["case_error"] = str(exc)[:200]
            return result

        def render(result: dict[str, Any]) -> str:
            return dump({key: v for key, v in result.items() if not key.startswith("_")})

        inputs = {"stage_id": stage_id, "template": template, "drop_duplicates": drop_duplicates}
        out = run("write_upload", why, inputs, go, render=render)
        if out.startswith("ERROR: audit unavailable") and written:
            written[0].unlink(missing_ok=True)  # no record, so the write must not stand
        elif written and not out.startswith(("ERROR", "REFUSED")):
            try:  # the upload is approved and written: only now do the counter and the store move
                _commit_approved(k, stage_id, template, why, engine_table[0] if engine_table else None)
                _finalise_job(k, stage_id, template, why, written[0])
                state.save(ctx)
            except AuditError as exc:
                written[0].unlink(missing_ok=True)
                return f"ERROR: audit unavailable: {exc}"
        return out

    @tool
    def run_job(
        why: str, stage_id: str = "", path: str = "", record_type: str = "",
        columns: dict[str, str] | None = None, sheet: str = "", header_row: int = 1, first_row: int = 0,
        fund_id: str = "", fund_name: str = "", offsets: bool = False,
    ) -> str:
        """Read-only. Run the engine over one unit and return its digest: outcome, row tally, the tie-out, one
        line per cause (rule, field, count, where) and what is MISSING. No rows are returned and nothing is
        written. Name the unit by a staged table (`stage_id`), or by a source file (`path` and `record_type`):
        a GL file is read as lines, its accounts mapped and checked with `columns` (Account, Deal, Journal,
        Debit, Credit as the file's header text); any other type is lifted with `columns` ({field: column
        letter}). Copy the figures from the digest; never retype them."""
        def go() -> Any:
            if bool(stage_id) == bool(path):
                raise ValueError("name the unit by stage_id or by path and record_type, not both and not neither")
            if stage_id:
                spec = agent_to_spec(ctx, stage_id, state=state)
            else:
                if not record_type:
                    raise ValueError("record_type is required with path")
                spec = file_to_spec(ctx, resolve(path), record_type, sheet=sheet, header_row=header_row,
                                    first_row=first_row, columns=columns, fund_id=fund_id, fund_name=fund_name,
                                    offsets=offsets)
            ev = evaluate_checked(spec, agent_ports(ctx), stop_after="check")
            errs = sum(1 for f in ev.findings if f.severity == "ERR")
            warns = sum(1 for f in ev.findings if f.severity == "WARN")
            text = ev.digest(MAX_RESPONSE_CHARS)
            explained = 0
            if explainer is not None and (errs or warns):
                from worker.roles import explain_causes

                tie = dataclasses.asdict(ev.tie_out) if ev.tie_out is not None else None
                unit = stage_id or f"{record_type}@{Path(path).name}"
                found = explain_causes(explainer, ev.findings, unit_id=unit, tie_out=tie)
                text = cap(_beside_findings(text, ev.findings, found))
                explained = sum(1 for e in found.values() if e.status == "explained")
            out: dict[str, Any] = {"_text": text, "eval_key": ev.manifest.eval_key,
                                   "outcome": ev.outcome, "reached": ev.reached, "err": errs, "warn": warns,
                                   "rows_read": int(ev.tally.get("read", 0))}
            if explainer is not None:  # the record's shape is unchanged when no Explainer is wired
                out["explained"] = explained
            return out

        inputs = {"stage_id": stage_id, "path": path, "record_type": record_type, "columns": columns or {},
                  "sheet": sheet, "header_row": header_row, "first_row": first_row, "fund_id": fund_id,
                  "fund_name": fund_name, "offsets": offsets}
        return run("run_job", why, inputs, go, [path] if path else None, render=lambda r: r["_text"])

    return [duplicates, reconcile, write_upload, run_job]
