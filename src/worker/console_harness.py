"""View models and actions of the console's harness pages: Improvements, Health, Skills and a skill's tables.

Everything is computed from real state: the improvement cards on disk, the audit of finished jobs (the harness
metrics), the skills folder and its decision tables. A page that has nothing to show says so; no figure is a
placeholder. Editing a decision table follows the same road as any improvement: Test replays the recorded cases
with the edited table, Propose files a card through the replay gates, and a named human promotes it.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from worker import metrics, persist
from worker import recon as R
from worker.accounts import Versions, key_of, map_keys
from worker.decide import Table, TableError
from worker.errors import StatusError
from worker.improver import SLOW_KINDS, Outcome, ReplayCase, build_cases, cluster, evaluate, info_from_key
from worker.skills import Skill, load_skills

WEEKS = 8  # weeks of history in the Health sparklines
EVIDENCE_NEEDED = 3  # similar corrections before the improver proposes a change
REPLAYABLE = {("map-accounts", "coa-cascade")}  # tables with a recorded-case evaluator behind them
RECON = R.SKILL  # every table of this skill is replayed on its known-GL cases


def _replayable(skill: str, table: str) -> bool:
    return (skill, table) in REPLAYABLE or skill == RECON


class HarnessError(StatusError):
    """A request the harness pages cannot honour; `status` is the HTTP status to answer with."""


# ---- Health -----------------------------------------------------------------------------------------------
def _week_of(iso: str) -> str | None:
    try:
        year, week, _ = datetime.fromisoformat(iso).isocalendar()
    except (TypeError, ValueError):
        return None
    return f"{year}-W{week:02d}"


def _promoted_weeks(manager: Any) -> set[str]:
    """Weeks in which an improvement went live: promoted cards, and the improver entries of the change log."""
    weeks: set[str] = set()
    for card in _store(manager).cards():
        if card.get("promoted"):
            w = _week_of(str(card["promoted"]))
            if w:
                weeks.add(w)
    for c in metrics.ChangeLog(manager.state_dir / "changes.jsonl").all():
        if c.get("tag") == "improver":
            w = _week_of(str(c.get("ts")))
            if w:
                weeks.add(w)
    return weeks


def _pct(v: float | None) -> int | None:
    return None if v is None else round(v * 100)


def _readiness(jobs: Sequence[dict[str, Any]]) -> int | None:
    keys = sum(j["keys"] for j in jobs)
    return round(100 * sum(j["keys_without_model"] for j in jobs) / keys) if keys else None


def health_view(manager: Any, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    jobs = metrics.collect_jobs(manager.runs_dir)
    prices = metrics._prices(env)
    priced = bool(prices["in_per_m"] or prices["out_per_m"] or prices["reviewer_per_hour"])
    out: dict[str, Any] = {"definition": metrics.DEFINITION_VERSION, "jobs": len(jobs), "priced": priced,
                           "reviewer_rate": prices["reviewer_per_hour"], "rows": [], "watch": [], "weeks": []}
    if not jobs:
        out["empty"] = "No finished job has been recorded yet. Health fills in as jobs run and are approved."
        return out
    fn = metrics.DEFINITIONS[metrics.DEFINITION_VERSION]
    all_weeks = sorted({j["week"] for j in jobs})[-WEEKS:]
    promoted = _promoted_weeks(manager)
    lat = metrics.latency(manager.runs_dir)["per_1000_rows"]
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for j in jobs:
        by_type[j["record_type"]].append(j)
    for rt, js in sorted(by_type.items()):
        m = fn(js, prices)
        series = []
        for w in all_weeks:
            v = _readiness([j for j in js if j["week"] == w])
            if v is not None:
                series.append({"week": w, "value": v, "promoted": w in promoted})
        spread = lat.get(rt)
        out["rows"].append({
            "workflow": rt, "jobs": m["jobs"], "rows": m["rows"], "readiness": series,
            "overrides_per_1000": m["overrides_per_1000_rows"], "questions_per_1000": m["questions_per_1000_rows"],
            "decided_without_model": _pct(m["keys_resolved_without_model"]),
            "first_pass": _pct(m["first_pass_rate"]), "upload_rejections": m["upload_rejections"],
            "model_calls_per_1000": m["model_calls_per_1000_rows"], "success_rate": _pct(m["success_rate"]),
            "cost_per_successful_job": m["cost_per_successful_job"],
            "cost_model": m["cost_breakdown"]["model_spend"], "cost_review": m["cost_breakdown"]["reviewer_cost"],
            "retries_per_job": round(m["cost_breakdown"]["retries"] / m["jobs"], 2) if m["jobs"] else 0,
            "seconds_per_1000_p50": spread["p50"] if spread else None,
            "seconds_per_1000_p95": spread["p95"] if spread else None,
        })
    out["weeks"] = all_weeks
    out["watch"] = _watch(jobs, all_weeks, promoted, metrics.harness_metrics(manager.runs_dir, env=env)["skill_usage"])
    return out


def _watch(jobs: Sequence[dict[str, Any]], weeks: Sequence[str], promoted: set[str],
           usage: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for skill, u in sorted(usage.items()):
        row: dict[str, Any] = {"skill": skill, "loaded": u["loaded"], "needed": u["needed"],
                               "loaded_not_needed": u["loaded_not_needed"], "needed_not_loaded": u["needed_not_loaded"]}
        if skill in metrics.EVIDENCE:
            series = []
            for w in weeks:
                v = _readiness([j for j in jobs if j["week"] == w and skill in j["skills_needed"]])
                if v is not None:
                    series.append({"week": w, "value": v, "promoted": w in promoted})
            row["trend"] = series
        rows.append(row)
    return rows


# ---- Skills -----------------------------------------------------------------------------------------------
def _skills() -> dict[str, Skill]:
    return load_skills()


def _case_count(skill: Skill) -> int:
    n = 0
    for p in sorted((skill.path / "cases").glob("*.json")) if (skill.path / "cases").is_dir() else []:
        try:
            n += len(json.loads(p.read_text(encoding="utf-8")).get("cases", []))
        except (OSError, ValueError):
            continue
    return n


def _table_ids(skill: Skill) -> list[str]:
    d = skill.path / "tables"
    return sorted(p.stem for p in d.glob("*.yaml")) if d.is_dir() else []


def skills_view(manager: Any) -> dict[str, Any]:
    usage = metrics.harness_metrics(manager.runs_dir)["skill_usage"]
    rows = []
    for name, s in _skills().items():
        rows.append({"name": name, "version": s.version, "stamp": s.stamp, "description": s.description,
                     "tables": _table_ids(s), "cases": _case_count(s),
                     "jobs_used": usage.get(name, {}).get("loaded", 0)})
    out: dict[str, Any] = {"skills": rows}
    if not rows:
        out["empty"] = "No skills are installed."
    return out


def _cell(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, Mapping) and "in" in v:
        return "in " + ", ".join(str(x) for x in v["in"])
    return "" if v is None else str(v)


def _table_view(skill: Skill, tid: str) -> dict[str, Any]:
    t = skill.table(tid)
    outputs: list[str] = []
    for r in t.rows:
        for k in r["then"]:
            if k not in outputs:
                outputs.append(k)
    return {
        "id": t.id, "description": t.description, "hit": t.hit, "inputs": dict(t.inputs), "outputs": outputs,
        "replayable": _replayable(skill.name, t.id),
        "rows": [{"id": r["id"], "when": {k: _cell(v) for k, v in (r.get("when") or {}).items()},
                  "then": {k: _cell(v) for k, v in r["then"].items()}} for r in t.rows],
    }


def _checks_view(skill: Skill) -> list[dict[str, Any]]:
    return [{"id": str(r.get("id")), "severity": str(r.get("severity", "")), "family": str(r.get("family", "")),
             "message": str(r.get("message", ""))} for r in (skill.checks().get("rules") or [])]


def skill_view(manager: Any, name: str) -> dict[str, Any]:
    skill = _skills().get(name)
    if skill is None:
        raise HarnessError(f"no skill named {name!r}", 404)
    overlay = _overlay_rows(manager, name)
    tables = []
    for tid in _table_ids(skill):
        tv = _table_view(skill, tid)
        tv["live_overlay"] = [r["id"] for r in overlay.get(tid, [])]
        tables.append(tv)
    out: dict[str, Any] = {"name": name, "stamp": skill.stamp, "description": skill.description, "tables": tables,
                           "checks": _checks_view(skill)}
    if name == RECON:
        rules = R.rules_for(None, manager.state_dir)
        out["hash"] = rules.digest
        out["controls"] = {
            "editable": False, "who": "a named controller who is not the proposer; the improver cannot propose a "
            "change here and the fast loop never promotes one", **rules.view()}
    return out


# ---- decision-table edits: parse, test, propose -----------------------------------------------------------
def _parse_cell(raw: Any) -> Any:
    s = str(raw).strip()
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    if s.startswith("in "):
        return {"in": [x.strip() for x in s[3:].split(",") if x.strip()]}
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return s


def _rows_from_cells(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i, r in enumerate(rows, start=1):
        rid = str(r.get("id", "")).strip()
        if not rid:
            raise HarnessError(f"row {i} needs an id")
        when = {k: _parse_cell(v) for k, v in (r.get("when") or {}).items()
                if str(v).strip() != ""}
        then = {k: _parse_cell(v) for k, v in (r.get("then") or {}).items() if str(v).strip() != ""}
        if not then:
            raise HarnessError(f"row {rid} needs at least one result")
        row: dict[str, Any] = {"id": rid, "then": then}
        if when:
            row["when"] = when
        out.append(row)
    return out


def _candidate(skill: Skill, tid: str, cells: Sequence[Mapping[str, Any]]) -> tuple[Table, Table]:
    if tid not in _table_ids(skill):
        raise HarnessError(f"{skill.name} has no table {tid!r}", 404)
    base = skill.table(tid)
    rows = _rows_from_cells(cells)
    try:
        cand = Table.from_dict({"id": base.id, "inputs": base.inputs, "rows": rows, "description": base.description})
        # a table must always decide: its last row has to be a catch-all
        if not rows or rows[-1].get("when"):
            raise TableError(f"table {base.id}: the last row must match anything (leave its conditions empty)")
    except TableError as exc:
        raise HarnessError(str(exc)) from exc
    return base, cand


def _fixture_run(manager: Any, table: Table) -> dict[str, dict[str, Any]]:
    replay = manager._improvement_parts()[0]
    data = json.loads((_skills()["map-accounts"].path / "cases" / "cascade.json").read_text(encoding="utf-8"))
    out: dict[str, dict[str, Any]] = {}
    for c in data["cases"]:
        key = key_of(c["account_type"], c["account"], c["transaction_type"])
        info = {"account_type": c["account_type"], "account": c["account"],
                "transaction_type": c["transaction_type"], "rows": 1}
        r = map_keys({key: info}, replay.coa, replay.types, data.get("sponsor_store", {}), {}, table)[0]
        exp = c["expect"]
        out[c["name"]] = {"ok": all(getattr(r, f) == v for f, v in exp.items()), "row": r.row, "action": r.action}
    return out


def _recorded_cases(manager: Any) -> list[ReplayCase]:
    entries = {k: v for k, v in persist.account_stores(manager.state_dir / "accounts").items() if k != "_golden"}
    keys = {}
    for e in entries.values():
        for k in e:
            try:
                keys[k] = info_from_key(k)
            except ValueError:
                continue
    return build_cases(entries, keys)


def test_table(manager: Any, skill: str, table: str, cells: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Replay recorded jobs with the edited table. Reads only; nothing is saved."""
    s = _skills().get(skill)
    if s is None:
        raise HarnessError(f"no skill named {skill!r}", 404)
    base, cand = _candidate(s, table, cells)
    if skill == RECON:
        return _test_recon(manager, base, cand)
    if (skill, table) not in REPLAYABLE:
        return {"ok": True, "replayable": False, "passed": False,
                "message": "There is no recorded job set to replay this table against yet, so it cannot be tested."}
    before, after = _fixture_run(manager, base), _fixture_run(manager, cand)
    broke = sorted(n for n in before if before[n]["ok"] and not after[n]["ok"])
    fixed = sorted(n for n in before if not before[n]["ok"] and after[n]["ok"])
    replay, golden = manager._improvement_parts()[0], manager._improvement_parts()[2]
    cases = _recorded_cases(manager)
    gate = evaluate(cases, replay.run(cases, dict(golden.current())), replay.run(cases, dict(golden.current()), cand))
    passed = not broke and not gate.regressions and not gate.signed_off_changed and cand.rows != base.rows
    return {
        "ok": True, "replayable": True, "passed": passed, "changed": cand.rows != base.rows,
        "fixtures": {"total": len(before), "pass_before": sum(v["ok"] for v in before.values()),
                     "pass_after": sum(v["ok"] for v in after.values()), "broke": broke, "fixed": fixed},
        "recorded": {"cases": len(cases), "fixes": len(gate.fixes), "regressions": len(gate.regressions),
                     "signed_off_changed": len(gate.signed_off_changed), "asked_before": gate.asked_before,
                     "asked_after": gate.asked_after},
    }


