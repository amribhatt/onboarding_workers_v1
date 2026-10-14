"""View models of the job screen's "Show working" tabs: fields, decisions, every row, crosswalk, skills, context,
runs and audit.

Every figure is counted from the job's own state (its mapping, rules, runs, decisions), the console's records and
audit, and the installed skills. A tab the state cannot fill says so (`empty`) rather than showing a number.
Nothing here writes; the tabs are read-only evidence next to the chat.
"""

from __future__ import annotations

from typing import Any

from worker import console_demo as D
from worker import console_engine as CE
from worker import engine_console as EC
from worker import record_facts as RF
from worker.errors import StatusError

MAX_ROWS = 300  # rows sent per tab; `total` always carries the full count

_BASE = [("fields", "Fields"), ("dm", "Decisions"), ("results", "Every row")]
_TAIL = [("skills", "Skills"), ("graph", "Context"), ("runs", "Runs & trace"), ("ask", "Ask the job"),
         ("audit", "Audit")]


class WorkingError(StatusError):
    pass


def tabs_for(record_type: str) -> list[tuple[str, str]]:
    gl = RF.is_gl(record_type)
    base = [("fields", "Mapping" if gl else "Fields"), *_BASE[1:]]
    return [*base, *([("xwalk", "Crosswalks")] if gl else []), *_TAIL]


def find_job(c: Any, job_id: str) -> dict[str, Any]:
    j = next((x for x in c.S["jobs"] if x.get("id") == job_id and "up" in x), None)
    if j is None:
        raise WorkingError(f"no job {job_id!r}", 404)
    return j


def working_view(c: Any, j: dict[str, Any], tab: str) -> dict[str, Any]:
    if tab not in dict(tabs_for(j["type"])):
        raise WorkingError(f"no tab {tab!r} for {j['type']}", 404)
    from worker.console_ask import ask_view

    return {"fields": _fields, "dm": _dm, "results": _results, "xwalk": _xwalk, "skills": _skills,
            "graph": _graph, "runs": _runs, "ask": ask_view, "audit": _audit}[tab](c, j)


def _money(n: float) -> str:
    return f"{n:,.2f}"


# ---- fields / mapping -------------------------------------------------------------------------------------
def _field_kind(src: str) -> str:
    if not src:
        return "missing"
    if src.startswith("constant"):
        return "constant"
    if src == "generated":
        return "generated"
    if src.startswith("("):
        return "code"
    return "column"


def _fields(c: Any, j: dict[str, Any]) -> dict[str, Any]:
    if RF.is_gl(j["type"]):
        return _mapping(c, j)
    rows = []
    for f in CE.fields_of(j):
        name, src, _default, conf, by_you = f
        kind = _field_kind(src)
        required = c.req_of(j["type"], f, j["sp"])
        if kind == "missing":
            status = "required_missing" if required else "missing"
        elif by_you:
            status = "set"
        elif kind in ("constant", "code"):
            status = "sponsor"
        elif j["okF"].get(name) or (required and conf >= 95):
            status = "approved"
        else:
            status = "pending"
        rows.append({"field": name, "required": required, "source": src[10:] if kind == "constant" else src,
                     "kind": kind, "confidence": conf if kind == "column" or by_you else None, "status": status})
    return {"kind": "fields", "title": "Matching fields",
            "sub": "Each target field and the source column it comes from. A field the file cannot give stays blank "
                   "and is reported, never invented.", "rows": rows,
            "note": "Fields below 95% confidence need your approval."}


def _mapping(c: Any, j: dict[str, Any]) -> dict[str, Any]:
    lines = [x["l"] for x in CE.gl_eval(c, j)["lines"]]
    first: dict[str, Any] = {}
    for ln in lines:
        first.setdefault(ln.acct, ln)
    golden = {a: g for a, g, _f in D.ACCTS}
    accounts = []
    for acct, ln in first.items():
        mine = j["acct"].get(acct)
        hist = c.known_account(j["sp"], acct, ln.tt)
        if mine:
            row = {"ledgerline": f"mapped to {mine}", "tier": "manual", "status": "set"}
        elif hist:
            row = {"ledgerline": next((g for a, g, _f in D.ACCTS if g.startswith(hist)), hist), "tier": "1",
                   "status": "matched"}
        elif acct in golden:
            row = {"ledgerline": "", "tier": "3", "status": "needs"}
        else:
            row = {"ledgerline": "", "tier": "3", "status": "no_match"}
        accounts.append({"account": acct, "name": ln.an, **row, "suggest": D.SUGA.get(acct, "")})
    tts = list(dict.fromkeys(ln.tt for ln in lines))
    return {"kind": "mapping", "title": "Mapping",
            "sub": "Source accounts are matched to the Golden chart of accounts in tiers: 1 matched before, 3 unmatched "
                   "for you. Transaction types are checked against the standard mapping.",
            "stats": {"accounts": len(accounts), "matched": sum(1 for a in accounts if a["status"] in ("matched", "set")),
                      "unmatched": sum(1 for a in accounts if a["status"] == "needs"),
                      "types_mapped": sum(1 for t in tts if t in D.TXN), "types": len(tts)},
            "accounts": accounts}


