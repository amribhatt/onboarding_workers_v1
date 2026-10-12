"""Tools of the staged chain after the lift: existing records and IDs, account mapping, saved workflows."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from worker import persist, recipes
from worker import record_facts as RF
from worker.accounts import Coa, TypeMap, map_keys
from worker.accounts import key_of as account_key
from worker.agentspec import job_name, released_by
from worker.idgen import Reservation, normalize, taken_ids
from worker.memory import apply_spec
from worker.recipes import header_row_texts
from worker.registry import row_key
from worker.skills import load_skills
from worker.templates import sponsors_dir, standards_dir
from worker.tools_kit import Kit, derive_stage, norm_text, record_feedback


def _entity(k: Kit, stage_id: str) -> tuple[str, Any]:
    rec = k.ctx.stage.summary(stage_id)["template"]
    spec = k.ctx.stage.templates[rec]
    if not spec.key:
        raise ValueError(f"{rec} has no key fields, so it cannot be matched against existing records")
    return rec, spec


def _exclusion_entry(stage_id: str, x: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    m = x["matched"]
    why = f"{x['reason']}; matched {m['key'] or m['name']} ({m['source']})"
    inputs = {"stage_id": stage_id, "row": x["src"], "key": x["key"], "name": x["name"], "key_kind": x["key_kind"]}
    return why, inputs, {"matched": m, "rule": "same key and same name", "decision": "excluded"}


def after_set(
    k: Kit, base_sid: str, sid: str, key_fields: Sequence[str],
    values: Mapping[tuple[str, ...], Mapping[str, str]], via: str, args: Mapping[str, Any],
) -> None:
    derive_stage(k, base_sid, sid, "set_by_key", key_fields=list(key_fields),
                 items=[[list(key), dict(v)] for key, v in values.items()], via=via, args=dict(args))


def match_impl(k: Kit, stage_id: str) -> str:
    ctx = k.ctx
    if ctx.registry is None:
        raise ValueError("no existing-records store is configured for this run")
    rec, spec = _entity(k, stage_id)
    known = ctx.registry.entries(rec)
    sid = ctx.stage.match_existing(stage_id, known, spec.key, spec.name_field)
    left = ctx.stage.left_out(sid)
    derive_stage(k, stage_id, sid, "edit_rows", via="match_existing", drop=[x["src"] for x in left])
    ctx.audit.record_many("row.excluded", [_exclusion_entry(stage_id, x) for x in left])
    return sid


def assign_impl(
    k: Kit, stage_id: str, id_field: str, source_key_field: str | None, existing_ids: Sequence[str] = ()
) -> tuple[str, Reservation]:
    ctx = k.ctx
    if ctx.id_root is None:
        raise ValueError("no ID counter is configured for this run")
    rec = ctx.stage.summary(stage_id)["template"]
    spec = ctx.stage.templates[rec]
    scheme = persist.id_scheme(ctx.id_root, ctx.sponsor, rec, spec.id_generation)
    if scheme.scheme["mode"] == "source":
        raise ValueError(  # live run: the old wording steered the model to change a person's decision
            f"{rec} IDs use the source ID as supplied (scheme mode 'source'): no new IDs are needed, the "
            "lifted ID column is already the ID. Do not call assign_ids or set_id_scheme; continue. Only if the "
            "human asks for new IDs do they change the scheme"
        )
    taken = set(existing_ids) | taken_ids(ctx.stage.rows(stage_id), id_field)
    if ctx.registry is not None:  # IDs the sponsor already has are never reissued
        taken |= {e["key"] for e in ctx.registry.entries(rec) if e.get("key")}
    rsv = scheme.reserve(taken)
    sid, _crosswalk = ctx.stage.assign_ids(stage_id, id_field, rsv.take, source_key_field)
    ctx.reservations[sid] = (scheme, rsv, id_field, source_key_field)
    derive_stage(k, stage_id, sid, "assign_ids", field=id_field, source_key_field=source_key_field,
                 scheme=rsv.scheme, first=rsv.first)
    return sid, rsv


def resolve_impl(k: Kit, stage_id: str, field: str, record_type: str) -> tuple[str, dict[str, Any]]:
    ctx = k.ctx
    if ctx.registry is None:
        raise ValueError("no existing-records store is configured for this run")
    template = ctx.stage.summary(stage_id)["template"]
    if field not in ctx.stage.templates[template].fields:
        raise ValueError(f"{field!r} is not a field of {template}")
    known: dict[str, str] = {}
    for e in ctx.registry.entries(record_type):
        ident = str(e.get("key") or e.get("name") or "")
        for ref in (ident, str(e.get("source_key") or "")):
            if ref:
                known.setdefault(norm_text(ref), ident)
    values = {str(r.get(field, "")).strip() for r in ctx.stage.rows(stage_id)} - {""}
    found = {v: known[norm_text(v)] for v in sorted(values) if norm_text(v) in known}
    chosen = {(norm_text(v),): {field: ident} for v, ident in found.items()}
    sid, changed = ctx.stage.set_by_key(stage_id, [field], chosen, only_blank=False)
    after_set(k, stage_id, sid, [field], chosen, "resolve_references", {"field": field, "record_type": record_type})
    info = {"field": field, "from": record_type, "resolved_values": len(found), "rows_changed": changed,
            "not_found": sorted(values - set(found))[:50]}
    return sid, info


def records_tools(k: Kit) -> list[BaseTool]:
    ctx, run, resolve, summary = k.ctx, k.run, k.resolve, k.summary
    specs_by_stage = k.state.specs

    @tool
    def match_existing(stage_id: str, why: str) -> str:
        """Check a staged table against this sponsor's existing records (earlier batches, an Ledgerline export, quick
        entries). Same key and name: left out as 'already onboarded' (counted, listed). Same key other name, same
        name other key, or a probable spelling variant when the source has no ID: kept and flagged as a
        conflict the human must resolve with resolve_conflicts. Reports where the existing count comes from."""
        def go() -> Any:
            sid = match_impl(k, stage_id)
            out = summary(sid)
            assert ctx.registry is not None
            out["existing_from"] = ctx.registry.provenance(out["template"]) or "no existing records are stored"
            return out

        return run("match_existing", why, {"stage_id": stage_id}, go)

    @tool
    def resolve_conflicts(stage_id: str, decisions: dict[str, dict[str, Any]], why: str) -> str:
        """Record the human's decision for conflict or flagged (ERR) rows. `decisions` maps a source row address (e.g. 'A7') to
        {"action": "exclude_duplicate" | "exclude_incorrect" | "correct", "reason": "<free text>", "values":
        {field: value}} (values only for `correct`). Only apply decisions the human gave you; never decide one."""
        def go() -> Any:
            rec, _spec = _entity(k, stage_id)
            known = ctx.registry.entries(rec) if ctx.registry is not None else []
            sid = ctx.stage.resolve_conflicts(stage_id, decisions, known)
            drop = [s for s, d in decisions.items() if d["action"] != "correct"]
            edits = {s: dict(d["values"]) for s, d in decisions.items() if d["action"] == "correct"}
            was = {str(r.get("_src", "")): r for r in ctx.stage.rows(stage_id)}
            derive_stage(k, stage_id, sid, "edit_rows", via="resolve_conflicts", drop=drop, edits=edits)
            # a corrected row that is now an exact match was left out by the re-check
            kept = {str(r.get("_src", "")) for r in ctx.stage.rows(sid)}
            specs = specs_by_stage.get(sid)
            if specs is not None:
                specs["drop"] = drop + [s for s in edits if s not in kept]
            ctx.audit.record_many("conflict.resolved", [
                (str(d["reason"]).strip(),
                 {"stage_id": stage_id, "row": s, "key": row_key(was.get(s, {}), _spec.key),
                  "action": d["action"], "values": d.get("values")},
                 {"before": {f: was.get(s, {}).get(f, "") for f in (d.get("values") or {})},
                  "after": d.get("values") or {}})
                for s, d in decisions.items()
            ])
            return summary(sid)

        return run("resolve_conflicts", why, {"stage_id": stage_id, "decisions": decisions}, go)

    @tool
    def add_existing_record(record_type: str, records: list[dict[str, str]], why: str) -> str:
        """Record that records already exist in Ledgerline, loaded outside the app, so a job that is blocked on a
        missing reference can continue. `records` is [{"key": "<existing Ledgerline ID>", "name": "<name>"}, ...]
        (several in one call). They are used as they are: no new ID, no reconciliation. Needs human approval;
        the audit keeps the reason."""
        def go() -> Any:
            if ctx.registry is None:
                raise ValueError("no existing-records store is configured for this run")
            if record_type not in ctx.stage.templates:
                raise ValueError(f"unknown record type {record_type!r}")
            bad = [r for r in records if not str(r.get("key", "")).strip()]
            if not records or bad:
                raise ValueError("every record needs the existing Ledgerline ID in `key`")
            batch = job_name(ctx)
            added = ctx.registry.add(record_type, records, source="quick entry", batch=batch)
            ctx.audit.record_many("existing_record.added", [
                (why, {"record_type": record_type, "key": r["key"], "name": r.get("name", "")},
                 {"source": "quick entry"}) for r in records])
            return {"record_type": record_type, "given": len(records), "added": added,
                    "already_known": len(records) - added, "now_stored": ctx.registry.provenance(record_type)}

        return run("add_existing_record", why, {"record_type": record_type, "records": records}, go)

    @tool
    def import_existing_records(
        path: str, sheet: str, template: str, columns: dict[str, str], header_row: int | None, first_row: int,
        why: str, last_row: int | None = None,
    ) -> str:
        """Import an export of records already loaded into Ledgerline outside the app into this sponsor's
        existing-records store. `columns` maps the record type's key and name fields to column letters. Needs
        human approval; imported records are used as is and are never reconciled."""
        def go() -> Any:
            if ctx.registry is None:
                raise ValueError("no existing-records store is configured for this run")
            spec = ctx.stage.templates[template]
            sid = ctx.stage.lift(
                resolve(path), sheet, columns, header_row, first_row, last_row, None, template=template
            )
            records = [
                {"key": row_key(r, spec.key), "name": str(r.get(spec.name_field, "")) if spec.name_field else ""}
                for r in ctx.stage.rows(sid)
            ]
            added = ctx.registry.add(template, records, source="outside export", batch=Path(path).name)
            return {"read": len(records), "added": added, "already_known": len(records) - added,
                    "now_stored": len(ctx.registry.entries(template)), "stage_id": sid}

        inputs = {"path": path, "sheet": sheet, "template": template, "columns": columns,
                  "header_row": header_row, "first_row": first_row, "last_row": last_row}
        return run("import_existing_records", why, inputs, go, [path])

    @tool
    def assign_ids(
        stage_id: str, id_field: str, why: str, source_key_field: str | None = None, existing_ids: list[str] | None = None
    ) -> str:
        """Give every row a new ID from this sponsor's counter (investors and commitments always get one). The
        old ID is kept in `source_key_field` (e.g. Source_Investor_Key). The same old ID gets the same new ID.
        Numbers already used by `existing_ids` are skipped. The counter moves only when the upload is approved."""
        def go() -> Any:
            sid, rsv = assign_impl(k, stage_id, id_field, source_key_field, existing_ids or [])
            out = summary(sid)
            out["new_ids"] = {"first": rsv.issued[0] if rsv.issued else None,
                              "last": rsv.issued[-1] if rsv.issued else None, "count": len(rsv.issued)}
            return out

        return run("assign_ids", why, {"stage_id": stage_id, "id_field": id_field,
                                       "source_key_field": source_key_field}, go)

    @tool
    def resolve_references(stage_id: str, field: str, record_type: str, why: str) -> str:
        """Replace the IDs in `field` (e.g. a commitment's Investor_ID, as the source file wrote them) with the
        Ledgerline IDs of the records the sponsor already has (record_type, e.g. Investors): the same store the
        console reads. A value is matched by its source key or its Ledgerline ID, never by guessing a name. Values
        with no record are left as they are and listed: they are a missing dependency, not something to invent."""
        def go() -> Any:
            sid, info = resolve_impl(k, stage_id, field, record_type)
            out = summary(sid)
            out["references"] = info
            return out

        return run("resolve_references", why, {"stage_id": stage_id, "field": field, "record_type": record_type}, go)

    @tool
    def set_id_scheme(
        record_type: str, why: str, mode: str, prefix: str = "", width: int = 0, start: int = 1,
        pattern: str = "", reset_counter_to: int | None = None,
    ) -> str:
        """Change this sponsor's ID scheme for a record type (mode source | sequential | custom), or reset its
        counter. Needs human approval; the audit records the old and new values."""
        def go() -> Any:
            if ctx.id_root is None:
                raise ValueError("no ID counter is configured for this run")
            if record_type not in ctx.stage.templates:
                raise ValueError(f"unknown record type {record_type!r}")
            tpl = ctx.stage.templates[record_type]
            store = persist.id_scheme(ctx.id_root, ctx.sponsor, record_type, tpl.id_generation)
            # the console's rules: one normalize (target tokens, safe literals) and one change, by the gate's person
            tokens = RF.target(record_type).id_tokens
            proposed, problems = normalize(
                tokens, {"mode": mode, "prefix": prefix, "width": width, "start": start, "pattern": pattern})
            if problems:
                raise ValueError("; ".join(problems))
            new = store.change(proposed, reset_counter_to, by=released_by(ctx.audit.read()), reason=why,
                               record=ctx.audit.record, tokens=tokens)
            return {"record_type": record_type, "scheme": new["scheme"], "counter": new["counter"]}

        return run("set_id_scheme", why, {"record_type": record_type, "mode": mode, "prefix": prefix,
                                          "width": width, "start": start, "pattern": pattern,
                                          "reset_counter_to": reset_counter_to}, go)

    return [match_existing, resolve_conflicts, import_existing_records, add_existing_record, assign_ids,
            resolve_references, set_id_scheme]


def _account_setup(k: Kit) -> tuple[Any, Any, Any, Any, Any]:
    ctx = k.ctx
    if ctx.accounts_root is None:
        raise ValueError("no account mapping store is configured for this run")
    skill = load_skills().get("map-accounts")
    if skill is None:
        raise ValueError("the map-accounts skill is not installed")
    base = standards_dir()
    sponsor_root = sponsors_dir() / ctx.sponsor
    return (
        Coa.load(base / "Golden_COA.csv"),
        TypeMap.load(base / "Transaction_Type_Mapping.csv"),
        skill.table("coa-cascade", sponsor_root if sponsor_root.is_dir() else None),
        persist.accounts(ctx.accounts_root, ctx.sponsor),
        skill,
    )


def map_impl(
    k: Kit, stage_id: str, account_field: str = "Account_Name", type_field: str = "Transaction_Type",
    target_field: str = "Account_Number", account_type_field: str = "",
) -> tuple[str, list[Any], int, Any]:
    ctx = k.ctx
    coa, types, table, store, skill = _account_setup(k)
    template = ctx.stage.summary(stage_id)["template"]
    keys: dict[str, dict[str, Any]] = {}
    for r in ctx.stage.rows(stage_id):
        at = str(r.get(account_type_field, "")) if account_type_field else ""
        acct, ttype = str(r.get(account_field, "")), str(r.get(type_field, ""))
        entry = keys.setdefault(account_key(at, acct, ttype), {"account_type": at, "account": acct,
                                                              "transaction_type": ttype, "rows": 0, "variants": []})
        entry["rows"] += 1
        pair = [acct, ttype]
        if pair not in entry["variants"]:
            entry["variants"].append(pair)
    golden = persist.golden(ctx.accounts_root).current() if ctx.accounts_root else {}
    results = map_keys(keys, coa, types, store.entries(), golden, table, ctx.ranker)
    k.state.skills.setdefault(skill.name, skill.version)
    applied = {
        (norm_text(a), norm_text(t)): {target_field: x.account}
        for x in results
        if x.action == "apply" and x.account
        for a, t in x.variants
    }
    sid, changed = ctx.stage.set_by_key(stage_id, [account_field, type_field], applied)
    args = {"account_field": account_field, "type_field": type_field, "target_field": target_field,
            "account_type_field": account_type_field}
    after_set(k, stage_id, sid, [account_field, type_field], applied, "map_accounts", args)
    ctx.stage.annotate(
        sid, "account_mapping",
        {"skill": skill.stamp, "keys": [x.as_dict() for x in results], "template": template, **args},
    )
    ctx.audit.record_many("account.mapped", [
        (f"{x.tier}: {x.evidence}", {"stage_id": stage_id, "key": x.key, "rows": x.rows},
         {"account": x.account, "action": x.action, "confidence": x.confidence, "row": x.row,
          "skill": skill.stamp})
        for x in results
    ])
    return sid, results, changed, skill


def accounts_tools(k: Kit) -> list[BaseTool]:
    ctx, run, summary = k.ctx, k.run, k.summary

    @tool
    def map_accounts(
        stage_id: str, why: str, account_field: str = "Account_Name", type_field: str = "Transaction_Type",
        target_field: str = "Account_Number", account_type_field: str = "",
    ) -> str:
        """Map the staged GL lines' source accounts to the Golden chart of accounts, one decision per distinct
        account+type key (skill map-accounts). Applies L0 historic and L1 transaction-type matches, and returns a
        line per key with tier, confidence, evidence, the account and the top candidates. Keys that are not
        applied keep a blank account until the human decides (confirm_account_mappings). Never types an account:
        every account comes from the chart."""
        def go() -> Any:
            sid, results, changed, skill = map_impl(
                k, stage_id, account_field, type_field, target_field, account_type_field
            )
            tiers: dict[str, int] = {}
            for x in results:
                tiers[x.tier] = tiers.get(x.tier, 0) + 1
            out = summary(sid)
            shown = ("key", "tier", "action", "confidence", "account", "account_name", "evidence", "flags", "rows")
            out["accounts"] = {
                "skill": skill.stamp,
                "keys": len(results),
                "rows_mapped": changed,
                "by_tier": tiers,
                "for_you": [
                    {key: x.as_dict()[key] for key in shown} | {"candidates": x.candidates[:3]}
                    for x in results
                    if x.action != "apply"
                ][:30],
            }
            return out

        return run("map_accounts", why,
                   {"stage_id": stage_id, "account_field": account_field, "type_field": type_field,
                    "target_field": target_field}, go)

    @tool
    def confirm_account_mappings(stage_id: str, picks: dict[str, str], why: str) -> str:
        """Record the human's decision for account keys: `picks` maps a key (as returned by map_accounts) to an
        account number from the chart. Needs human approval. Each pick is checked against the chart and the
        transaction type mapping, written to this sponsor's store (so it is L0 next time) and applied to the
        lines. Only pass what the human chose."""
        def go() -> Any:
            coa, types, _table, store, skill = _account_setup(k)
            info = ctx.stage.meta(stage_id).get("account_mapping")
            if not info:
                raise ValueError("this stage has no account mapping; run map_accounts first")
            by_key = {x["key"]: x for x in info["keys"]}
            chosen: dict[tuple[str, ...], dict[str, str]] = {}
            for key, account in picks.items():
                if key not in by_key:
                    raise ValueError(f"unknown key {key!r}")
                if account not in coa:
                    raise ValueError(f"{account!r} is not in the Golden chart of accounts")
                known = types.lookup(by_key[key]["transaction_type"])
                if known is not None and known[1] is not None and account not in known[1]:
                    raise ValueError(f"{account} is not allowed for transaction type {known[0]}")
                for a, t in by_key[key]["variants"]:
                    chosen[(norm_text(a), norm_text(t))] = {info["target_field"]: account}
            keyf = [info["account_field"], info["type_field"]]
            sid, changed = ctx.stage.set_by_key(stage_id, keyf, chosen, only_blank=False)
            after_set(k, stage_id, sid, keyf, chosen, "confirm_account_mappings", {})
            decided = [
                {**x, "account": picks[x["key"]], "account_name": coa.name(picks[x["key"]]), "action": "apply",
                 "decided_by": "human"}
                if x["key"] in picks
                else x
                for x in info["keys"]
            ]
            ctx.stage.annotate(sid, "account_mapping", {**info, "keys": decided})
            for key, account in picks.items():
                shown = by_key[key]
                record_feedback(
                    ctx,
                    kind="suggestion",
                    action="accept" if shown.get("account") == account else "override",
                    shown=f"{shown.get('account') or '(no account)'} {shown.get('tier', '')}".strip(),
                    chosen=account,
                    reason=why if shown.get("account") != account else "",
                    skill=skill.stamp,
                    table_row=shown.get("row"),
                    lineage=key,
                )
            ctx.audit.record_many("account.confirmed", [  # audit first: if it cannot be written, the store is untouched
                (why, {"stage_id": stage_id, "key": key},
                 {"account": a, "stored_for": ctx.sponsor, "skill": skill.stamp}) for key, a in picks.items()])
            for key, account in picks.items():
                store.add(key, account, source=f"approved in {ctx.out_dir.parent.name}", reason=why)
            return {**summary(sid), "confirmed": len(picks), "rows_changed": changed}

        return run("confirm_account_mappings", why, {"stage_id": stage_id, "picks": picks}, go)

    return [map_accounts, confirm_account_mappings]


def _match_variants(k: Kit, file: Path, template: str) -> dict[str, Any]:
    assert k.ctx.recipes is not None
    found = []
    for v in k.ctx.recipes.variants(template):
        leaves_now = [header_row_texts(file, leaf) for leaf in recipes.leaves(v["spec"])]
        found.append({"variant_id": v["variant_id"], "name": v.get("name", ""),
                      **recipes.match_layout(v["layout"], leaves_now)})
    order = {"exact": 0, "close": 1, "none": 2}
    found.sort(key=lambda m: (order[m["match"]], len(m["missing"])))
    best = found[0]["match"] if found else "none"
    return {"template": template, "saved_variants": len(found), "best": best, "variants": found}


def recipe_tools(k: Kit) -> list[BaseTool]:
    ctx, run, resolve, summary = k.ctx, k.run, k.resolve, k.summary
    constants_by_stage, specs_by_stage = k.state.constants, k.state.specs

    @tool
    def find_recipe(path: str, template: str, why: str) -> str:
        """Look for this sponsor's saved workflow for a record type and compare the new file's layout with each
        saved source variant. Reports `exact` (apply it), `close` (a proposed mapping with a diff to confirm with
        the human) or `none` (build it from scratch). Nothing is applied."""
        def go() -> Any:
            if ctx.recipes is None:
                raise ValueError("no saved workflows are configured for this run")
            return _match_variants(k, resolve(path), template)

        return run("find_recipe", why, {"path": path, "template": template}, go, [path])

    @tool
    def apply_recipe(
        path: str, template: str, why: str, variant_id: str = "", mapping: dict[str, str] | None = None,
        constants: dict[str, str] | None = None, paths: dict[str, str] | None = None,
    ) -> str:
        """Re-run a saved variant on a new file with code only (no model): lift, joins, distinct, then the saved
        steps (check against existing records, issue IDs). `variant_id` picks a variant (default: the best
        match; only `exact` applies on its own). For a `close` match pass the `mapping` ({field: column letter})
        the human confirmed from find_recipe. Fields the file no longer has stay blank and are reported MISSING.
        `constants` overrides fund parameters (e.g. Fund_ID); GP_ID is this run's. Ends at the approval gate."""
        def go() -> Any:
            if ctx.recipes is None:
                raise ValueError("no saved workflows are configured for this run")
            file = resolve(path)
            found = _match_variants(k, file, template)
            if not found["variants"]:
                raise ValueError(f"no saved workflow for {template}; build it from scratch")
            pick = next((v for v in found["variants"] if v["variant_id"] == (variant_id or found["variants"][0]["variant_id"])), None)
            if pick is None:
                raise ValueError(f"no variant {variant_id!r}; saved: {[v['variant_id'] for v in found['variants']]}")
            stored = next(v for v in ctx.recipes.variants(template) if v["variant_id"] == pick["variant_id"])
            if pick["match"] == "none":
                raise ValueError(f"{pick['variant_id']} does not fit this file ({len(pick['missing'])} fields missing)")
            if pick["match"] == "close" and not mapping:
                raise ValueError(
                    "the layout is close but not the same: show the human this diff and pass the confirmed "
                    f"`mapping`: {json.dumps(pick['diff'])}"
                )
            use_map = dict(mapping or pick["mapping"])
            consts = {**stored.get("constants", {})}
            if "GP_ID" in consts:
                consts["GP_ID"] = ctx.gp_id
            consts.update(constants or {})
            names = {Path(leaf["path"]).name for leaf in recipes.leaves(stored["spec"])}
            file_paths = {n: str(resolve(p)) for n, p in (paths or {}).items()}
            if len(names) == 1 and not file_paths:
                file_paths = {next(iter(names)): str(file)}
            bound = recipes.bind(stored["spec"], file_paths, use_map, consts)
            sid = apply_spec(ctx.stage, template, bound)
            specs_by_stage[sid] = bound
            constants_by_stage[sid] = consts
            for step in stored["post"]:
                if step["step"] == "match_existing":
                    nxt = match_impl(k, sid)
                elif step["step"] == "map_accounts":
                    nxt = map_impl(k, sid, **{key: v for key, v in step.items() if key != "step"})[0]
                elif step["step"] == "resolve_references":
                    nxt = resolve_impl(k, sid, step["field"], step["record_type"])[0]
                else:
                    nxt, _rsv = assign_impl(k, sid, step["field"], step.get("source_key_field"))
                sid = nxt
            ctx.audit.record(
                "recipe.applied", why,
                {"template": template, "variant": stored["variant_id"], "version": ctx.recipes.versions(template)[-1]},
                {"match": pick["match"], "stage_id": sid, "missing": pick["missing"]},
            )
            out = summary(sid)
            out["recipe"] = {"variant": stored["variant_id"], "match": pick["match"], "diff": pick["diff"],
                             "missing_fields": pick["missing"], "constants_used": consts,
                             "note": "applied by code, no model call; the approval gate still decides"}
            return out

        inputs = {"path": path, "template": template, "variant_id": variant_id, "mapping": mapping,
                  "constants": constants, "paths": paths}
        return run("apply_recipe", why, inputs, go, [path])

    return [find_recipe, apply_recipe]
