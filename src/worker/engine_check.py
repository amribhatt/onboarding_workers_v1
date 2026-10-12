"""The check stage (`_Run` mixin): quality scan, review, per-row outcomes, coverage, tie-out, reconcile, the console
family's decisions; and the findings it raises, each with its rule, family and the options a person has."""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from collections.abc import Iterable, Iterator, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from worker import accuracy as AC
from worker import checks as checks_mod
from worker import columnar, quality
from worker import engine_console as EC
from worker import recon as R
from worker import record_facts as RF
from worker import review as review_mod
from worker.engine_types import Finding, Refused, RuleRef, Totals, is_blank, src_row
from worker.glio import GlRows, read_gl_lines
from worker.guard import PathRefused
from worker.ports import LoadedRules
from worker.setops import RowTable
from worker.spool import Spool, read, read_json
from worker.staging import SRC, _norm
from worker.templates import build_coverage, sponsor_levels
from worker.transforms import FAILURE_SENTENCES

_FAMILY = {"conflict": "existing", "missing_dependency": "existing", "duplicate_id": "uniqueness",
           "required_blank": "completeness", "no_rows": "completeness", "id_too_long": "validity",
           "gp_id_differs": "consistency", "identical_columns": "consistency", "account_unmapped": "consistency",
           "account_not_allowed": "consistency", "transaction_type_unknown": "validity",
           "quality_not_checked": "completeness"}
_OPTIONS = {"required_blank": ("supply", "declare_missing", "exclude", "accept_flag"),
            "id_too_long": ("correct_id", "exclude"), "duplicate_id": ("correct_id", "exclude"),
            "missing_dependency": ("exclude", "correct_id"), "conflict": ("exclude", "correct_id"),
            "account_unmapped": ("map_account",), "account_not_allowed": ("map_account",),
            "transaction_type_unknown": ("map_account", "accept_flag"), "gp_id_differs": ("accept_flag", "map_column"),
            "identical_columns": ("accept_flag", "map_column"), "no_rows": ("map_column",),
            "quality_not_checked": ("accept_flag",)}
_FAMILY_OPTIONS = {"completeness": ("supply", "declare_missing", "exclude"), "validity": ("correct_id", "exclude"),
                   "uniqueness": ("correct_id", "exclude"), "consistency": ("map_account", "exclude"),
                   "balance": ("exclude",)}


def engine_finding(code: str, severity: str, message: str, field_name: str = "", rows: Sequence[str] = (),
                   count: int | None = None, **extra: Any) -> dict[str, Any]:
    """A finding the engine itself raises (never a rule of a skill). Same shape as `checks.finding`."""
    digest = hashlib.sha256(f"{code}|{field_name}|{message}".encode()).hexdigest()[:8]
    return {"id": f"{code}:{digest}", "code": code, "severity": severity, "message": message,
            "rows": list(rows)[:50], "count": len(rows) if count is None else count, "field": field_name,
            "engine": True, **extra}


def console_message(code: str, name: str = "") -> str:
    """What a console cause says, once for all its rows (the row-level reason stays on the row)."""
    if code == "required_blank":
        return f"{name} is blank"
    if code == "optional_missing":
        return f"{name} is not in the file for some rows, left blank and reported"
    if code == "transform_failed":  # name is "<field>|<failure code>"
        fld, _, why = name.partition("|")
        return f"{fld} could not be read: {FAILURE_SENTENCES.get(why, why)}"
    return EC.cause(code)["sentence"]


def console_finding_id(code: str, name: str) -> str:
    return str(engine_finding(code, "ERR", console_message(code, name), name)["id"])


# options a person has for each console cause (closed `Choice` kinds)
_CONSOLE_OPTIONS = {
    "required_blank": ("supply", "exclude", "map_column"), "key_clash": ("exclude", "correct_id"),
    "name_clash": ("exclude", "correct_id"), "suffix_clash": ("exclude", "correct_id"),
    "similar_in_file": ("exclude",), "missing_dependency": ("accept_flag", "exclude"),
    "bad_id_chars": ("correct_id", "exclude"), "generated_id_collision": ("correct_id", "exclude"),
    "gl_only_investment": ("accept_flag", "exclude"), "id_too_long": ("correct_id", "exclude", "set_rule_option"),
    "id_collision_after_cut": ("correct_id", "exclude"), "unknown_investor": ("accept_flag",),
    "reference_not_in_crosswalk": ("accept_flag",), "account_not_in_chart": ("map_account",),
    "transaction_type_unknown": ("map_account",), "deal_required": ("supply", "exclude"),
    "transform_failed": ("transform", "supply", "exclude"),
    "journal_unbalanced": ("exclude", "accept_flag"), "fuzzy_match": ("accept_flag",),
}