# ---- decisions --------------------------------------------------------------------------------------------
def _rules_in_force(j: dict[str, Any]) -> list[dict[str, str]]:
    r = j["rules"]
    out = [{"rule": {"error": "An ID over the limit is flagged for you, never cut",
                     "truncate": "An ID over the limit is cut",
                     "drop": "A long ID prefix is dropped"}.get(r["id"], str(r["id"])), "source": "ID length"},
           {"rule": f"Names are cut at {r['nameMax']} characters", "source": "Name length"}]
    if r["suffix"]:
        out.append({"rule": "Ltd and Limited count as the same name", "source": "Duplicate detection"})
    if RF.is_gl(j["type"]):
        out.append({"rule": (f"A journal may be off by up to {_money(r['tolPass'])} and passes; up to "
                             f"{_money(r['tolWarn'])} is a WARN; more is an ERR"), "source": "Journal balance"})
    out += [{"rule": x["text"], "source": f"rule you changed ({x['scope']})"} for x in j.get("logR", [])]
    return out


def _dm(c: Any, j: dict[str, Any]) -> dict[str, Any]:
    from worker.console_harness import _table_view
    from worker.skills import load_skills

    tables: list[dict[str, Any]] = []
    if RF.is_gl(j["type"]):
        for name, sk in load_skills().items():
            if name not in ("map-accounts", "reconcile-gl"):
                continue
            d = sk.path / "tables"
            for p in sorted(d.glob("*.yaml")) if d.is_dir() else []:
                t = _table_view(sk, p.stem)
                tables.append({"skill": name, "id": t["id"], "description": t["description"], "hit": t["hit"],
                               "inputs": t["inputs"], "outputs": t["outputs"], "rows": t["rows"]})
    out: dict[str, Any] = {"kind": "dm", "title": "Decisions",
                           "sub": "Classification and checks are decision tables, read top to bottom. Code evaluates "
                                  "them, so the same input always gets the same answer.",
                           "tables": tables, "rules": _rules_in_force(j)}
    if not tables:
        out["empty"] = (f"No decision table backs {j['type']} yet; its rules run in code. The rules in force for "
                        "this job are listed below.")
    return out


# ---- every row --------------------------------------------------------------------------------------------
def _results(c: Any, j: dict[str, Any]) -> dict[str, Any]:
    gl = RF.is_gl(j["type"])
    out: dict[str, Any] = {"kind": "results", "title": "Every row" if not gl else "Every line", "rows": [],
                           "total": 0, "gl": gl}
    if not j["runs"]:
        out["empty"] = "No run yet. Run the process to see what was written, what was left out and why."
        return out
    last = j["runs"][-1]
    t = CE.tally(c, j)
    out["run"] = {"n": last["n"], "time": last.get("time", "")}
    if gl:
        e = CE.gl_eval(c, j)
        out["stats"] = {"read": t["read"], "journals": len(e["js"]), "err": t["err"], "warn": t["warn"],
                        "out": t["out"], "skip": t["skip"], "offsets": len(e["offsets"])}
        out["journals"] = [{"journal": x["j"], "lines": x["n"], "debit": _money(x["dr"]), "credit": _money(x["cr"]),
                            "diff": _money(x["diff"]), "st": x["st"]} for x in e["js"]]
        out["total"] = len(e["lines"])
        for x in e["lines"][:MAX_ROWS]:
            ln = x["l"]
            out["rows"].append({
                "st": x["st"], "journal": ln.j, "line": ln.ln, "account": f"{ln.acct} {ln.an}", "type": ln.tt,
                "debit": _money(ln.dr) if ln.dr else "", "credit": _money(ln.cr) if ln.cr else "",
                "ref": ", ".join(v for _k, v in EC.gl_refs(ln)),
                "why": "; ".join([m["m"] for m in x["errs"]] + x["msgs"]) or "Resolved, written as is"})
        return out
    rows = CE.static_rows(c, j)
    ids = CE.out_ids(c, j)[0]
    out["stats"] = {"read": t["read"], "already": sum(1 for r in rows if r["st"] == "skip"
                                                      and r["why"].startswith("Already")),
                    "err": t["err"], "warn": t["warn"], "out": t["out"], "skip": t["skip"]}
    out["total"] = len(rows)
    for r in rows[:MAX_ROWS]:
        out["rows"].append({"i": r["i"], "st": r["st"], "id": r["id"], "new_id": ids.get(r["i"], ""),
                            "name": r["nm"], "x": r["x"] or "", "why": r["why"], "hit": r.get("hit", "")})
    return out