def _changed_rows(base: Table, cand: Table) -> list[dict[str, Any]]:
    mine = {r["id"]: r for r in base.rows}
    return [r for r in cand.rows if mine.get(r["id"]) != r]


def propose_table(manager: Any, skill: str, table: str, cells: Sequence[Mapping[str, Any]], scope: str,
                  sponsor: str, who: str) -> dict[str, Any]:
    """File one card per changed row through the replay gates; the card is `ready` or `blocked`."""
    if scope not in ("sponsor", "organisation"):
        raise HarnessError("scope must be sponsor or organisation")
    s = _skills().get(skill)
    if s is None:
        raise HarnessError(f"no skill named {skill!r}", 404)
    if not _replayable(skill, table):
        raise HarnessError("there is no recorded job set to replay this table against, so it cannot be proposed")
    base, cand = _candidate(s, table, cells)
    changed = _changed_rows(base, cand)
    if not changed:
        raise HarnessError("nothing differs from the live table")
    replay, store, golden = manager._improvement_parts()
    cases = _recorded_cases(manager)
    if skill == RECON:
        replay, cases, golden = ReconReplay(manager, base), _recon_cases(manager), None
    filed = []
    for row in changed:
        proposal = {
            "kind": "table_row", "target": f"skills/{skill}/tables/{table}.yaml", "scope": scope,
            "change": {"row": row}, "evidence": {"sponsor": sponsor, "sponsors": [sponsor] if sponsor else [],
                                                 "count": 1, "table": table, "skill": skill},
            "proposed_by": who or "console", "speed": "fast",
            "summary": f"{who or 'console'} edits row {row['id']} of {table}",
        }
        filed.append(store.file(proposal, cases, replay, _overlay(manager, skill, table), golden=golden))
    return {"cards": [c["id"] for c in filed], "status": [c["status"] for c in filed]}