def shape_findings(rules: LoadedRules, found: Sequence[Mapping[str, Any]]) -> tuple[Finding, ...]:
    """The `Finding`s of a sealed check: each raw finding with the rule it comes from, its family and the options."""
    out: list[Finding] = []
    controlled = checks_mod._CONTROLLED
    floors = rules.controls_floor
    for f in found:
        code = str(f["code"])
        if f.get("engine"):
            ref = RuleRef(f"engine.{code}", "src/worker/engine.py", "", "", False)
            family = "consistency"
            options: tuple[str, ...] = ("accept_flag",) if f["severity"] != "ERR" else ("set_rule_option",)
            if code == "choice_invalid":
                options = ("map_account",)
            elif code == "transform_failed":
                options = ("transform", "supply", "exclude")
            elif code == "id_scheme_not_set":
                options = ("set_rule_option",)
        elif f.get("console"):
            rid = EC.RULE_OF.get(code)
            idx = rules.rule_index.get(rid) if rid else None
            backed = rid is not None and idx is not None
            ref = RuleRef(
                f"console.{code}",
                f"skills/check-data-quality/checks.yaml:rules[{idx}]" if backed else "src/worker/engine_console.py",
                "check-data-quality" if backed else "", rules.skills.get("check-data-quality", "") if backed else "",
                bool(backed and rid in (floors.get("check-data-quality", {}).get("severity_floor", {}) or {})))
            family = EC.cause(code)["family"]
            options = _CONSOLE_OPTIONS.get(code, ("accept_flag",))
        elif code == "data_quality":
            rid = str(f.get("rule", ""))
            idx = rules.rule_index.get(rid)
            file = "skills/check-data-quality/checks.yaml" + (f":rules[{idx}]" if idx is not None else "")
            control = rid in (floors.get("check-data-quality", {}).get("severity_floor", {}) or {})
            ref = RuleRef(f"check-data-quality.{rid}", file, "check-data-quality",
                          rules.skills.get("check-data-quality", ""), control)
            family = str(f.get("cause", "consistency.x")).split(".", 1)[0]
            options = _FAMILY_OPTIONS.get(family, ("accept_flag",))
        else:
            skill = "map-accounts" if code in controlled else "check-data-quality"
            ref = RuleRef(f"checks.{code}", f"src/worker/checks.py:DEFAULT_LEVELS[{code}]", skill,
                          rules.skills.get(skill, ""), code in controlled)
            family = _FAMILY.get(code, "consistency")
            options = _OPTIONS.get(code, ("accept_flag",))
        loc = f.get("locator")
        locator = {k: str(v) for k, v in loc.items() if k in ("where", "filter", "file", "sheet")} \
            if isinstance(loc, Mapping) else {}
        sample = tuple(n for n in (src_row(s) for s in (f.get("rows") or [])) if n)
        out.append(Finding(
            id=str(f["id"]), rule=ref, severity=str(f["severity"]), family=family, field=str(f.get("field", "")),
            message=str(f["message"]), count=int(f.get("count", 0)), ranges_text=str(f.get("ranges", "")),
            locator=locator, sample=sample, options=tuple(options), detail=dict(f)))
    return tuple(out)