# ---- crosswalks -------------------------------------------------------------------------------------------
def _xwalk(c: Any, j: dict[str, Any]) -> dict[str, Any]:
    e = CE.gl_eval(c, j)
    refs: dict[str, list[str]] = {k: [] for k in ("Investors", "Vendors", "Affiliates", "Investments")}
    for x in e["lines"]:
        for kind, v in EC.gl_refs(x["l"]):
            if v not in refs[kind]:
                refs[kind].append(v)
    rows = []
    for kind, vals in refs.items():
        bad = [v for v in vals if not EC.known_ref(CE.ConsoleWorld(c, j["sp"]), kind, v) and f"{kind}{v}" not in j["qa"]]
        rows.append({"ref": kind, "distinct": len(vals), "resolved": len(vals) - len(bad), "unresolved": bad})
    accts = list(dict.fromkeys(x["l"].acct for x in e["lines"]))
    flagged = {a for a, _g, f in D.ACCTS if f}
    bad_a = [a for a in accts if a in {z[0] for z in D.ACCTS} and a not in flagged and a not in j["acct"]
             and not any(c.known_account(j["sp"], a, x["l"].tt) for x in e["lines"] if x["l"].acct == a)]
    rows.append({"ref": "Accounts (COA)", "distinct": len(accts), "resolved": len(accts) - len(bad_a),
                 "unresolved": bad_a})
    return {"kind": "xwalk", "title": "Crosswalks",
            "sub": "Every reference on every line is looked up in the records you onboarded, so the ledger points at "
                   "the new IDs. An unknown reference blocks.", "rows": rows}


# ---- skills -----------------------------------------------------------------------------------------------
def _recon_evidence(c: Any, j: dict[str, Any]) -> str:
    """Which reconcile-gl version produced this job's figures: the identity stored with each client report the job
    holds, or the rules in force now when no report was supplied yet."""
    from worker import recon as R

    ft = "DLP" if j["type"] == "GL LP" else "DE"
    applies = sum(1 for s in R.SPECS if ft in s["scope"])
    seen: dict[str, list[str]] = {}
    for spec in R.SPECS:
        saved = R.load_report(c.job_dir(j), spec["id"])
        if saved:
            ident = f"{saved.get('skill', 'unknown version')}#{saved.get('skill_hash', '')}".rstrip("#")
            seen.setdefault(ident, []).append(spec["id"])
    head = f"{applies} of 11 checks apply to {j['type']}"
    if not seen:
        return f"{head}; no client report supplied yet, rules in force {R.default_rules().ident}"
    return head + "; client reports checked under " + "; ".join(
        f"{ident} ({', '.join(ids)})" for ident, ids in seen.items())


def _skills(c: Any, j: dict[str, Any]) -> dict[str, Any]:
    from worker.skills import load_skills

    gl = RF.is_gl(j["type"])
    ran = bool(j["runs"])
    t = CE.tally(c, j) if ran else None
    resolved = 0
    n_acct = 0
    if gl:
        lines = [x["l"] for x in CE.gl_eval(c, j)["lines"]]
        first: dict[str, Any] = {}
        for ln in lines:
            first.setdefault(ln.acct, ln)
        n_acct = len(first)
        resolved = sum(1 for a, ln in first.items() if j["acct"].get(a) or c.known_account(j["sp"], a, ln.tt))
    rows = []
    for name, sk in load_skills().items():
        d = sk.path / "tables"
        used, evidence = False, ""
        if name == "map-accounts" and gl:
            used = True
            evidence = f"{resolved} of {n_acct} accounts resolved from history or your choice"
        elif name == "reconcile-gl" and gl and ran:
            used = True
            evidence = _recon_evidence(c, j)
        elif name == "check-data-quality" and ran and t is not None:
            used = True
            evidence = (f"its rule families ran in Check, run {len(j['runs'])}: {t['err']} to resolve, "
                        f"{t['warn']} to check, {t['skip']} left out")
        rows.append({"name": name, "version": sk.version, "description": sk.description,
                     "tables": sorted(p.stem for p in d.glob("*.yaml")) if d.is_dir() else [],
                     "used": used, "evidence": evidence})
    out: dict[str, Any] = {"kind": "skills", "title": "Skills",
                           "sub": "Each skill is a folder with a short description, know-how for the model, scripts "
                                  "that code runs and decision tables that code evaluates.", "skills": rows}
    if not rows:
        out["empty"] = "No skills are installed."
    return out