# ---- reconcile-gl: replay of the known GL cases --------------------------------------------------------------
def _recon_cases(manager: Any) -> list[ReplayCase]:
    """The skill's known-GL cases (signed off) and the analyst's recorded ones in `<state>/recon_cases/*.json`."""
    base_dir = _skills()[RECON].path / "cases"
    docs = [(p, True) for p in sorted(base_dir.glob("*.json"))]
    rec = manager.state_dir / "recon_cases"
    docs += [(p, False) for p in sorted(rec.glob("*.json"))] if rec.is_dir() else []
    out: list[ReplayCase] = []
    root: dict[str, Any] = {}
    for path, signed in docs:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if signed and not root:
            root = doc
        merged = {**root, **doc, "fixtures": {**root.get("fixtures", {}), **doc.get("fixtures", {})}}
        for c in doc.get("cases", []):
            labels = sorted(c["expect"].get("figures", {}))
            want = json.dumps({"state": c["expect"]["state"], "worst": c["expect"]["worst"],
                               "figures": c["expect"].get("figures", {})}, sort_keys=True)
            out.append(ReplayCase(f"{path.stem}:{c['name']}", "fixture" if signed else "recorded", "GL", c["name"],
                                  {"doc": merged, "case": c, "labels": labels}, want, signed))
    return out