class CheckStage:
    def _quality_references(self, record_type: str) -> dict[str, set[str]]:
        world = self.ports.world
        refs: dict[str, set[str]] = {"investors": set(), "existing": set(), "coa_sources": set()}
        for e in world.store("Investors"):
            refs["investors"].update({_norm(e.get("key", "")), _norm(e.get("source_key", ""))} - {""})
        for e in world.store(record_type):
            refs["existing"].add(_norm(e.get("key", "")))
        # what the person confirmed in this job is known to the check the way a stored mapping is (the agent path
        # stores it before it checks; the engine stores it at commit)
        confirmed = (self.P.get("map") or {}).get("accounts_confirmed") or {}
        for entries in (world.account_entries(), world.golden_accounts(), confirmed):
            for k in entries:
                refs["coa_sources"].add(_norm(k.split("|")[1] if k.count("|") >= 2 else k))
        return refs

    def _levels(self) -> dict[str, str]:
        spec_t = self.templates[self.template]
        levels = {**spec_t.levels, **sponsor_levels(self.spec.sponsor)}
        for c in self.spec.choices:
            if c.kind == "set_rule_option" and c.subject.startswith("level:") and c.value in ("ERR", "WARN"):
                code = c.subject.split(":", 1)[1]
                levels[code] = checks_mod.stricter(levels.get(code, "WARN"), str(c.value))  # raises only
        return levels

    def _run_quality(self, step: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
        rules = self.rules
        assert rules is not None
        ref = self._source(step)
        path = self._path(ref)
        record_type = str(step.get("record_type") or self.spec.record_type)
        tspec = self.templates.get(self.template)
        groups = quality.groups_from_spec(tspec) if tspec is not None else {}
        if tspec is not None:
            groups["required"] = [f for f in groups["required"] if f != "GP_ID"]
            if record_type == "GL" or self.template == "GL":
                groups["required"] = []
        by_row, by_field = self._supplies()
        supplied_all = {("required_blank", f): v for f, v in by_field.items()}
        header_row = int(step.get("header_row") or ref.header_row or 1)
        report = quality.scan(
            path, list(rules.quality_rules), columns=dict(step["columns"]), record_type=record_type,
            sheet=(step.get("sheet") or ref.sheet or None), header_row=header_row, groups=groups,
            references=self._quality_references(record_type), levels=self._levels(),
            supplied=by_row, supplied_all=supplied_all,
            default_breakdown="Account" if record_type == "GL" else "", generated=list(step.get("generated") or []))
        if report["causes"]:
            self._audit_many("quality.cause", [
                (f"{c['family']}: {c['message']}", {"cause": c["id"], "file": path.name, "eval_key": self.eval_key},
                 {"severity": c["severity"], "count": c["count"], "ranges": c["ranges_text"]})
                for c in report["causes"]])
        return path, report

    def _produce_check(self, key: str) -> dict[str, Any]:
        if self.console is not None:
            return self._console_check(key)
        cv = self.P["convert"]
        store = self.ports.store
        reports: dict[str, dict[str, Any]] = {}
        for step in self._steps("check"):
            if step["step"] == "check_data_quality":
                path, report = self._run_quality(step)
                reports[str(path)] = report
        meta = dict(cv["meta"])
        meta.setdefault("template", self.template)
        if self.P["map"].get("account_mapping") and "account_mapping" not in meta:
            meta["account_mapping"] = self.P["map"]["account_mapping"]
        facade = _ReviewCtx(self.spec.sponsor, str(self.spec.context.get("gp_id") or self.spec.sponsor),
                            self.ports.world, reports)
        extra: list[dict[str, Any]] = []
        # --- the review findings: one set-based review over the whole table, whatever its size ---
        wstep = self._first_step("write") or {}
        table = RowTable.from_batches(self._row_batches(cv))
        rev = review_mod.review_table(
            facade, table, {**meta, "template": self.template, "stage_id": "s1"}, self.templates,
            bool(wstep.get("drop_duplicates", False)), wstep.get("duplicate_key"), None,
            lambda target: [table] if target == self.template else [])
        found = list(rev["findings"])
        for p in self.P["map"].get("problems", []) + cv.get("problems", []):
            extra.append(engine_finding(p["code"], "ERR" if p["code"] == "choice_invalid" else "WARN",
                                         p["message"], "", ()))
        bad = cv.get("transform_failures") or []
        if bad:
            by_code: dict[tuple[str, str], list[str]] = {}
            for b in bad:
                by_code.setdefault((b["field"], b["code"]), []).append(b["src"])
            for (fld, code), srcs in sorted(by_code.items()):
                extra.append(engine_finding(
                    "transform_failed", "ERR",
                    f"{len(srcs)} value(s) of {fld} could not be read: {FAILURE_SENTENCES.get(code, code)}",
                    fld, srcs, failure=code))
        for f in extra:
            f["acknowledged"] = None
        found = [*found, *extra]  # acknowledgements belong to the approval, not to the sealed check
        # --- per-row outcomes, coverage and tie-out in one pass over the table spool ---
        marks = _Marks()
        for f in found:
            marks.add_named(f["id"], f["severity"], f.get("rows") or [])
        for rep in reports.values():
            for c in rep["causes"]:
                fid = next((f["id"] for f in found if f.get("cause") == c["id"]), c["id"])
                marks.add_runs(fid, c["severity"], c.get("row_ranges") or [])
        info = meta.get("account_mapping") or self.P["map"].get("account_mapping")
        target = info["target_field"] if info else ""
        blank_acct = [(f["id"], f["severity"]) for f in found if f.get("code") == "account_unmapped"] if info else []
        out_key = f"st/{key}/outcomes"
        outcomes = Spool(store, out_key)
        is_gl = self.template == "GL"
        filled: dict[str, int] = {}
        order: list[str] = []
        total = 0
        sums = Totals()
        counts = {"ok": 0, "warn": 0, "err": 0}
        cap = self.rules.limits.get("max_keys") if self.rules else None
        for t, row in enumerate(self._rows_of(cv)):
            total += 1
            if t == 0:
                order = [f for f in row if f != SRC]
            for f in order:
                if not is_blank(row.get(f)):
                    filled[f] = filled.get(f, 0) + 1
            src = str(row.get(SRC, ""))
            n = src_row(src)
            hit = marks.of(src, n)
            if blank_acct and target and is_blank(row.get(target)):
                hit = [*hit, *blank_acct]
            status = "ok"
            if any(s == "ERR" for _i, s in hit):
                status = "err"
            elif any(s == "WARN" for _i, s in hit):
                status = "warn"
            counts[status] += 1
            ids = list(dict.fromkeys(i for i, s in hit if s in ("ERR", "WARN")))
            why = ""
            if ids:
                top = next(f for f in found if f["id"] == ids[0])
                why = str(top["message"])[:160]
            outcomes.append({"i": t + 1, "src_row": n, "status": status, "why": why, "finding_ids": ids, "t": t})
            if not is_blank(row.get("Total_Amount")) and is_gl:  # what the file would hold, once the ERRs are cleared
                sums.add(row["Total_Amount"])
            if is_gl and row.get("Journal_ID") not in (None, ""):
                sums.journals.add(str(row["Journal_ID"]))
                if cap is not None and len(sums.journals) > int(cap):
                    raise Refused("too_large", f"more than {cap} distinct journals; a controller raises the cap.")
        skipped = self._skipped_rows(meta)
        for k, s in enumerate(skipped):
            outcomes.append({"i": total + k + 1, "src_row": src_row(s["src"]), "status": "skip",
                             "why": s["reason"], "finding_ids": [], "t": None})
        out_digest = outcomes.seal()
        coverage = self._coverage(meta, order, filled, total)
        spec_missing = self._missing(coverage, total)
        read_n = int(meta.get("input_rows") or total)
        skip_n = len(skipped)
        tie = sums.tie(read_n, total, skip_n, bool((rev.get("reconciliation") or {}).get("balanced", True)), is_gl)
        return {
            "findings": found, "reconciliation": rev.get("reconciliation"), "missing": spec_missing, "tie": tie,
            "counts": {"read": read_n, "out": total - counts["err"], "warn": counts["warn"], "err": counts["err"],
                       "skip": skip_n},
            "outcomes": out_key, "outcomes_digest": out_digest, "rows": total,
            "reconcile": self._reconcile(cv, out_key, key),
        }

    def _skipped_rows(self, meta: Mapping[str, Any]) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for x in (meta.get("existing") or {}).get("left_out", []):
            out.append({"src": str(x["src"]), "reason": f"{x['reason']}; matched {x['matched'].get('key') or x['matched'].get('name')}"})
        for r in meta.get("resolutions") or []:
            if str(r.get("action", "")).startswith("exclude"):
                out.append({"src": str(r["src"]), "reason": f"{r['action']}: {r['reason']}"})
        for x in meta.get("excluded") or []:
            out.append({"src": str(x["src"]), "reason": str(x["reason"])})
        return out

    def _coverage(self, meta: Mapping[str, Any], order: list[str], filled: dict[str, int], total: int) -> Any:
        """`templates.build_coverage` over counted columns (no column of the table is built in memory)."""
        staged = {f: _Column(total, filled.get(f, 0)) for f in order}
        sources = dict(meta.get("sources", {}))
        assumed: dict[str, str] = {}
        for step in self._steps("read"):
            for f, v in (step.get("constants") or {}).items():
                assumed[f] = f"constant supplied: {v}"
        for step in self._steps("read"):
            if step["step"] == "lift_gl_lines":  # the GP_ID of a GL line is the run's, as the tool sets it
                gp = step.get("gp_id") or self.spec.context.get("gp_id") or self.spec.sponsor
                assumed["GP_ID"] = f"constant supplied: {gp}"
        for c in self.spec.choices:
            if c.kind == "supply" and c.subject.startswith("field:"):
                assumed[c.subject.split(":", 1)[1]] = "supplied by the person"
        assumed = {f: n for f, n in assumed.items() if f not in sources}
        return build_coverage(self.template, staged, sources, assumed, {}, self.templates)

    def _missing(self, cov: Any, total: int) -> list[dict[str, Any]]:
        declared = {c.subject.split(":", 1)[1] for c in self.spec.choices
                    if c.kind == "declare_missing" and ":" in c.subject}
        required = set(self.templates[self.template].required)
        out: list[dict[str, Any]] = []
        for name, c in cov.fields.items():
            if c.status.value == "MISSING":
                reason = "declared_missing" if name in declared else "unsupplied" if name in required else "no_source"
                out.append({"field": name, "rows": total, "reason": reason})
            elif c.filled < c.total:
                out.append({"field": name, "rows": c.total - c.filled, "reason": "blank_in_source"})
        return out

    def _reconcile(self, cv: Mapping[str, Any], outcomes_key: str, key: str) -> dict[str, Any] | None:
        step = self._first_step("reconcile")
        if step is None:
            return None
        rules = self.rules
        assert rules is not None and rules.coa is not None
        coa = {k: dict(v) for k, v in rules.coa.accounts.items()}
        spec_id = str(step["check"])
        spec = R.SPEC.get(spec_id)
        if spec is None:
            raise Refused("schema_invalid", f"reconcile check {spec_id!r} is not one of {', '.join(R.CHECK_IDS)}")
        bad = {r["t"] for r in read(self.ports.store, outcomes_key) if r["status"] == "err" and r.get("t") is not None}
        rows, names = self._recon_rows(cv, step, coa, bad)
        report = None
        if step.get("report") is not None:
            ref = self.spec.sources[int(step["report"])]
            try:
                report = R.read_report(self.ports.reader.path(ref), step.get("aliases"))
            except (R.ReportError, PathRefused) as exc:
                raise Refused("schema_invalid", f"the client's report cannot be read: {exc}") from exc
        ctx = R.make_ctx(rows, {**names, **(step.get("investors") or {})})
        aliases = dict(step.get("aliases_map") or {})
        invs = {r.inv for r in rows if r.inv}
        for ref in invs:
            aliases.setdefault(ref.casefold(), ref)
        for e in self.ports.world.store("Investors"):
            for ref in invs:
                if ref in (e.get("key"), e.get("name"), e.get("source_key")):
                    for k in ("key", "name", "source_key"):
                        if e.get(k):
                            aliases.setdefault(str(e[k]).casefold(), ref)
        def resolve(line: str, tt: str) -> str:
            for t in dict.fromkeys([line.strip(), line.strip().split(" ")[0]]):
                if t in coa:
                    return t
                known = self.ports.world.known_account(t, tt)
                if known:
                    return known
            return ""

        fund_rows = lp_rows = None
        other = step.get("other")
        if other:
            fund_rows = self._load_recon_rows(other.get("fund"))
            lp_rows = self._load_recon_rows(other.get("lp"))
        result = R.run_check(spec, rows, ctx, coa, report, resolve, aliases, fund_rows, lp_rows,
                             incomplete=bool(step.get("incomplete", False)))
        spool = Spool(self.ports.store, f"st/{key}/recon_rows")
        spool.extend({"acct": r.acct, "fsg": r.fsg, "fsl": r.fsl, "amt": r.amt, "date": r.date, "inv": r.inv,
                      "tt": r.tt, "invst": r.invst, "pr": r.pr, "shares": r.shares, "agn": r.agn,
                      "offset": r.offset} for r in rows)
        spool.seal()
        return {"check": spec_id, "state": result["state"], "notes": result["notes"], "rows": result["rows"],
                "recon_rows": spool.key}

    def _load_recon_rows(self, eval_key: str | None) -> list[Any] | None:
        if not eval_key:
            return None
        idx = read_json(self.ports.store, f"eval/{eval_key}")
        if not idx or "check" not in idx.get("stages", {}):
            raise Refused("source_changed", f"evaluation {str(eval_key)[:12]} has no sealed check to reconcile "
                          "against; run it first.")
        meta = read_json(self.ports.store, f"st/{idx['stages']['check']}/meta")
        rr = (meta or {}).get("reconcile") or {}
        if not rr.get("recon_rows"):
            raise Refused("schema_invalid", f"evaluation {str(eval_key)[:12]} has no reconcile rows.")
        return [R.Row(**d) for d in read(self.ports.store, rr["recon_rows"])]

    def _recon_rows(self, cv: Mapping[str, Any], step: Mapping[str, Any], coa: Mapping[str, Any],
                    bad: set[int]) -> tuple[list[Any], dict[str, str]]:
        """The upload side as `recon.Row`s: the lines that will be written (ok or warn), each with its account,
        FS grouping and FS line item from the Golden chart; an LP offset has a blank investor."""
        ref = self._source(step)
        path = self.ports.reader.path(ref)
        columns = dict((self._first_step("lift_gl_lines") or {}).get("columns") or {})
        header_row = int(ref.header_row or 1)
        src = quality.Source(path, ref.sheet or None, header_row)
        extra_cols = {"invst": "Investment", "pr": "PR_DATE", "shares": "NO_OF_SHARES", "agn": "AGNST_COMMIT",
                      **(step.get("extra_columns") or {})}
        at = {k: src.index_of(h) for k, h in extra_cols.items()}
        extras: dict[int, dict[str, Any]] = {}
        for n, row in src.rows():
            extras[n] = {k: (row[i] if i is not None and i < len(row) else None) for k, i in at.items()}
        raw: dict[int, Any] = {n: ln for n, ln in read_gl_lines(path, ref.sheet or None, columns, header_row)}
        names: dict[str, str] = {}
        rows: list[Any] = []
        for t, row in enumerate(self._rows_of(cv)):
            if t in bad:
                continue
            src_s = str(row.get(SRC, ""))
            n = src_row(src_s)
            acct = str(row.get("Account_Number", "")).strip()
            ln = raw.get(n)
            if not acct and step.get("accounts") == "source" and ln is not None and ln.acct in coa:
                acct = ln.acct  # the source already reports in the Golden chart
            if not acct or acct not in coa:
                continue
            off = str(row.get("Event_ID", "")).endswith("-OFF")
            ac = coa[acct]
            if ln is not None and ln.inv and ln.inv not in names:
                names[ln.inv] = str(row.get("Investor_Name", "")) or ln.inv
            x = extras.get(n, {})
            shares = x.get("shares")
            rows.append(R.Row(
                acct, ac.get("FS_Grouping", ""), ac.get("FS_Line_Item", ""), float(row["Total_Amount"] or 0),
                R.parse_date(row.get("Event_Date")), "" if off else (ln.inv if ln else ""),
                str(row.get("Transaction_Type", "")), "" if off else str(x.get("invst") or ""),
                None if off else R.parse_date(x.get("pr")),
                None if off or shares in (None, "") else float(shares),
                None if off else R._yes(str(x.get("agn") or "")), off))
        return rows, names

    def _console_check(self, key: str) -> dict[str, Any]:
        """Decide every row or line of a console job, whole: the console family's checks (`engine_console`), the
        IDs each written row gets, the template rows that would be written, and the findings per cause. Severities
        are the console namespace, `console.<code>`: raised to the check-data-quality rule and floor when the skill
        has that rule, never lowered, and not unified with the agent path's."""
        step = self.console
        assert step is not None and self.rules is not None
        rd, cv = self.P["read"], self.P["convert"]
        store = self.ports.store
        meta = rd["meta"]
        sheet = str(meta.get("sheet", ""))
        gl = str(self.spec.record_type) in ("GL Fund", "GL LP")
        floors = (self.rules.controls_floor or {}).get("check-data-quality") or {}
        dec = EC.parse_choices(self.spec.choices)
        recs = list(self._rows_of(cv))
        facts: dict[str, Any] = {"sheet": sheet, "header": list(meta.get("header") or []),
                                 "kind": "gl" if gl else "static"}
        if cv.get("recipe"):  # a sponsor's recipe was applied: what it made and what it could not read
            facts["recipe"] = dict(cv["recipe"])
            facts["failures"] = [dict(x) for x in cv.get("transform_failures") or []]
        if gl:
            tally, written, records, whys = self._console_gl(step, recs, dec, facts)
        else:
            tally, written, records, whys = self._console_static(step, recs, dec, facts)
        table = Spool(store, f"st/{key}/table")
        table.extend(written)
        facts["table_digest"] = table.seal()
        facts["table"] = table.key
        facts["written"] = len(written)
        # outcomes (a page at a time later) and the causes they name
        causes: dict[tuple[str, str], list[int]] = {}
        outcomes = Spool(store, f"st/{key}/outcomes")
        finding_ids: dict[tuple[str, str], str] = {}  # one id per cause, not one lookup per row
        for idx, (detail, srow, status, reasons, t) in enumerate(records):
            ids_here: list[str] = []
            for code, name in reasons:
                causes.setdefault((code, name), []).append(srow)
                fid = finding_ids.get((code, name))
                if fid is None:
                    fid = finding_ids[(code, name)] = console_finding_id(code, name)
                ids_here.append(fid)
            outcomes.append({"i": idx + 1, "src_row": srow, "status": status, "why": whys[idx],
                             "finding_ids": ids_here, "t": t, "detail": detail})
        out_digest = outcomes.seal()
        found: list[dict[str, Any]] = []
        miss_fields: dict[str, int] = {}
        for (code, name), srows in causes.items():
            where = EC.ranges_of(srows)
            f = engine_finding(code, EC.console_level(code, floors), console_message(code, name), name,
                                [f"{sheet}!{n}" for n in srows], len(srows), console=True)
            f.pop("engine", None)
            if code == "transform_failed":  # one finding per (field, code); the value is not in it
                f["field"], _, f["failure"] = name.partition("|")
            f["ranges"] = ", ".join(where)
            f["locator"] = {"sheet": sheet, "where": ", ".join(where)}
            f["acknowledged"] = None
            found.append(f)
            if code == "optional_missing":
                miss_fields[name] = len(srows)
        accuracy = self._console_accuracy(step, gl, sheet, recs, records, written, tally, dec)
        facts["accuracy"] = [c.as_record() for c in accuracy]
        for c in AC.failed(accuracy):  # a failed proof is an ERR: it blocks the approval and, again, the write
            bad = engine_finding("accuracy_failed", "ERR", f"{c.name}: {c.detail}", c.name, [], 1)
            bad.update(ranges="", locator={"sheet": sheet, "where": ""}, acknowledged=None)
            found.append(bad)
        read_n, offsets = int(tally["read"]), int(facts.get("offsets", 0))
        accounted = tally["err"] + tally["skip"] + tally["out"] - offsets
        recon = {"input_rows": read_n, "output_rows": len(written), "unresolved": tally["err"],
                 "excluded": tally["skip"], "generated": offsets, "unexplained": read_n - accounted,
                 "balanced": read_n == accounted}
        sums = Totals()
        if gl:
            for r in written:
                if not is_blank(r.get("Total_Amount")):
                    sums.add(r["Total_Amount"])
                sums.journals.add(str(r.get("Journal_ID", "")))
        return {
            "findings": found, "reconciliation": recon,
            "missing": [{"field": f, "rows": n, "reason": "unsupplied"} for f, n in sorted(miss_fields.items())],
            "tie": sums.tie(read_n, tally["out"], tally["skip"], bool(recon["balanced"]), gl),
            "counts": {"read": read_n, "out": tally["out"], "warn": tally["warn"], "err": tally["err"],
                       "skip": tally["skip"]},
            "outcomes": outcomes.key, "outcomes_digest": out_digest, "rows": len(written),
            "console": facts,
        }

    def _console_accuracy(self, step: Mapping[str, Any], gl: bool, sheet: str, recs: Sequence[Mapping[str, Any]],
                          records: Sequence[Any], written: Sequence[Mapping[str, Any]], tally: Mapping[str, int],
                          dec: Any) -> list[AC.Check]:
        """The accuracy checks of a console job, each a query over the rows (never a loop of their own):
        conservation of rows, source-to-file totals and copied values (GL), and the accounting of source columns."""
        kept = [(rec, r) for rec, r in zip(recs, records, strict=False) if r[2] in ("ok", "warn")] if gl else []
        generated = int(tally["out"]) - len(kept) if gl else 0  # the offsets actually written
        out = [AC.conservation(int(tally["read"]), int(tally["out"]),
                               {"excluded by a decision": int(tally["skip"]), "blocked, not written": int(tally["err"])},
                               generated)]
        path = self.ports.reader.path(self._source(step))
        header, filled = columnar.column_fill(path, sheet or None)
        columns = {h: n for h, n in zip(header, filled, strict=False) if h}
        if gl:
            lp = str(self.spec.record_type) == "GL LP"
            src = RowTable.from_rows([
                {"_src": f"{sheet}!{rec['row']}", "j": rec["j"], "acct": rec["acct"], "tt": rec["tt"],
                 "date": rec["date"], "dr": str(rec["dr"]), "cr": str(rec["cr"]),
                 "off": "1" if lp and rec["inv"] else ""} for rec, _r in kept],
                ["j", "acct", "tt", "date", "dr", "cr", "off"])
            tgt = RowTable.from_rows(
                [{**written[r[4]], "_src": f"{sheet}!{r[1]}"} for r in records if r[4] is not None],
                [*self.templates[self.template].fields])
            expected_net = src.scalar(
                f"coalesce(sum(CASE WHEN {src.col('off')} = '1' THEN 0 ELSE round("
                f"CAST({src.col('dr')} AS DECIMAL(38,12)) - CAST({src.col('cr')} AS DECIMAL(38,12)), 2) END), 0)")
            try:
                net, _n = tgt.exact_sum("Total_Amount")
                actual = {"rows": len(tgt), "net": net}
            except Exception as exc:  # noqa: BLE001  an amount that is not exact is a failed proof, not a crash
                actual = {"rows": len(tgt), "net": f"not exact: {exc}"}
            out.append(AC.control_totals({"rows": int(tally["out"]), "net": Decimal(str(expected_net))}, actual,
                                         "the staged file against the source lines"))
            out.append(AC.paired_values(
                src, tgt, [("j", "Journal_ID"), ("acct", "Account_Name"), ("tt", "Transaction_Type"),
                           ("date", "Event_Date")], f"pystrip({tgt.col('Event_ID')}) NOT LIKE '%-OFF'"))
            reads = EC.gl_header_reads(header)
            out.append(AC.cell_accounting(
                columns, {h: f"GL line part {p}" for h, p in reads.items()},
                {h: "not a field of the GL upload template" for h in columns if h not in reads}))
        else:
            words = EC.Words(str(step.get("sponsor", self.spec.sponsor)))
            used: dict[str, str] = {}
            for name, source, *_rest in EC.plan_with(step["plan"], dec.colmap):
                if not source or source == "generated" or str(source).startswith(("(", "constant")):
                    continue
                h = words.detect(name, source, header)
                if h:
                    used.setdefault(h, name)
            out.append(AC.cell_accounting(columns, used, {h: "not a field of this upload template"
                                                           for h in columns if h not in used}))
        return out

    def _console_static(self, step: Mapping[str, Any], recs: list[dict[str, Any]], dec: Any, facts: dict[str, Any]
                        ) -> tuple[dict[str, int], list[dict[str, Any]], list[Any], list[str]]:
        typ = str(self.spec.record_type)
        world = self.ports.world
        tspec = self.templates[self.template]
        tgt, mem = RF.target(typ), RF.membership()
        # the target file's ID format (prefix, width), unless the sponsor's person decided another (the job's step)
        scheme = {**tgt.id_generation, **dict(step.get("id_scheme") or {}), "namecode_len": tgt.namecode_length}
        rules = dict(step["rules"])
        header = list(self.P["read"]["meta"].get("header") or [])
        path = self.ports.reader.path(self._source(step))
        words = EC.Words(str(step.get("sponsor", self.spec.sponsor)))
        sspec = EC.StaticSpec(
            record_type=typ, job_id=self.spec.job_id, plan=[list(f) for f in step["plan"]], rules=rules,
            key_col=str(step["key_col"]), name_col=str(step["name_col"]), ent_col=str(step.get("ent_col", "")),
            amount_col=str(step.get("amount_col", "")), date_col=str(step.get("date_col", "")),
            id_limit=int(tgt.max_length or tspec.id_max_len or 20), id_prefix=str(scheme.get("prefix", "")),
            id_pad=int(scheme.get("width", 0) or 0), gen=typ in mem.gen, regt=typ in mem.regt,
            idgen_field=str(mem.idgen.get(typ, "")), id_scheme=scheme,
            gl_only=EC.gl_only_test(path, words) if typ == "Investments" else None,
            sponsor_slug=str(step.get("sponsor", self.spec.sponsor)), fixed_ids=dict(step.get("fixed_ids") or {}),
            entities=list(step.get("entities") or []), fund=str(step.get("fund", "")),
            entity_ids=dict(step.get("entity_ids") or {}))
        plan = EC.plan_with(sspec.plan, dec.colmap)
        sources = EC.source_rows(sspec, plan, recs)  # made once for both passes
        rows = EC.eval_static(sspec, header, recs, dec, world, words, sources)
        counter = int(world.counter(typ)) if hasattr(world, "counter") else 1
        taken = [str(e["key"]) for e in world.store(typ) if e.get("key") and e.get("job") != sspec.job_id]
        ids, gen = EC.issue_ids(sspec, rows, counter, taken)
        name_field = next((f[0] for f in plan if f[0].endswith("_Name")), "")
        written = EC.static_template_rows(sspec, tspec.fields, header, recs, rows, ids, dec, world, tgt.key[-1],
                                          name_field, words, sources)
        index: dict[int, int] = {}
        for r in rows:
            if r["st"] in ("ok", "warn"):
                index[r["i"]] = len(index)
        records: list[Any] = []
        for r in rows:
            reasons: list[tuple[str, str]] = []
            for code in r.get("codes", []):
                if code == "required_blank":
                    reasons += [(code, f) for f in r["blank"]]
                elif code == "optional_missing":
                    reasons += [(code, f[5:]) for f in r["fl"] if f.startswith("miss:")]
                elif code == "transform_failed":
                    reasons += [(code, f"{x['field']}|{x['code']}") for x in r.get("failed", [])]
                else:
                    reasons.append((code, ""))
            records.append((dict(r), int(r["row"]), r["st"], reasons, index.get(r["i"])))
        facts["ids"] = {str(k): v for k, v in sorted(ids.items())}
        facts["gen"] = gen
        facts["issued"] = [] if sspec.fixed_ids else [
            ids[r["i"]] for r in rows if r["st"] in ("ok", "warn") and r["i"] in ids
            and (sspec.gen or not (r["nid"] or re.search(r"generated ID (.+)$", r["x"] or "")))]
        return EC.static_tally(rows), written, records, [str(r["why"]) for r in rows]

    def _console_gl(self, step: Mapping[str, Any], recs: list[dict[str, Any]], dec: Any, facts: dict[str, Any]
                    ) -> tuple[dict[str, int], list[dict[str, Any]], list[Any], list[str]]:
        typ = str(self.spec.record_type)
        world = self.ports.world
        slug = str(step.get("sponsor", self.spec.sponsor))
        fund_id, fund_name = str(step.get("fund_id", "")), str(step.get("fund_name", ""))
        gspec = EC.GlSpec(typ, dict(step["rules"]), list(step.get("chart_accounts") or []),
                          list(step.get("txn_types") or []), slug, fund_id, fund_name)
        e = EC.eval_gl(gspec, recs, dec, world)
        investors = EC.known_view(world.store("Investors"))

        by_ref: dict[Any, dict[str, Any]] = {}
        for x in investors:  # the first investor holding a reference, indexed once
            for ref_value in (x.get("id"), x.get("key"), x.get("name")):
                by_ref.setdefault(ref_value, x)

        def investor(ref: str) -> tuple[str, str]:
            hit = by_ref.get(ref)
            return (hit["id"], hit["name"]) if hit else ("", "")

        def account(acct: str, tt: str) -> str:
            picked = dec.acct.get(acct)
            return str(picked).partition(" ")[0] if picked else world.known_account(acct, tt)

        mk = GlRows(self.templates[self.template], slug, fund_id, fund_name, account, investor)
        written: list[dict[str, Any]] = []
        records: list[Any] = []
        whys: list[str] = []
        for x in e["lines"]:
            t = None
            if x["st"] in ("ok", "warn"):
                t = len(written)
                written.append(mk.row(x["l"]))
            reasons = [(c, "") for c in dict.fromkeys(x["codes"])] if x["st"] in ("err", "warn") else []
            detail = {"l": EC.line_dict(x["l"]), "errs": x["errs"], "msgs": x["msgs"], "off": False,
                      "codes": [c for c, _f in reasons]}
            records.append((detail, int(x["row"]), x["st"], reasons, t))
            whys.append("; ".join([m["m"] for m in x["errs"]] + x["msgs"]) or "Resolved, written as is")
        for o in e["offsets"]:
            t = None
            if o["st"] == "ok":
                t = len(written)
                written.append(mk.row(o["l"], offset=True))
            records.append(({"l": EC.line_dict(o["l"]), "errs": [], "msgs": [], "off": True, "codes": []},
                            int(o["row"]), o["st"], [], t))
            whys.append("")
        facts["journals"] = [{"j": j["j"], "dr": j["dr"], "cr": j["cr"], "n": j["n"], "st": j["st"],
                              "diff": j["diff"]} for j in e["js"]]
        facts["offsets"] = len(e["offsets"])
        facts["issued"] = []
        return EC.gl_tally(e), written, records, whys


class _Column:
    """A column as `build_coverage` counts it: `n` values of which `filled` are not blank."""

    def __init__(self, n: int, filled: int) -> None:
        self.n, self.filled = n, filled

    def __len__(self) -> int:
        return self.n

    def __iter__(self) -> Iterator[str]:
        for i in range(self.n):
            yield "x" if i < self.filled else ""


class _Marks:
    """Which finding names a row: exact ranges from the streaming checks, and the rows a review finding names."""

    def __init__(self) -> None:
        self.named: dict[str, list[tuple[str, str]]] = {}
        self.runs: list[tuple[list[int], list[int], str, str]] = []

    def add_named(self, fid: str, severity: str, srcs: Iterable[str]) -> None:
        for s in srcs:
            self.named.setdefault(str(s), []).append((fid, severity))

    def add_runs(self, fid: str, severity: str, runs: Iterable[Sequence[int]]) -> None:
        ordered = sorted((int(a), int(b)) for a, b in runs)
        if ordered:
            self.runs.append(([a for a, _b in ordered], [b for _a, b in ordered], fid, severity))

    def of(self, src: str, n: int) -> list[tuple[str, str]]:
        out = list(self.named.get(src, ()))
        for starts, ends, fid, sev in self.runs:
            i = bisect_right(starts, n) - 1
            if i >= 0 and n <= ends[i]:
                out.append((fid, sev))
        return out


class _ReviewCtx:
    """What `review.review_table` and its parts read from a run context, over the engine's ports: no ctx of the
    agent path is needed to reuse its checks."""

    def __init__(self, sponsor: str, gp_id: str, world: Any, reports: Mapping[str, dict[str, Any]]) -> None:
        self.sponsor = sponsor
        self.gp_id = gp_id
        self.registry = self  # its `entries` is what the checks ask of a registry
        self._world = world
        self.quality_reports = dict(reports)

    def entries(self, record_type: str) -> list[dict[str, Any]]:
        return list(self._world.store(record_type))