# ---- context ----------------------------------------------------------------------------------------------
def _graph(c: Any, j: dict[str, Any]) -> dict[str, Any]:
    from worker.skills import load_skills

    sp = j["sp"]
    rec = next((s for s in c.S["sponsors"] if s["n"] == sp), {})
    funds = [{"name": f, "static": c.static_done(f, sp), "gl": sum(c.S["F"][f]["gl"].values())}
             for f in c.ents_of(sp)]
    reg = c.reg_of(sp)
    wf = [k.split("|")[1] for k, v in c.S["wf"].items() if v and k.startswith(sp + "|")]
    tables = 0
    for sk in load_skills().values():
        d = sk.path / "tables"
        tables += len(list(d.glob("*.yaml"))) if d.is_dir() else 0
    book = c.S.get("book", {}).get(sp, [])
    facts = [
        {"label": "Workflows", "value": ", ".join(wf) or "none approved yet"},
        {"label": "Records in Ledgerline", "value": " · ".join(f"{len(reg.get(t, []))} {t.lower()}" for t in RF.stored_types())},
        {"label": "ID counters", "value": " · ".join(f"{t} next {RF.id_fmt(t, v)}" for t, v in c.counters_of(sp).items()) or "not started"},
        {"label": "Semantic layer", "value": f"{len(EC.header_words())} concepts the column detector knows"},
        {"label": "Decision models", "value": f"{tables} table{'s' if tables != 1 else ''} in installed skills"
         + (f", {len(j['logR'])} rule change(s) by you" if j.get("logR") else "")},
        {"label": "Rulebook", "value": f"{len(book)} rule{'s' if len(book) != 1 else ''} taught for {sp}"},
    ]
    return {"kind": "graph", "title": "Context",
            "sub": f"What I know about {sp}. Every job reads from it, and only approvals write to it.",
            "sponsor": sp, "sponsor_id": rec.get("id", ""), "funds": funds, "facts": facts}


# ---- runs and trace ---------------------------------------------------------------------------------------
def _runs(c: Any, j: dict[str, Any]) -> dict[str, Any]:
    from worker import console_view as V

    runs = []
    for i, r in enumerate(j["runs"]):
        p = j["runs"][i - 1]["t"] if i else None
        t = r["t"]
        runs.append({"n": r["n"], "time": r.get("time", ""), "rules": r["rules"], "err": t["err"], "warn": t["warn"],
                     "out": t["out"], "d_err": t["err"] - p["err"] if p else None,
                     "d_warn": t["warn"] - p["warn"] if p else None, "d_out": t["out"] - p["out"] if p else None})
    return {"kind": "runs", "title": "Runs & trace", "sub": "Each run keeps the rules it used, so a change and its "
            "effect sit side by side.", "runs": runs, "stages": V.timeline(c, j), "retries": j.get("retries", 0),
            "failed": j.get("failed"),
            "note": "Model calls, tokens and stage timings are not recorded for console jobs, so none are shown."}


# ---- audit ------------------------------------------------------------------------------------------------
def _audit(c: Any, j: dict[str, Any]) -> dict[str, Any]:
    rows = [{"t": a.get("t", ""), "d": a.get("d", ""), "who": a.get("who", ""), "a": a.get("a", ""),
             "reason": a.get("r", "")}
            for a in c.S["audit"] if a.get("job", "") in ("", j["type"]) and a.get("sp", j["sp"]) in ("", j["sp"])]
    out: dict[str, Any] = {"kind": "audit", "title": "Audit",
                           "sub": "Every instruction, run and decision with its reason.", "rows": rows}
    if not rows:
        out["empty"] = "Nothing recorded for this job yet."
    return out