def _coa_dict() -> dict[str, dict[str, str]]:
    from worker.accounts import Coa
    from worker.templates import standards_dir

    return {k: dict(v) for k, v in Coa.load(standards_dir() / "Golden_COA.csv").accounts.items()}


class ReconReplay:
    """Replays reconcile-gl cases with a candidate table, in the shape `ImprovementStore.file` expects."""

    def __init__(self, manager: Any, table: Table) -> None:
        self.manager, self.table = manager, table
        self.coa = _coa_dict()

    def run(self, cases: Sequence[ReplayCase], golden: Mapping[str, Any], table: Table | None = None
            ) -> dict[str, Outcome]:
        overlay = R.read_overlay(self.manager.state_dir)
        overlay.pop(self.table.id, None)  # the table under test replaces the promoted rows of its own id
        rules = R.load_rules(overlay=overlay, table_override={self.table.id: table or self.table})
        out: dict[str, Outcome] = {}
        for c in cases:
            res = R.replay_case(rules, c.info["doc"], c.info["case"], self.coa)
            out[c.case_id] = Outcome("apply", R.case_signature(res, c.info["labels"]))
        return out


def _test_recon(manager: Any, base: Table, cand: Table) -> dict[str, Any]:
    cases = _recon_cases(manager)
    replay = ReconReplay(manager, base)
    before, after = replay.run(cases, {}), replay.run(cases, {}, cand)
    ok = {c.case_id: (before[c.case_id].applied == c.expected, after[c.case_id].applied == c.expected) for c in cases}
    fixtures = [c for c in cases if c.signed_off]
    broke = sorted(c.case_id for c in cases if ok[c.case_id][0] and not ok[c.case_id][1])
    fixed = sorted(c.case_id for c in cases if not ok[c.case_id][0] and ok[c.case_id][1])
    gate = evaluate(cases, before, after)
    changed = cand.rows != base.rows
    return {
        "ok": True, "replayable": True, "changed": changed,
        "passed": changed and not broke and not gate.regressions and not gate.signed_off_changed,
        "fixtures": {"total": len(cases), "pass_before": sum(v[0] for v in ok.values()),
                     "pass_after": sum(v[1] for v in ok.values()), "broke": broke, "fixed": fixed,
                     "known_gl": len(fixtures)},
        "recorded": {"cases": len(cases), "fixes": len(gate.fixes), "regressions": len(gate.regressions),
                     "signed_off_changed": len(gate.signed_off_changed), "asked_before": gate.asked_before,
                     "asked_after": gate.asked_after},
    }


