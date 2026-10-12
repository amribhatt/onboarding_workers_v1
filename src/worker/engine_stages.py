"""The stages that make the rows: read, map and convert, for the agent path and the console family (`_Run` mixin)."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from worker import engine_console as EC
from worker import setrecipe as SR
from worker import transforms as tf
from worker import workbook as wb
from worker.accounts import key_of, map_keys
from worker.contracts import ProfileColumn, RecordTypeCandidate, SourceProfile
from worker.engine_types import Refused, SourceRef, is_blank, sha, src_row
from worker.glio import GlRows, offset_line, read_gl_lines
from worker.hashing import sha256_file
from worker.idgen import taken_ids
from worker.pipeline import route_record_type
from worker.replay import digest as replay_digest
from worker.setops import ColumnMapper, RowTable
from worker.spool import Spool, read_json, write_json
from worker.staging import SRC, Stage, _enc, _first_sheet, _norm
from worker.templates import templates_for

# A resource bound, not a business rule: a GL read flushes its spool and its resume cursor every this many lines.
CHUNK_LINES = 5000


def _same_headers(batch: list[dict[str, Any]]) -> Iterator[tuple[tuple[str, ...], list[dict[str, Any]]]]:
    """Consecutive rows of a batch that share one header layout (the keys of their source cells)."""
    run: list[dict[str, Any]] = []
    headers: tuple[str, ...] | None = None
    for rec in batch:
        h = tuple(rec["v"])
        if headers is not None and h != headers:
            yield headers, run
            run = []
        headers = h
        run.append(rec)
    if headers is not None:
        yield headers, run


class RowStages:
    def _profile(self) -> None:
        facts: list[dict[str, Any]] = []
        profile = None
        for ref in self.spec.sources:
            if ref.kind != "file":
                continue
            path = self._path(ref)
            info = wb.list_sheets(path)
            sheets = []
            for s in info.get("sheets", []):
                look = wb.look(path, s["name"], 1, 12, 1, 10)
                sheets.append({**s, "look_digest": replay_digest(look)})
            facts.append({"ref": ref.ref, "kind": info.get("kind"), "sheets": sheets, "sha256": sha256_file(path)})
            if profile is None and info.get("sheets"):
                profile = self._profile_of(path, ref, info["sheets"])
        self.source_facts = tuple(facts)
        self.profile = profile

    def _profile_of(self, path: Path, ref: SourceRef, sheets: list[dict[str, Any]]) -> Any:
        want = ref.sheet or sheets[0]["name"]
        sh = wb._sheet(path, want)
        header = ref.header_row
        if header is None:
            for r in range(1, min(sh.max_row, 20) + 1):
                if sum(1 for c in range(1, sh.max_column + 1) if sh.get(r, c) and sh.get(r, c)[0] not in (None, "")) >= 2:
                    header = r
                    break
        header = header or 1
        cols = []
        texts = []
        for c in range(1, sh.max_column + 1):
            cell = sh.get(header, c)
            text = str(cell[0]).strip() if cell and cell[0] is not None else ""
            texts.append(text)
            kind = "text"
            for r in range(header + 1, min(sh.max_row, header + 20) + 1):
                v = sh.get(r, c)
                if v and v[0] not in (None, ""):
                    val = v[0]
                    kind = "number" if isinstance(val, int | float) and not isinstance(val, bool) else \
                        "date" if hasattr(val, "isoformat") else "text"
                    break
            cols.append(ProfileColumn(col_id=f"c{c}", header=text, kind=kind))
        low = [t.lower() for t in texts]
        layout = "gl_lines" if any("debit" in t for t in low) and any("credit" in t for t in low) else \
            "compact" if sh.has_pivot else "flat"
        best, scores = route_record_type(texts, templates_for(self.spec.sponsor) if self.spec.sponsor else {})
        cands = tuple(RecordTypeCandidate(type=t, confidence=min(1.0, max(0.0, s)), evidence="share of required fields found "
                                                                                    "in the headers")
                      for t, s in sorted(scores.items(), key=lambda kv: -kv[1]) if s > 0)[:3]
        if best is None and layout == "gl_lines":
            cands = (RecordTypeCandidate(type="GL", confidence=0.5, evidence="Debit and Credit columns"), *cands)[:3]
        fp = sha256_file(path)
        return SourceProfile(profile_id=f"prof-{fp[:12]}", file_fp=fp, sheet=sh.name, header_row=int(header),
                           layout=layout, columns=tuple(cols), record_type_candidates=cands)

    def _produce_read(self, key: str) -> dict[str, Any]:
        steps = self._steps("read")
        if self.stop_after == "read":
            self._profile()
        console = next((s for s in steps if s["step"] == "console_read"), None)
        if console is not None:
            return self._console_read(key, console)
        gl = next((s for s in steps if s["step"] == "lift_gl_lines"), None)
        return self._read_gl(key, gl) if gl is not None else self._read_table(key, steps)

    def _lift_columns(self, step: Mapping[str, Any]) -> dict[str, str]:
        cols = dict(step.get("columns") or {})
        for c in self.spec.choices:
            if c.kind == "map_column" and c.subject.startswith("field:"):
                cols[c.subject.split(":", 1)[1]] = str(c.value)
        return cols

    def _sheet_name(self, path: Path, step: Mapping[str, Any], ref: SourceRef) -> str:
        name = step.get("sheet") or ref.sheet
        if name:
            return str(name)
        return str(wb.list_sheets(path)["sheets"][0]["name"])

    def _read_table(self, key: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
        tmp = self._mkdtemp()
        stage = Stage(tmp, self.templates)
        names: dict[str, str] = {}
        last = ""
        wants_quality = {int(s.get("source", 0)) for s in self._steps("check") if s["step"] == "check_data_quality"}
        source_file = ""
        for n, step in enumerate(steps):
            kind = step["step"]
            as_name = str(step.get("as") or f"t{n}")
            try:
                if kind in ("lift", "lift_blocks", "flatten_compact"):
                    ref = self._source(step)
                    path = self._path(ref)
                    sheet = self._sheet_name(path, step, ref)
                    header = step.get("header_row", ref.header_row)
                    constants = dict(step.get("constants") or {})
                    if kind == "lift":
                        first = int(step.get("first_row") or ((header or 0) + 1))
                        sid = stage.lift(path, sheet, self._lift_columns(step), header, first, step.get("last_row"),
                                         constants, template=self.template)
                    elif kind == "lift_blocks":
                        blocks = [(b[0], int(b[1] or 1), b[2]) for b in step["blocks"]]
                        sid = stage.lift_blocks(path, sheet, self._lift_columns(step), blocks, constants,
                                                template=self.template)
                    else:
                        sid = stage.flatten_compact(path, sheet, {int(k): v for k, v in step["levels"].items()},
                                                    int(step["first_row"]), template=self.template,
                                                    last_row=step.get("last_row"), column=step.get("column", "A"))
                    if int(step.get("source", 0)) in wants_quality and not source_file:
                        source_file = str(path)
                elif kind == "join":
                    sid = stage.join(names[step["left"]], names[step["right"]], step["left_key"], step["right_key"],
                                     list(step["take"]), step.get("how", "left"))
                else:  # distinct: the closed list of read steps is checked at the start
                    sid = stage.distinct(names[step.get("of")] if step.get("of") else last, list(step["key_fields"]))
            except (ValueError, KeyError) as exc:
                raise Refused("schema_invalid", f"step {kind} cannot run on this file: {exc}") from exc
            names[as_name] = sid
            last = sid
        meta = stage.meta(last)
        if source_file:
            meta["source_file"] = source_file
        rows = stage.rows(last)
        spool = Spool(self.ports.store, f"st/{key}/rows")
        spool.extend(rows)
        digest = spool.seal()
        return {"mode": "table", "template": self.template, "meta": meta, "rows": len(rows), "spools": [spool.key],
                "digest": digest}

    def _investor_lookup(self) -> dict[str, tuple[str, str]]:
        known: dict[str, tuple[str, str]] = {}
        for e in self.ports.world.store("Investors"):
            ident, label = str(e.get("key") or e.get("name") or ""), str(e.get("name", ""))
            for ref in (ident, str(e.get("source_key") or e.get("key") or e.get("name") or ""), label):
                if ref:
                    known.setdefault(ref, (ident, label))
        return known

    def _read_gl(self, key: str, step: dict[str, Any]) -> dict[str, Any]:
        store = self.ports.store
        ref = self._source(step)
        path = self._path(ref)
        columns = self._lift_columns(step)
        header_row = int(step.get("header_row") or ref.header_row or 1)
        sheet = step.get("sheet") or ref.sheet or None
        sheet_name = sheet or _first_sheet(path)
        gp_id = str(step.get("gp_id") or self.spec.context.get("gp_id") or self.spec.sponsor)
        fund_id = str(step.get("fund_id", self.spec.context.get("fund_id", "")))
        fund_name = str(step.get("fund_name", self.spec.context.get("fund", "")))
        offsets = bool(step.get("offsets", False))
        known = self._investor_lookup()
        mk = GlRows(self.templates[self.template], gp_id, fund_id, fund_name, lambda _a, _t: "",
                    lambda r: known.get(r, ("", "")))
        main_key, extra_key, cursor_key = f"st/{key}/rows", f"st/{key}/extras", f"st/{key}/cursor"
        cur = read_json(store, cursor_key) or {"lines": 0, "main": 0, "extras": 0}
        store.truncate(main_key, int(cur["main"]))
        store.truncate(extra_key, int(cur["extras"]))
        huge = 1 << 60  # chunks are flushed by hand, together with the cursor
        main = Spool(store, main_key, chunk=huge, resume=True)
        extras = Spool(store, extra_key, chunk=huge, resume=True)
        skip = int(cur["lines"])
        consumed = 0
        cap = self.rules.limits.get("max_keys") if self.rules else None
        journals: set[str] = set()
        for n, ln in read_gl_lines(path, sheet, columns, header_row):
            consumed += 1
            if consumed <= skip:
                continue
            where = f"{sheet_name}!{n}"
            row = mk.row(ln)
            row[SRC] = where
            main.append(row)
            if offsets and ln.inv:
                off = mk.row(offset_line(ln), offset=True)
                off[SRC] = where
                extras.append(off)
            if cap is not None:
                journals.add(ln.j)
                if len(journals) > int(cap):
                    raise Refused("too_large", f"more than {cap} distinct journals: the per-journal balances would "
                                  "not fit in memory. A controller raises the cap, or the file is split by period.")
            if consumed % CHUNK_LINES == 0:
                main.flush()
                extras.flush()
                write_json(store, cursor_key, {"lines": consumed, "main": main.count, "extras": extras.count})
                self._progress("read", consumed, "chunk")
        main_digest = main.seal()
        extras_digest = extras.seal()
        acc = Stage(self._mkdtemp(), self.templates)._new_acc(self.template)
        meta = {k: v for k, v in acc.items() if k != "fields"}
        meta.update(template=self.template, stage_id="s1", input_rows=consumed, sources=dict(columns),
                    sheet=sheet_name, generated_rows=extras.count, source_file=str(path))
        return {"mode": "gl", "template": self.template, "meta": meta, "rows": main.count + extras.count,
                "spools": [main_key, extra_key], "digest": sha(main_digest, extras_digest)}

    def _produce_map(self, key: str) -> dict[str, Any]:
        rules = self.rules
        assert rules is not None
        rd = self.P["read"]
        steps = self._steps("map")
        ops: list[dict[str, Any]] = []
        info: dict[str, Any] | None = None
        problems: list[dict[str, Any]] = []
        picks_applied: list[dict[str, Any]] = []
        calls = 0
        acct_step = next((s for s in steps if s["step"] == "map_accounts"), None)
        cap = rules.limits.get("max_keys")
        for step in steps:
            if step["step"] == "map_accounts":
                if rules.cascade is None or rules.coa is None or rules.types is None:
                    raise Refused("schema_invalid", "the map-accounts skill or the Golden chart is not installed.")
                account_field = step.get("account_field", "Account_Name")
                type_field = step.get("type_field", "Transaction_Type")
                target_field = step.get("target_field", "Account_Number")
                at_field = step.get("account_type_field", "")
                keys: dict[str, dict[str, Any]] = {}
                # the distinct (account type, account, transaction type) of the file, counted by one query; the
                # key rule runs on those few triples, never on a row
                wanted = [f for f in (at_field, account_field, type_field) if f]
                triples = RowTable.from_batches(self._row_batches(rd), wanted).group_counts(wanted)
                for combo, n_rows in triples:
                    got = dict(zip(wanted, combo, strict=True))
                    at = got.get(at_field, "") if at_field else ""
                    k = key_of(at, got[account_field], got[type_field])
                    entry = keys.setdefault(k, {"account_type": at, "account": got[account_field],
                                                "transaction_type": got[type_field], "rows": 0, "variants": []})
                    entry["rows"] += n_rows
                    pair = [got[account_field], got[type_field]]
                    if pair not in entry["variants"]:
                        entry["variants"].append(pair)
                    if cap is not None and len(keys) > int(cap):
                        raise Refused("too_large", f"more than {cap} distinct account keys; a controller raises "
                                      "the cap.")
                ranker = self.ports.ranker.rank if self.ports.ranker is not None else None
                results = map_keys(keys, rules.coa, rules.types, self.ports.world.account_entries(),
                                   self.ports.world.golden_accounts(), rules.cascade, ranker)
                calls += 1 if ranker is not None else 0
                applied = {(_norm(a), _norm(t)): {target_field: x.account}
                           for x in results if x.action == "apply" and x.account for a, t in x.variants}
                ops.append({"via": "map_accounts", "key_fields": [account_field, type_field],
                            "only_blank": True, "items": [[list(k), v] for k, v in applied.items()]})
                info = {"skill": rules.stamps.get("map-accounts", "map-accounts"),
                        "keys": [x.as_dict() for x in results], "template": self.template,
                        "account_field": account_field, "type_field": type_field, "target_field": target_field,
                        "account_type_field": at_field}
                self._audit_many("account.mapped", [
                    (f"{x.tier}: {x.evidence}", {"eval_key": self.eval_key, "key": x.key, "rows": x.rows},
                     {"account": x.account, "action": x.action, "confidence": x.confidence, "row": x.row,
                      "skill": info["skill"]}) for x in results])
            elif step["step"] == "resolve_references":
                field_name, rtype = str(step["field"]), str(step["record_type"])
                if field_name not in self.templates[self.template].fields:
                    raise Refused("schema_invalid", f"{field_name!r} is not a field of {self.template}")
                known: dict[str, str] = {}
                for e in self.ports.world.store(rtype):
                    ident = str(e.get("key") or e.get("name") or "")
                    for ref in (ident, str(e.get("source_key") or "")):
                        if ref:
                            known.setdefault(_norm(ref), ident)
                # the confirmed references as a join over the column's distinct values
                column = RowTable.from_batches(self._row_batches(rd), [field_name]).column(field_name)
                joined = ColumnMapper(column, fold=True).distinct(known)
                found = {v.strip(): r for v, _n, r in joined if r is not None and v.strip()}
                found = {v: found[v] for v in sorted(found)}
                ops.append({"via": "resolve_references", "key_fields": [field_name], "only_blank": False,
                            "items": [[[_norm(v)], {field_name: ident}] for v, ident in found.items()]})
        picks = [c for c in self.spec.choices if c.kind == "map_account"]
        if picks:
            if info is None or acct_step is None:
                problems.append({"code": "choice_invalid", "message": "a map_account choice was given but the "
                                 "recipe has no map_accounts step; it was not applied."})
            else:
                by_key = {x["key"]: x for x in info["keys"]}
                chosen: dict[tuple[str, ...], dict[str, str]] = {}
                accepted: dict[str, str] = {}
                for c in picks:
                    k = c.subject.split(":", 1)[1] if c.subject.startswith("key:") else c.subject
                    account = str(c.value)
                    if k not in by_key:
                        problems.append({"code": "choice_invalid", "message": f"unknown account key {k!r}"})
                        continue
                    assert rules.coa is not None and rules.types is not None
                    if account not in rules.coa:
                        problems.append({"code": "choice_invalid",
                                         "message": f"{account!r} is not in the Golden chart of accounts"})
                        continue
                    known_t = rules.types.lookup(by_key[k]["transaction_type"])
                    if known_t is not None and known_t[1] is not None and account not in known_t[1]:
                        problems.append({"code": "choice_invalid", "message":
                                         f"{account} is not allowed for transaction type {known_t[0]}"})
                        continue
                    for a, t in by_key[k]["variants"]:
                        chosen[(_norm(a), _norm(t))] = {info["target_field"]: account}
                    accepted[k] = account
                    picks_applied.append({"key": k, "account": account, "by": c.by, "reason": c.reason,
                                          "shown": by_key[k].get("account", ""), "scope": c.scope})
                if chosen:
                    ops.append({"via": "confirm_account_mappings",
                                "key_fields": [info["account_field"], info["type_field"]], "only_blank": False,
                                "items": [[list(k), v] for k, v in chosen.items()]})
                    info = {**info, "keys": [
                        {**x, "account": accepted[x["key"]], "account_name": rules.coa.name(accepted[x["key"]]),
                         "action": "apply", "decided_by": "human"} if x["key"] in accepted else x
                        for x in info["keys"]]}
                    self._audit_many("choice.applied", [
                        (p["reason"], {"eval_key": self.eval_key, "kind": "map_account", "key": p["key"],
                                       "by": p["by"], "scope": p["scope"]},
                         {"account": p["account"], "shown": p["shown"]}) for p in picks_applied])
        return {"ops": ops, "account_mapping": info, "problems": problems,
                "accounts_confirmed": {p["key"]: p["account"] for p in picks_applied},
                "rows": rd["rows"], "model_calls": calls}

    def _supplies(self) -> tuple[dict[tuple[int, str], Any], dict[str, Any]]:
        by_row: dict[tuple[int, str], Any] = {}
        by_field: dict[str, Any] = {}
        for c in self.spec.choices:
            if c.kind != "supply":
                continue
            if c.subject.startswith("row:"):
                n, _, fld = c.subject[4:].partition("|")
                by_row[(int(n), fld)] = c.value
            else:
                by_field[c.subject.split(":", 1)[-1]] = c.value
        return by_row, by_field

    def _produce_convert(self, key: str) -> dict[str, Any]:
        rd, mp = self.P["read"], self.P["map"]
        if rd["mode"] == "console":  # decided whole in the check stage; the rows pass through (a recipe applied)
            return self._console_convert(key, rd, mp)
        if rd["mode"] == "gl":
            return self._convert_stream(key, rd, mp)
        return self._convert_table(key, rd, mp)

    def _restore(self, stage: Stage, tmp: str, payload: Mapping[str, Any], meta: Mapping[str, Any]) -> str:
        rows = list(self._rows_of(payload))
        target = Path(tmp) / "staged"
        target.mkdir(parents=True, exist_ok=True)
        m = {**meta, "template": self.template, "stage_id": "s1"}
        (target / "s1.json").write_text(json.dumps({"rows": rows, "meta": m}, default=_enc, ensure_ascii=False),
                                        encoding="utf-8")
        stage.restore()
        return "s1"

    def _convert_table(self, key: str, rd: dict[str, Any], mp: dict[str, Any]) -> dict[str, Any]:
        world = self.ports.world
        tmp = self._mkdtemp()
        stage = Stage(tmp, self.templates)
        sid = self._restore(stage, tmp, rd, rd["meta"])
        spec_t = self.templates[self.template]
        for op in mp["ops"]:
            items = {tuple(k): v for k, v in op["items"]}
            sid, _changed = stage.set_by_key(sid, op["key_fields"], items, only_blank=bool(op["only_blank"]))
        if mp["account_mapping"] is not None:
            stage.annotate(sid, "account_mapping", mp["account_mapping"])
        failures: list[dict[str, Any]] = []
        issued: list[str] = []
        problems: list[dict[str, Any]] = list(mp.get("problems", []))
        applied_choices: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        decided = False
        for step in self._steps("convert"):
            kind = step["step"]
            if kind == "assign_ids" and not decided:  # what the person decided comes before IDs are issued
                sid = self._apply_decisions(stage, sid, applied_choices)
                decided = True
            if kind == "transform":
                sid, bad = self._apply_transform(stage, sid, step)
                failures.extend(bad)
            elif kind == "match_existing":
                if not spec_t.key:
                    raise Refused("schema_invalid", f"{self.template} has no key fields, so it cannot be matched "
                                  "against existing records")
                known = world.store(self.template)
                sid = stage.match_existing(sid, known, spec_t.key, spec_t.name_field)
                left = stage.left_out(sid)
                self._audit_many("row.excluded", [
                    ("{}; matched {} ({})".format(x["reason"], x["matched"]["key"] or x["matched"]["name"],
                                                  x["matched"]["source"]),
                     {"eval_key": self.eval_key, "row": x["src"], "key": x["key"], "name": x["name"],
                      "key_kind": x["key_kind"]},
                     {"matched": x["matched"], "rule": "same key and same name", "decision": "excluded"})
                    for x in left])
            elif kind == "assign_ids":
                sid, got, more = self._assign_ids(stage, sid, step)
                issued.extend(got)
                problems.extend(more)
            # conflicts and exclusions the person decided come after the check against existing records
            if kind == "match_existing" and not decided:
                sid = self._apply_decisions(stage, sid, applied_choices)
                decided = True
        if not decided:
            sid = self._apply_decisions(stage, sid, applied_choices)
        sid = self._apply_supplies(stage, sid, applied_choices)
        self._audit_many("choice.applied", applied_choices)
        meta = stage.meta(sid)
        rows = stage.rows(sid)
        spool = Spool(self.ports.store, f"st/{key}/rows")
        spool.extend(rows)
        digest = spool.seal()
        return {"mode": "table", "template": self.template, "meta": meta, "rows": len(rows), "spools": [spool.key],
                "digest": digest, "ids_issued": issued, "transform_failures": failures, "problems": problems}

    def _assign_ids(self, stage: Stage, sid: str, step: Mapping[str, Any]) -> tuple[str, list[str], list[dict[str, Any]]]:
        world = self.ports.world
        id_field = str(step["id_field"])
        source_key = step.get("source_key_field")
        scheme = world.id_scheme(self.template)
        if scheme is None:
            return sid, [], [{"code": "id_counter_missing", "message": "no ID counter is configured for this run, so "
                              "no ID was issued."}]
        if scheme.get("mode") == "source":
            return sid, [], [{"code": "id_scheme_not_set", "message":
                              f"{self.template} IDs use the source ID as supplied (scheme mode 'source'); a person "
                              "sets a sequential or custom scheme before IDs are issued."}]
        rows = stage.rows(sid)
        taken = set(step.get("existing_ids") or ()) | taken_ids(rows, id_field)
        taken |= {str(e["key"]) for e in world.store(self.template) if e.get("key")}
        ids = list(world.reserve_ids(self.template, len(rows), self.eval_key, sorted(taken)))
        it = iter(ids)
        used: list[str] = []

        def next_id() -> str:
            v = next(it)
            used.append(v)
            return v

        sid2, _crosswalk = stage.assign_ids(sid, id_field, next_id, source_key)
        return sid2, used, []

    def _decisions(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for c in self.spec.choices:
            if c.kind == "exclude":
                src = c.subject.split(":", 1)[1] if ":" in c.subject else c.subject
                action = "exclude_duplicate" if str(c.value) == "duplicate" else "exclude_incorrect"
                out[src] = {"action": action, "reason": c.reason, "by": c.by}
            elif c.kind == "correct_id":
                src, _, fld = c.subject.split(":", 1)[-1].partition("|")
                cur = out.setdefault(src, {"action": "correct", "reason": c.reason, "values": {}, "by": c.by})
                if cur["action"] == "correct":
                    cur["values"][fld or "Investor_ID"] = c.value
        return out

    def _apply_decisions(self, stage: Stage, sid: str, log: list[tuple[str, dict[str, Any], dict[str, Any]]]) -> str:
        decisions = self._decisions()
        if not decisions:
            return sid
        before = {str(r.get(SRC, "")): r for r in stage.rows(sid)}
        todo = {s: {k: v for k, v in d.items() if k != "by"} for s, d in decisions.items() if s in before}
        if not todo:
            return sid
        known = self.ports.world.store(self.template)
        sid = stage.resolve_conflicts(sid, todo, known)
        for s, d in todo.items():
            log.append((str(d["reason"]).strip(), {"eval_key": self.eval_key, "row": s, "action": d["action"],
                                                   "values": d.get("values"), "by": decisions[s]["by"]},
                        {"before": {f: before.get(s, {}).get(f, "") for f in (d.get("values") or {})},
                         "after": d.get("values") or {}}))
        return sid

    def _apply_supplies(self, stage: Stage, sid: str, log: list[tuple[str, dict[str, Any], dict[str, Any]]]) -> str:
        by_row, by_field = self._supplies()
        if not by_row and not by_field:
            return sid
        edits: dict[str, dict[str, Any]] = {}
        who = {(c.subject): c for c in self.spec.choices if c.kind == "supply"}
        for row in stage.rows(sid):
            src = str(row.get(SRC, ""))
            n = src_row(src)
            for fld in self.templates[self.template].fields:
                if not is_blank(row.get(fld)):
                    continue
                if (n, fld) in by_row:
                    edits.setdefault(src, {})[fld] = by_row[(n, fld)]
                elif fld in by_field:
                    edits.setdefault(src, {})[fld] = by_field[fld]
        if not edits:
            return sid
        for c in who.values():
            log.append((c.reason, {"eval_key": self.eval_key, "kind": "supply", "subject": c.subject, "by": c.by,
                                   "scope": c.scope}, {"value": str(c.value), "inferred": False}))
        return stage.edit_rows(sid, edits=edits)

    def _apply_transform(self, stage: Stage, sid: str, step: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        recipe = {**step["recipe"], "fields": dict(step["recipe"].get("fields", {}))}
        for c in self.spec.choices:
            if c.kind == "transform":
                recipe["fields"][c.subject.split(":", 1)[-1]] = c.value
        ctx = self._transform_context()
        try:
            compiled = tf.compile_recipe(recipe, None, ctx)
        except tf.RecipeError as exc:
            raise self._recipe_refusal(exc) from exc
        ref = self._source(step)
        path = self.ports.reader.path(ref)
        sh = wb._sheet(path, self._sheet_name(path, step, ref))
        header_row = int(step.get("header_row") or ref.header_row or 1)
        headers = tuple(str((sh.get(header_row, c) or ("",))[0] if (sh.get(header_row, c) or (None,))[0] is not None
                            else "") for c in range(1, sh.max_column + 1))
        rows = stage.rows(sid)
        srcs = [str(r.get(SRC, "")) for r in rows]
        numbers = [src_row(x) for x in srcs]
        columns = [[(sh.get(n, c) or (None,))[0] for n in numbers] for c in range(1, sh.max_column + 1)]
        done = SR.apply_recipe(compiled, headers, columns, ctx, len(rows))
        status = {name: r.status for name, r in done.items()}
        code = {name: r.code for name, r in done.items()}
        value = {name: r.value for name, r in done.items()}
        edits: dict[str, dict[str, Any]] = {}
        failures: list[dict[str, Any]] = []
        for k, src in enumerate(srcs):
            for name in compiled.fields:
                if status[name][k] == "ok":
                    edits.setdefault(src, {})[name] = value[name][k]
                elif status[name][k] == "missing":
                    edits.setdefault(src, {})[name] = ""
                else:
                    failures.append({"src": src, "field": name, "code": code[name][k]})
        return stage.edit_rows(sid, edits=edits), failures

    def _convert_stream(self, key: str, rd: dict[str, Any], mp: dict[str, Any]) -> dict[str, Any]:
        """Row-wise conversion of a GL file: the account decisions of the map stage, then what the person
        supplied, corrected or excluded, one row at a time. The same `set_by_key` rule as the table path."""
        store = self.ports.store
        ops = [(list(op["key_fields"]), {tuple(k): v for k, v in op["items"]}, bool(op["only_blank"]))
               for op in mp["ops"]]
        by_row, by_field = self._supplies()
        decisions = self._decisions()
        fields_t = self.templates[self.template].fields
        out = Spool(store, f"st/{key}/rows")
        skipped: list[dict[str, Any]] = []
        log: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        problems: list[dict[str, Any]] = list(mp.get("problems", []))
        seen_supply = False
        for batch in self._row_batches(rd):
            held: set[int] = set()
            for at, row in enumerate(batch):
                src = str(row.get(SRC, ""))
                dec = decisions.get(src)
                if dec is not None and dec["action"] != "correct":
                    held.add(at)
                    skipped.append({"src": src, "reason": f"excluded by {dec['by']}: {dec['reason']}"})
                    log.append((dec["reason"], {"eval_key": self.eval_key, "row": src, "action": dec["action"],
                                                "by": dec["by"]}, {"decision": "excluded"}))
            # every confirmed mapping is one hash join of the rows' keys against its table, in the order the
            # mappings are listed (a later mapping reads what an earlier one filled), applied to the matched rows
            for key_fields, items, only_blank in ops:
                keys = list(items)
                for at, k in RowTable.from_rows(batch, key_fields).match_keys(key_fields, keys):
                    if at in held:
                        continue
                    row = batch[at]
                    for name, value in items[keys[k]].items():
                        if not only_blank or row.get(name, "") == "":
                            row[name] = value
            for at, row in enumerate(batch):
                if at in held:
                    continue
                src = str(row.get(SRC, ""))
                dec = decisions.get(src)
                if dec is not None:  # correct
                    for fld, v in dec["values"].items():
                        row[fld] = v
                if by_row or by_field:
                    n = src_row(src)
                    for fld in fields_t:
                        if is_blank(row.get(fld)):
                            if (n, fld) in by_row:
                                row[fld] = by_row[(n, fld)]
                                seen_supply = True
                            elif fld in by_field:
                                row[fld] = by_field[fld]
                                seen_supply = True
                out.append(row)
        if seen_supply:
            for c in self.spec.choices:
                if c.kind == "supply":
                    log.append((c.reason, {"eval_key": self.eval_key, "kind": "supply", "subject": c.subject,
                                           "by": c.by, "scope": c.scope}, {"value": str(c.value), "inferred": False}))
        self._audit_many("choice.applied", log)
        digest = out.seal()
        meta = {**rd["meta"], "excluded": skipped[:200], "excluded_count": len(skipped)}
        if mp.get("account_mapping") is not None:
            meta["account_mapping"] = mp["account_mapping"]
        return {"mode": "gl", "template": self.template, "meta": meta, "rows": out.count, "spools": [out.key],
                "digest": digest, "ids_issued": [], "transform_failures": [], "problems": problems}

    def _console_read(self, key: str, step: Mapping[str, Any]) -> dict[str, Any]:
        """Read a console job's source as it is: one record per data row (a static type) or per line (a GL)."""
        ref = self._source(step)
        path = self._path(ref)
        spool = Spool(self.ports.store, f"st/{key}/rows")
        header: list[str] = []
        try:
            if step.get("kind") == "gl":
                sponsor = str(step.get("sponsor", ""))
                sheet, recs = EC.read_gl_records(path, lambda acct: EC.deal_required(acct, sponsor))
            else:
                sheet, header, recs = EC.read_static_records(path)
        except (ValueError, KeyError, OSError) as exc:
            raise Refused("schema_invalid", f"{path.name} cannot be read as a console source: {exc}") from exc
        spool.extend(recs)
        digest = spool.seal()
        meta = {"header": header, "sheet": sheet, "input_rows": len(recs), "source_file": str(path),
                "template": self.template, "sources": {}}
        return {"mode": "console", "template": self.template, "meta": meta, "rows": len(recs),
                "spools": [spool.key], "digest": digest}

    def _field_steps(self) -> tuple[dict[str, Any], dict[str, str]]:
        """(the fields a recipe decides, the pins of the sponsor recipes among them): the sponsor's recipe step(s)
        in the spec, then the person's job-scope transform choices over them."""
        fields: dict[str, Any] = {}
        pins: dict[str, str] = {}
        for b in (self.spec.recipe.bindings if self.spec.recipe else ()):
            if b.get("step") == "transform":
                fields.update(dict((b.get("recipe") or {}).get("fields") or {}))
                fr = b.get("field_recipe") or {}
                if fr.get("pin"):
                    pins[str(fr.get("record_type") or self.spec.record_type)] = str(fr["pin"])
        for c in self.spec.choices:
            if c.kind == "transform":
                fields[c.subject.split(":", 1)[-1]] = c.value
        return fields, pins

    def _console_convert(self, key: str, rd: dict[str, Any], mp: dict[str, Any]) -> dict[str, Any]:
        """The console family's convert stage: the rows as read, with the sponsor's field recipe (and the person's
        job-scope steps) applied to a static source before any rule or check. A value the recipe makes is kept beside
        the row as `rv`; a value it cannot read is kept as `rf` (the code) and in `transform_failures` with the
        original cell, and the output stays blank. Nothing changes when there is no recipe."""
        out: dict[str, Any] = {"mode": "console", "template": self.template, "meta": rd["meta"], "rows": rd["rows"],
                               "spools": list(rd["spools"]), "digest": rd["digest"], "ids_issued": [],
                               "transform_failures": [], "problems": list(mp.get("problems", []))}
        fields, pins = self._field_steps()
        if not fields or (self.console or {}).get("kind") == "gl":
            return out
        ctx = self._transform_context(records=True)
        try:
            compiled = tf.compile_recipe({"fields": fields}, None, ctx)
        except tf.RecipeError as exc:
            raise self._recipe_refusal(exc) from exc
        sheet = str(rd["meta"].get("sheet", ""))
        spool = Spool(self.ports.store, f"st/{key}/rows")
        failures: list[dict[str, Any]] = []
        for batch in self._row_batches(rd):
            for headers, run in _same_headers(batch):
                columns = [list(c) for c in zip(*(tuple(r["v"].values()) for r in run), strict=True)]
                done = SR.apply_recipe(compiled, headers, columns, ctx, len(run))
                status = {name: r.status for name, r in done.items()}
                code = {name: r.code for name, r in done.items()}
                value = {name: r.value for name, r in done.items()}
                for k, rec in enumerate(run):  # the spool record of each row: persistence, not computation
                    rv: dict[str, Any] = {}
                    rf: dict[str, str] = {}
                    for name in compiled.fields:
                        if status[name][k] == "failed":
                            rf[name] = str(code[name][k])
                            failures.append({"src": f"{sheet}!{rec['row']}", "field": name, "code": str(code[name][k]),
                                             "value": tf._cut(done[name].source_cell(k))})  # the original cell
                        else:
                            rv[name] = "" if status[name][k] == "missing" else value[name][k]
                    spool.append({**rec, "rv": rv, "rf": rf})
        digest = spool.seal()
        return {**out, "spools": [spool.key], "digest": digest, "transform_failures": failures,
                "recipe": {"pins": dict(sorted(pins.items())), "fields": sorted(compiled.fields), "rows": spool.count,
                           "failed": len(failures)}}