# ---- improvement cards -------------------------------------------------------------------------------------
def _store(manager: Any) -> Any:
    return manager._improvement_parts()[1]


def _overlay(manager: Any, skill: str, table: str) -> Versions:
    return persist.versions(manager.state_dir / "table_versions" / skill / table, lambda: {"rows": []})


def _overlay_rows(manager: Any, skill: str) -> dict[str, list[dict[str, Any]]]:
    out = {}
    for tid in _table_ids(_skills()[skill]):
        rows = _overlay(manager, skill, tid).current().get("rows", [])
        if rows:
            out[tid] = rows
    return out


def _row_text(row: Mapping[str, Any] | None) -> str:
    if not row:
        return "No such row yet."
    when = row.get("when") or {}
    cond = " and ".join(f"{k} {_cell(v) if str(_cell(v))[:1] in '<>=!' else 'is ' + _cell(v)}"
                        for k, v in when.items()) or "anything else"
    return f"{row['id']}: when {cond} then " + ", ".join(f"{k} = {_cell(v)}" for k, v in row["then"].items())


def _gates(card: Mapping[str, Any]) -> list[list[str]]:
    if card.get("gates"):  # a card that carries its own gates (an uploaded standard)
        return [list(g) for g in card["gates"]]
    r = card.get("replay") or {}
    if card["proposal"]["kind"] == "control":
        return [["Replay", "information only: a replay cannot show that a control change is acceptable", "ok"],
                ["Approver", "a controller who is not the proposer", "err"]]
    fixes, hold = len(r.get("fixes", [])), (r.get("holdout") or {}).get("fixes", 0)
    out = [["Fixes", f"{fixes} recorded case{'s' if fixes != 1 else ''}", "ok" if fixes or hold else "err"]]
    so = len(r.get("signed_off_changed", []))
    out.append(["Signed-off outputs changed", str(so) if not so else f"{so} would change", "err" if so else "ok"])
    bad = sorted(k for k, v in (r.get("by_split") or {}).items() if v.get("regressions"))
    out.append(["Regression on any split", "none" if not bad and not r.get("regressions") else ", ".join(bad) or "yes",
                "err" if bad or r.get("regressions") else "ok"])
    out.append(["Noise", "deterministic replay, none", "ok"])
    qb, qa, mb, ma = (r.get("asked_before", 0), r.get("asked_after", 0), r.get("model_calls_before", 0),
                      r.get("model_calls_after", 0))
    more = qa > qb or ma > mb
    out.append(["Budget", f"questions {qb} → {qa}, model calls {mb} → {ma}", "err" if more else "ok"])
    return out


def _skill_stamp(card: Mapping[str, Any]) -> str:
    p = card["proposal"]
    name = (p.get("evidence") or {}).get("skill") or "map-accounts"
    s = _skills().get(name)
    return s.stamp if s else name


def _card_view(card: Mapping[str, Any], golden_now: Mapping[str, Any], coa: Any) -> dict[str, Any]:
    p = card["proposal"]
    ev = p.get("evidence") or {}
    sponsors = ev.get("sponsors") or ([ev["sponsor"]] if ev.get("sponsor") else [])
    change = p["change"]
    if p["kind"] == "golden_mapping":
        have = golden_now.get(change["key"])
        before = (f"Mapped to {have['account']}." if have else
                  "No approved mapping for this key: the cascade suggests and a person confirms.")
        after = f"{change['key']} → {change['account']} {coa.name(change['account'])} (L0 from now on)."
    elif p["kind"] == "standard":
        before, after = change.get("before", ""), change.get("after", "")
    elif p["kind"] == "control":
        before, after = "The controls in force.", p.get("summary", "A change to the controls.")
    else:
        sk = _skills().get(ev.get("skill") or "map-accounts") or _skills()["map-accounts"]
        table = Table.load(sk.path / "tables" / f"{ev.get('table', 'coa-cascade')}.yaml")
        mine = next((r for r in table.rows if r["id"] == change["row"]["id"]), None)
        before, after = _row_text(mine), _row_text(change["row"])
    n = len((card.get("replay") or {}).get("fixes", [])) or int(ev.get("count") or 0)
    pred = int((card.get("predicted") or {}).get("fixes", 0))
    return {
        "id": card["id"], "status": card["status"], "scope": "org" if p["scope"] == "organisation" else "sponsor",
        "sp": sponsors[0] if len(sponsors) == 1 else "", "cause": p["summary"], "skill": _skill_stamp(card),
        "kind": "slow loop" if p["kind"] in SLOW_KINDS else "fast loop", "surface": p["kind"], "ev": n,
        "before": before, "after": after, "gates": _gates(card),
        "reasons": (["changes a control: another controller must approve it"]
                    if card["status"] == "awaiting_controller" else []) + (card.get("replay") or {}).get("reasons", []),
        "pred": f"fixes {pred} recorded case{'s' if pred != 1 else ''}" if pred else "",
        "version": card.get("version"), "by": card.get("proposed_by") or p.get("proposed_by"),
        "approved_by": card.get("approved_by"), "promoted": card.get("promoted"),
    }


def improvements_view(manager: Any, sponsor: str = "", admin: bool = False) -> dict[str, Any]:
    replay, store, golden = manager._improvement_parts()
    cards = store.cards()
    gnow = golden.current()
    views = [_card_view(c, gnow, replay.coa) for c in cards]
    history = [v for v in views if v["status"] in ("promoted", "rolled_back", "rejected")]
    latest: dict[str, int] = {}
    for v in history:
        if v["status"] == "promoted" and v["version"] is not None:
            latest[v["surface"]] = max(latest.get(v["surface"], 0), v["version"])
    for v in history:
        v["can_rollback"] = (v["status"] == "promoted" and v["surface"] != "standard"
                             and v["version"] == latest.get(v["surface"]))
    return {
        "golden_version": golden.version, "admin": admin,
        "ready": [v for v in views if v["status"] == "ready"],
        # a card that changes a control waits here, with its status saying it needs another controller
        "blocked": [v for v in views if v["status"] in ("blocked", "awaiting_controller")],
        "waiting": _waiting(manager, cards),
        "history": sorted(history, key=lambda v: str(v["promoted"] or ""), reverse=True),
    }


def _waiting(manager: Any, cards: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    root = manager.state_dir / "feedback"
    items: list[dict[str, Any]] = []
    for sponsor in persist.feedback_sponsors(root):
        for f in persist.feedback(root, sponsor).list():
            items.append({**f, "sponsor": sponsor})
    filed = {(c["proposal"]["change"].get("key"), c["proposal"]["change"].get("account")) for c in cards
             if c["proposal"]["kind"] == "golden_mapping"}
    out = []
    for g in cluster(items):
        if g["count"] >= EVIDENCE_NEEDED or not g["chosen"] or (g["key"], g["chosen"]) in filed:
            continue
        out.append({"id": f"wait-{len(out) + 1}", "status": "waiting", "scope": "sponsor" if len(g["sponsors"]) == 1
                    else "org", "sp": g["sponsors"][0] if len(g["sponsors"]) == 1 else "",
                    "cause": f"{g['count']} override(s): {g['shown'] or 'no answer'} → {g['chosen']}",
                    "skill": g["skill"] or "", "kind": "fast loop", "ev": g["count"], "need": EVIDENCE_NEEDED})
    return out


# ---- pages and actions --------------------------------------------------------------------------------------
PAGES = ("improvements", "health", "skills", "skill")


def harness_page(manager: Any, page: str, *, name: str = "", sponsor: str = "", admin: bool = False,
                 env: Mapping[str, str] | None = None) -> dict[str, Any]:
    if page == "improvements":
        return improvements_view(manager, sponsor, admin)
    if page == "health":
        return health_view(manager, env)
    if page == "skills":
        return skills_view(manager)
    if page == "skill":
        return skill_view(manager, name)
    raise HarnessError(f"no harness page {page!r}; pages: {list(PAGES)}", 404)


def _service(call: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """A run manager call whose refusal (`ServiceError`) the harness pages answer with the same status."""
    from worker.service import ServiceError

    try:
        return call(*args, **kwargs)  # type: ignore[no-any-return]
    except ServiceError as exc:
        raise HarnessError(str(exc), exc.status) from exc


def _decide(manager: Any, card_id: str, verdict: str, reason: str, who: str, admin: bool) -> dict[str, Any]:
    _replay, store, _golden = manager._improvement_parts()
    try:
        card = store.load(card_id)
    except (OSError, ValueError) as exc:
        raise HarnessError("no such improvement", 404) from exc
    if card["proposal"]["kind"] != "table_row":
        return _service(manager.decide_improvement, card_id, verdict, reason, who, admin)
    ev = card["proposal"].get("evidence") or {}
    try:
        if verdict == "reject":
            if not reason.strip():
                raise HarnessError("a reason is required")
            return store.reject(card_id, who, reason)  # audited before the card changes
        if verdict != "approve":
            raise HarnessError("verdict must be approve or reject")
        overlay = _overlay(manager, ev.get("skill", "map-accounts"), ev.get("table", "coa-cascade"))
        if card["status"] == "awaiting_controller":  # narrows a one-way table: a controller's decision
            return _service(manager.decide_improvement, card_id, verdict, reason, who, admin, versions=overlay)
        return store.promote(card_id, who, overlay, admin=admin, reason=reason)
    except HarnessError:
        raise
    except PermissionError as exc:
        raise HarnessError(str(exc), 403) from exc
    except ValueError as exc:
        raise HarnessError(str(exc), 409) from exc


def _rollback(manager: Any, surface: str, who: str, reason: str) -> dict[str, Any]:
    if surface == "control":  # the latest control change, by any controller
        return _service(manager.rollback_control, who, reason)
    if surface != "table_row":
        return _service(manager.rollback_improvement, who, reason)
    _replay, store, _golden = manager._improvement_parts()
    last = max((c for c in store.cards("promoted") if c["proposal"]["kind"] == "table_row"),
               key=lambda c: str(c.get("promoted") or ""), default=None)
    ev = (last["proposal"].get("evidence") or {}) if last else {}
    try:
        return store.rollback(_overlay(manager, ev.get("skill") or "map-accounts", ev.get("table") or "coa-cascade"),
                              who, reason, kinds=("table_row",))
    except PermissionError as exc:
        raise HarnessError(str(exc), 403) from exc
    except ValueError as exc:
        raise HarnessError(str(exc), 409) from exc


def harness_act(manager: Any, body: Mapping[str, Any], who: str, admin: bool) -> dict[str, Any]:
    """One human action from the harness pages. `who` is the named reviewer; the agent has no path to this."""
    action = str(body.get("action", ""))
    stamp = time.strftime("%Y-%m-%d %H:%M")
    if action == "test":
        return test_table(manager, str(body.get("skill", "")), str(body.get("table", "")), body.get("rows") or [])
    if action == "propose":
        return propose_table(manager, str(body.get("skill", "")), str(body.get("table", "")), body.get("rows") or [],
                             str(body.get("scope", "sponsor")), str(body.get("sponsor", "")), who)
    if action == "propose_control":  # a person proposes; another controller approves it later
        return _service(manager.propose_control, body.get("controls") or {}, who, str(body.get("reason") or ""))
    if action == "promote":
        reason = str(body.get("reason") or f"approved by {who} in the console, {stamp}")
        return _decide(manager, str(body.get("card", "")), "approve", reason, who, admin)
    if action == "reject":
        reason = str(body.get("reason") or f"rejected by {who} in the console, {stamp}")
        return _decide(manager, str(body.get("card", "")), "reject", reason, who, admin)
    if action == "rollback":
        reason = str(body.get("reason") or f"rolled back by {who} in the console, {stamp}")
        return _rollback(manager, str(body.get("surface", "golden_mapping")), who, reason)
    raise HarnessError(f"unknown harness action {action!r}", 400)


__all__ = ["HarnessError", "harness_act", "harness_page", "health_view", "improvements_view", "propose_table",
           "skill_view", "skills_view", "test_table"]
