"""View models and actions of the job screen's learning parts: reconcile, teach-back, feedback and the rulebook.

Real state only. The reconcile figures are computed from the job's own lines (the same engine the tally and the
written file use); the guide's checks that need the client's own report are listed as "needs the report" and never
given a number. Teach-back reads the job's learned items; confirming one writes a feedback record (hash-chained, in the
job's audit and the sponsor's log) and, for "always for this sponsor", a rule into the sponsor's memory. The rulebook
is that memory. A part with nothing to show says so.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from worker import console_engine as CE
from worker import feedback as F
from worker import persist
from worker import recon as R
from worker import record_facts as RF
from worker import templates as T
from worker.audit import AuditError
from worker.errors import StatusError
from worker.money import balance_status, to_decimal, to_float, total

PARTS = ("recon", "teach", "fb", "book")
FUND_TYPES = [("DE", "Dual-Entity Level"), ("DLP", "Dual-LP Level"), ("EL", "Entity Level only"), ("LP", "LP Level only")]
SCOPES = ("job", "sponsor", "org")
SCOPE_WORDS = {"job": "Just this file", "sponsor": "Always for {sp}", "org": "Every sponsor"}
_FEEDBACK_SCOPE = {"job": "job", "sponsor": "sponsor", "org": "organisation"}
ORDER = {"ERROR": 4, "WARN": 3, "REVIEW": 2, "PASS": 1}
MAX_JOURNAL_ROWS = 12


class LearningError(StatusError):
    """A request the learning parts cannot honour; `status` is the HTTP status to answer with."""


# The eleven guide checks are declared in `worker.recon` (one engine, eleven specs); the same specs write the
# "Rows it selects" and "How it runs" text shown here.
_ALL = ["DE", "DLP", "EL", "LP"]
_LPS = ["DLP", "LP"]


# ---- helpers ----------------------------------------------------------------------------------------------
def _money(v: float) -> str:
    return f"{v:,.2f}"


def _status(a: float, b: float, kind: str, tol: tuple[float, float] | None, rules: R.Rules) -> tuple[float, str]:
    """Difference and flag. The one tolerance decision is `money.balance_status`; the thresholds of a row with no
    tolerance of its own come from the skill's controls (the flat style), the words from its levels."""
    low, high = (float(x) for x in rules.thresholds("flat", kind)) if tol is None else tol
    state, diff = balance_status(a, b, low, high)
    word = {"ok": "PASS", "warn": rules.level("within_warn_band"), "err": rules.level("over_warn_limit")}[state]
    return to_float(diff), word


def _row(label: str, a: float, b: float, rules: R.Rules, kind: str = "money",
         tol: tuple[float, float] | None = None, status: str | None = None) -> dict[str, Any]:
    d, s = _status(a, b, kind, tol, rules)
    return {"label": label, "a": a, "b": b, "diff": d, "kind": kind, "status": status or s}


def _worst(rows: list[dict[str, Any]]) -> str:
    w = "PASS"
    for r in rows:
        if ORDER.get(r["status"], 0) > ORDER[w]:
            w = r["status"]
    return w


def _check(c: dict[str, Any], state: str, rows: list[dict[str, Any]] | None = None, cols: list[str] | None = None,
           note: str = "", **more: Any) -> dict[str, Any]:
    rows = rows or []
    out = {"id": c["id"], "n": c["n"], "per": c["per"], "scope": c["scope"], "rule": c["rule"], "sel": c["sel"],
           "steps": c["steps"], "state": state, "rows": rows, "cols": cols or ["Item", "Upload", "Source"],
           "note": note, "worst": (_worst(rows) if state == "run" else R.worst(rows)),
           "bad": sum(1 for r in rows if r["status"] not in ("PASS", ""))}
    out.update(more)
    return out


def _job(c: Any, job_id: str) -> dict[str, Any]:
    j = next((x for x in c.S["jobs"] if x.get("id") == job_id), None)
    if j is None or "runs" not in j:
        raise LearningError("That job is not open in this app.", 404)
    return j


def _net(ln: Any) -> float:
    return to_float(to_decimal(ln.dr) - to_decimal(ln.cr))


def _other_run(c: Any, j: dict[str, Any]) -> dict[str, Any] | None:
    """The same entity's other GL run (a fund run for an LP run and the other way round), if it has been run."""
    want = "GL Fund" if j["type"] == "GL LP" else "GL LP"
    cand = [x for x in c.S["jobs"] if x.get("sp") == j["sp"] and x.get("fund") == j["fund"] and x.get("type") == want
            and x.get("runs") and not x.get("failed")]
    cand.sort(key=lambda x: (bool(x.get("approved")), str(x.get("id"))))
    return cand[-1] if cand else None


# ---- the upload's rows, as the eleven checks see them --------------------------------------------------------
def _coa() -> dict[str, dict[str, str]]:
    from worker.accounts import Coa

    path = T.standards_dir() / "Golden_COA.csv"
    return {k: dict(v) for k, v in Coa.load(path).accounts.items()} if path.is_file() else {}


def _job_rows(c: Any, j: dict[str, Any], e: dict[str, Any], coa: Mapping[str, Mapping[str, str]]
              ) -> tuple[list[R.Row], dict[str, str], int, int]:
    """Written lines then their offsets (the ones the file will carry), each with its Ledgerline account, FS
    grouping and FS line item from the Golden chart. Returns (rows, investor ref -> name, lines left out for want
    of an account, lines still needing the person)."""
    mk = CE.gl_mapper(c, j)
    names: dict[str, str] = {}
    rows: list[R.Row] = []
    unmapped = pending = 0
    for x in list(e["lines"]) + list(e["offsets"]):
        ln = x["l"]
        off = bool(x.get("off"))
        if x["st"] == "skip":
            continue
        if x["st"] == "err":
            pending += 0 if off else 1
            continue
        acct = mk.account(ln.acct, ln.tt)
        if not acct or acct not in coa:
            unmapped += 1
            continue
        if ln.inv and ln.inv not in names:
            names[ln.inv] = mk.investor(ln.inv)[1]
        ac = coa[acct]
        rows.append(R.Row(acct, ac.get("FS_Grouping", ""), ac.get("FS_Line_Item", ""), _net(ln),
                          R.parse_date(ln.date), "" if off else ln.inv, ln.tt, ln.invst, R.parse_date(ln.pr),
                          ln.shares, R._yes(ln.agn), off))
    return rows, names, unmapped, pending


def _acct_resolver(j: dict[str, Any], w: Any, coa: Mapping[str, Any]) -> Any:
    def resolve(line: str, tt: str) -> str:
        for t in dict.fromkeys([line.strip(), line.strip().split(" ")[0]]):
            picked = j["acct"].get(t)
            if picked:
                return str(picked).partition(" ")[0]
            if t in coa:
                return t
            known = w.known_account(t, tt)
            if known:
                return known
        return ""
    return resolve


def _aliases(w: Any, rows: list[R.Row]) -> dict[str, str]:
    """Every way the client's report may name an investor (ID, name, source key) -> the reference the rows carry."""
    m: dict[str, str] = {}
    store = w.store("Investors")
    for ref in {r.inv for r in rows if r.inv}:
        m[ref.casefold()] = ref
        hit = next((x for x in store if ref in (x.get("id"), x.get("key"), x.get("name"), x.get("source_key"))), None)
        for k in ("id", "key", "name", "source_key"):
            if hit and hit.get(k):
                m.setdefault(str(hit[k]).casefold(), ref)
    return m


def _rules(sponsor: str, root: Path | None) -> R.Rules:
    """The reconcile-gl rules for this sponsor: the skill, promoted rows, and the sponsor's folder (its
    `skills/reconcile-gl/` overlays)."""
    sponsor_root: Path | None = None
    slug = ""
    try:
        slug = T.sponsor_slug(sponsor)
        sponsor_root = T.sponsors_dir() / slug
    except ValueError:
        pass
    return R.rules_for(sponsor_root if sponsor_root is not None and sponsor_root.is_dir() else None, root, slug)


def _guide_checks(c: Any, j: dict[str, Any], e: dict[str, Any], w: Any, rules: R.Rules) -> list[dict[str, Any]]:
    coa = _coa()
    rows, names, unmapped, pending = _job_rows(c, j, e, coa)
    other = _other_run(c, j)
    o_rows: list[R.Row] | None = None
    o_names: dict[str, str] = {}
    if other is not None:
        o_rows, o_names, _u, _p = _job_rows(c, other, CE.gl_eval(c, other), coa)
    ctx = R.make_ctx(rows, {**o_names, **names})
    aliases = _aliases(w, rows)
    fund_rows = rows if j["type"] == "GL Fund" else o_rows
    lp_rows = rows if j["type"] == "GL LP" else o_rows
    resolver = _acct_resolver(j, w, coa)
    jdir = c.job_dir(j)
    left = []
    if pending:
        left.append(f"{pending} line{'s' if pending > 1 else ''} still need you and are not in these figures.")
    if unmapped:
        left.append(f"{unmapped} line{'s' if unmapped > 1 else ''} have no Ledgerline account yet and are not in "
                    "these figures.")
    out: list[dict[str, Any]] = []
    for spec in rules.specs:
        if spec.get("pair"):
            if other is None:
                want = "GL Fund" if j["type"] == "GL LP" else "GL LP"
                out.append(_check(spec, "waiting", cols=spec["cols"], note=f"Waiting for the {want} run of "
                                  f"{j['fund']}. It has not been run in this app yet."))
                continue
            res = R.run_check(spec, rows, ctx, coa, None, resolver, aliases, fund_rows, lp_rows)
            note = (f"Compared with the {other['type']} job {other['id']}"
                    f"{' (approved)' if other.get('approved') else ' (not yet approved)'}.")
            out.append(_check(spec, "run", res["rows"], spec["cols"], note + (" " + " ".join(left) if left else ""),
                              skill=rules.ident))
            continue
        saved = R.load_report(jdir, spec["id"])
        res = R.run_check(spec, rows, ctx, coa, saved["rows"] if saved else None, resolver, aliases, fund_rows,
                          lp_rows, incomplete=bool(pending or unmapped))
        notes = list(res["notes"])
        if res["state"] == "needs-report":
            notes.insert(0, f"Needs {spec['needs']}. Upload it in the layout below; until then the Client column is "
                            "blank and nothing is compared.")
        if res["state"] != "gap":
            if not res["rows"]:
                notes.append("No line of this upload falls in the rows this check selects.")
            notes.extend(left)
            if spec.get("period") is True and ctx.fy_start is not None:
                notes.append(f"Current fiscal year taken as {ctx.fy_start.year} (the calendar year of the latest GL "
                             "date)." + (f" {ctx.undated} line(s) have no readable GL date and count only in "
                                         "inception-to-date." if ctx.undated else ""))
        out.append(_check(spec, res["state"], res["rows"], spec["cols"], " ".join(notes),
                          layout=R.layout_info(spec), needs=spec["needs"], skill=rules.ident,
                          report={k: saved[k] for k in ("file", "at", "by")} if saved else None))
    return out


# ---- reconcile ----------------------------------------------------------------------------------------------
def recon_view(c: Any, j: dict[str, Any], root: Path | None = None,
               cap: int | None = MAX_JOURNAL_ROWS) -> dict[str, Any]:
    """`cap` is how many journals the card lists; None lists every one (the Reconcile table pages them)."""
    gl = RF.is_gl(j["type"])
    out: dict[str, Any] = {"job": j["id"], "type": j["type"], "gl": gl, "fund_types": [], "default_ft": "",
                           "checks": [], "by_ft": {}, "sponsor": j["sp"], "fund": j["fund"]}
    if not j["runs"]:
        out["empty"] = "Nothing to reconcile yet. The file has not been converted."
        return out
    w = c.world(j["sp"])
    t = CE.tally(c, j)
    rules = _rules(j["sp"], root)
    out["skill"] = {"stamp": rules.stamp, "hash": rules.digest, "ident": rules.ident}
    base = {"per": "ITD", "scope": _ALL}
    if not gl:
        written = t["out"] + t["skip"]
        row = _row("Rows read = written + left out", t["read"], written, rules, "int",
                   status=None if t["err"] == 0 else "REVIEW")
        row["note"] = f"{t['err']} still need you" if t["err"] else ""
        out["checks"] = [_check({"id": "tie", "n": "Rows in = rows out", "rule": "Every row read is written or left "
                                 "out with a reason. Anything still needing you is shown, never counted as written.",
                                 "sel": "All rows of the source sheet.", "steps": [
                                     "Count the rows read from the source",
                                     "Count the rows written, and the rows left out with a reason",
                                     "The two must agree; rows waiting on you are listed separately"], **base},
                                "run", [row], ["Count", "Read", "Written + left out"],
                                f"{t['out']} written · {t['skip']} left out · {t['err']} need you")]
        out["by_ft"] = {"": _tile(out["checks"], "")}
        out["note"] = "The eleven GL reconciliation checks apply to GL uploads."
        if j.get("approved") and j.get("out"):
            out["written"] = (j["out"] or {}).get("tie_out") if isinstance(j["out"], Mapping) else None
        return out

    e = CE.gl_eval(c, j)
    r = j["rules"]
    tol = (float(r["tolPass"]), float(r["tolWarn"]))
    lines = e["lines"]
    off_ok = sum(1 for o in e["offsets"] if o["st"] == "ok")
    checks: list[dict[str, Any]] = []
    tie_rows = [_row("Lines read = written + left out", t["read"], t["out"] - off_ok + t["skip"], rules, "int",
                     status=None if t["err"] == 0 else "REVIEW")]
    checks.append(_check({"id": "tie", "n": "Lines in = lines out", "rule": "Every line read is written or left out "
                          "with a reason. Offsets the code adds are counted apart.", "sel": "All lines of the source GL.",
                          "steps": ["Count the lines read", "Count the lines written and left out",
                                    "Offsets added by code are not source lines"], **base},
                         "run", tie_rows, ["Count", "Read", "Written + left out"],
                         f"{t['out']} written{' incl. ' + str(off_ok) + ' offsets' if off_ok else ''} · "
                         f"{t['skip']} left out · {t['err']} need you"))
    inc = [x for x in e["js"] if not str(j["dec"].get(x["j"], "")).startswith("exclude")]
    ordered = sorted(inc, key=lambda x: (-abs(x["diff"]), x["j"]))
    shown = [x for x in ordered if x["st"] != "ok"] + [x for x in ordered if x["st"] == "ok"]
    jr = [_row(f"Journal {x['j']}", round(x["dr"], 2), round(x["cr"], 2), rules, "money", tol) for x in shown[:cap]]
    dr, cr = float(total(x["dr"] for x in inc)), float(total(x["cr"] for x in inc))
    jr.append(_row(f"All {len(inc)} journals", dr, cr, rules, "money", tol))
    more = len(shown) - cap if cap is not None else 0
    checks.append(_check({"id": "bal", "n": "Journals balance", "per": "ITD", "scope": _ALL,
                          "rule": (f"A journal may be off by up to {_money(tol[0])} (PASS); up to {_money(tol[1])} is "
                                   f"a WARN; more is an ERROR.") if tol[0] else
                          f"Every journal nets to zero; up to {_money(tol[1])} is a WARN; more is an ERROR.",
                          "sel": "Every journal not excluded by you.",
                          "steps": ["Sum debits and credits per journal (offsets included for the LP run)",
                                    "Difference = debits - credits", "Flag by the tolerance of this job"]},
                         "run", jr, ["Journal", "Debits", "Credits"],
                         f"{more} more balanced journals not listed" if more > 0 else ""))
    if j["type"] == "GL LP":
        inv = [x for x in lines if x["l"].inv and x["st"] != "skip"]
        lp_net = round(sum(_net(x["l"]) for x in inv), 2)
        off_net = round(sum(_net(o["l"]) for o in e["offsets"] if o["st"] == "ok"), 2)
        checks.append(_check({"id": "off", "n": "LP offsets reverse each LP line", "per": "ITD", "scope": _LPS,
                              "rule": "One offset per LP line, sign reversed: LP total + offset total = 0.",
                              "sel": "Lines with an investor, and the offset lines the code adds.",
                              "steps": ["Count the investor lines and the offsets", "Sum both; they must cancel"]},
                             "run", [_row("Investor lines vs offsets", len(inv), off_ok, rules, "int"),
                                     _row("LP total vs minus the offset total", lp_net, -off_net, rules)],
                             ["Test", "LP lines", "Offsets"]))
    checks.extend(_guide_checks(c, j, e, w, rules))
    out["checks"] = checks
    out["fund_types"] = [{"id": k, "label": v} for k, v in FUND_TYPES]
    out["default_ft"] = "DLP" if j["type"] == "GL LP" else "DE"
    out["by_ft"] = {k: _tile(checks, k) for k, _ in FUND_TYPES}
    out["cross"] = {k: R.combined_breaks(checks, k, rules) for k, _ in FUND_TYPES}
    out["tolerance"] = {"pass": tol[0], "warn": tol[1]}
    if j.get("approved") and isinstance(j.get("out"), Mapping):
        out["written"] = j["out"].get("tie_out")
    return out


def _bucket(c: dict[str, Any]) -> str:
    """Where a check is counted: its worst flag when it ran (or when the upload fails its own test), else why not."""
    if c["state"] == "run" or (c["state"] == "needs-report" and c["worst"] == "ERROR"):
        return c["worst"].lower() or "pass"
    return {"needs-report": "needs_report", "gap": "gap", "waiting": "waiting"}.get(c["state"], "needs_report")


def _tile(checks: list[dict[str, Any]], ft: str) -> dict[str, int]:
    on = [c for c in checks if not ft or ft in c["scope"]]
    b = [_bucket(c) for c in on]
    return {"applies": len(on), "pass": b.count("pass"), "warn": b.count("warn"), "error": b.count("error"),
            "review": b.count("review"), "needs_report": b.count("needs_report"), "gap": b.count("gap"),
            "waiting": b.count("waiting")}



# ---- teach-back ---------------------------------------------------------------------------------------------
def teach_view(c: Any, j: dict[str, Any]) -> dict[str, Any]:
    items = []
    for i, x in enumerate(j.get("learn") or []):
        where = {"job": "this file only", "sponsor": f"in {j['sp']}'s rulebook",
                 "org": "sent to an admin for every sponsor"}.get(x.get("scope") or "", "") if x["st"] == "ok" else ""
        items.append({"i": i, "t": x["t"], "scope": x.get("scope"), "st": x["st"], "where": where})
    people = len(j["dec"]) + len(j["qa"]) + len(j["dates"]) + len(j["acct"])
    read = CE.tally(c, j)["read"] if j["runs"] else 0
    pct = round((read - min(people, read)) / read * 100) if read else None
    n = len(items)
    return {"job": j["id"], "sponsor": j["sp"], "items": items, "open": sum(1 for x in items if x["st"] == "new"),
            "title": (f"From this job I learned {n} thing{'s' if n > 1 else ''}. Confirm {'them' if n > 1 else 'it'} so "
                      f"I apply {'them' if n > 1 else 'it'} next time.") if n else "Nothing new to learn from this job.",
            "rules_pct": pct, "by_you": people,
            "scopes": [{"id": s, "label": SCOPE_WORDS[s].format(sp=j["sp"])} for s in SCOPES]}


# ---- feedback ----------------------------------------------------------------------------------------------
def _slug(sponsor: str) -> str:
    try:
        return T.sponsor_slug(sponsor)
    except ValueError as exc:
        raise LearningError(str(exc)) from exc


def feedback_view(c: Any, j: dict[str, Any], root: Path) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    store = persist.feedback(root / "feedback", _slug(j["sp"]))
    for r in store.list():
        if r.get("record_type") != j["type"]:
            continue
        items.append({"t": str(r.get("ts", ""))[11:19], "kind": f"{r['action']} · {r['kind']}",
                      "ref": r.get("shown") or r.get("job") or "", "text": r.get("reason") or "",
                      "routed": r.get("target") or "", "skill": r.get("skill") or "", "scope": r.get("scope"),
                      "job": r.get("job")})
    for x in c.S.get("fb", []):
        if x.get("sp") == j["sp"] and x.get("type") == j["type"]:
            items.append({"t": x.get("t", ""), "kind": x["kind"], "ref": x["ref"], "text": x.get("text", ""),
                          "routed": "", "skill": x.get("skill", ""), "scope": "", "job": ""})
    return {"job": j["id"], "items": items, "kinds": list(F.KINDS),
            "empty": "" if items else "Nothing yet. Use the flag on anything I chose."}


# ---- rulebook -----------------------------------------------------------------------------------------------
_EVIDENCE = re.compile(r"^(?P<by>.+?) confirmed after job (?P<job>.+?); scope (?P<scope>\w+)")


def book_view(c: Any, sponsor: str, root: Path) -> dict[str, Any]:
    mem = persist.memory(root / "memory", _slug(sponsor))
    rules = []
    for rid, f in mem.approved().items():
        m = _EVIDENCE.match(f.get("evidence", ""))
        rules.append({"id": rid, "text": f.get("text", ""), "by": m["by"] if m else "", "job": m["job"] if m else
                      f.get("evidence", ""), "status": f.get("status", ""), "when": str(f.get("approved_at", ""))[:10]})
    pending = [{"id": rid, "text": p.get("text", ""), "evidence": p.get("evidence", "")} for rid, p in
               mem.pending().items()]
    return {"sponsor": sponsor, "rules": rules, "pending": pending,
            "empty": "" if rules or pending else "Nothing yet. Rules you confirm after a job appear here."}


# ---- entry points -----------------------------------------------------------------------------------------------
def learning_view(c: Any, root: Path, part: str, job_id: str) -> dict[str, Any]:
    if part not in PARTS:
        raise LearningError("Unknown part.", 404)
    j = _job(c, job_id)
    if part == "recon":
        return recon_view(c, j, root)
    if part == "teach":
        return teach_view(c, j)
    if part == "fb":
        return feedback_view(c, j, root)
    return book_view(c, j["sp"], root)


def _record(c: Any, j: dict[str, Any], root: Path, **given: Any) -> dict[str, Any]:
    audit = persist.audit(c.job_dir(j) / "audit.jsonl")
    store = persist.feedback(root / "feedback", _slug(j["sp"]))
    try:
        return F.record(audit, store, job=j["id"], sponsor=_slug(j["sp"]), record_type=j["type"], **given)
    except (F.FeedbackError, AuditError) as exc:
        raise LearningError(str(exc)) from exc


def learning_act(c: Any, root: Path, body: Mapping[str, Any], who: str = "analyst") -> dict[str, Any]:
    """One human decision in the learning parts. Nothing changes unless its audit record is written first."""
    j = _job(c, str(body.get("job") or ""))
    action = str(body.get("action") or "")
    who = (who or "analyst").strip()[:60] or "analyst"
    if action == "flag":
        kind = str(body.get("kind") or "")
        if kind not in F.KINDS:
            raise LearningError(f"Kind must be one of {list(F.KINDS)}.")
        ref = str(body.get("ref") or "").strip()[:200]
        rec = _record(c, j, root, kind=kind, action="flag", shown=ref, reason=str(body.get("text") or ""),
                      skill=str(body.get("skill") or "") or None, scope="job")
        c.log(f"Feedback (flag) on {ref}", f"{who} · {rec['skill'] or 'job'} · routed to Improvements", j["type"])
        c.save()
        return {"ok": True, "part": "fb"}
    items = j.get("learn") or []
    try:
        i = int(body.get("i"))
        item = items[i]
    except (TypeError, ValueError, IndexError) as exc:
        raise LearningError("No such learned item.", 404) from exc
    if action == "scope":
        s = str(body.get("scope") or "")
        if s not in SCOPES:
            raise LearningError(f"Scope must be one of {list(SCOPES)}.")
        if item["st"] != "new":
            raise LearningError("This one is already decided.")
        item["scope"] = s
        c.save()
        return {"ok": True, "part": "teach"}
    if item["st"] != "new":
        raise LearningError("This one is already decided.")
    if action == "reject":
        _record(c, j, root, kind="suggestion", action="reject", shown=item["t"], chosen="not a rule",
                reason="not a rule", scope="job")
        item["st"] = "no"
        c.log(f"Not a rule: {item['t']}", f"{who} declined to teach it", j["type"])
        c.save()
        return {"ok": True, "part": "teach"}
    if action != "confirm":
        raise LearningError("Unknown action.", 404)
    scope = item.get("scope")
    if scope not in SCOPES:
        raise LearningError("Pick how far it applies first.")
    _record(c, j, root, kind="suggestion", action="accept", shown=item["t"], chosen=scope,
            reason=f"taught as: {SCOPE_WORDS[scope].format(sp=j['sp'])}", scope=_FEEDBACK_SCOPE[scope])
    rule_id = None
    if scope == "sponsor":
        mem = persist.memory(root / "memory", _slug(j["sp"]))
        rule_id = mem.propose(item["t"], f"{who} confirmed after job {j['id']} ({j['type']}, {j['fund']}); "
                                         "scope sponsor")
        mem.approve(rule_id)  # this call is the human decision: the console never calls it on its own
    item["st"] = "ok"
    c.log(f"Learned ({scope}): {item['t']}", f"{who} confirmed; "
          + {"job": "applies to this file only", "sponsor": f"added to {j['sp']}'s rulebook",
             "org": "sent to an admin for every sponsor"}[scope], j["type"])
    c.save()
    return {"ok": True, "part": "teach", "rule": rule_id}


# ---- the client's report for a check ---------------------------------------------------------------------------
def learning_report(c: Any, job_id: str, check: str, name: str, data: bytes, who: str = "analyst",
                    root: Path | None = None) -> dict[str, Any]:
    """Store the client's report for one check. The audit record is written first; if it cannot be, nothing is kept."""
    j = _job(c, job_id)
    if not RF.is_gl(j["type"]):
        raise LearningError("Client reports apply to GL uploads.", 409)
    if check not in R.SPEC or R.SPEC[check].get("pair"):
        raise LearningError("This check takes no client report.", 404)
    who = (who or "analyst").strip()[:60] or "analyst"
    try:
        meta = R.save_report(c.job_dir(j), check, name, data, who, persist.audit(c.job_dir(j) / "audit.jsonl"),
                             _rules(j["sp"], root))
    except (R.ReportError, AuditError) as exc:
        raise LearningError(str(exc)) from exc
    c.log(f"Client report for {R.SPEC[check]['n']}", f"{who} supplied {meta['file']} ({len(meta['rows'])} rows) "
          f"· {meta['skill']}", j["type"])
    c.save()
    return {"ok": True, "part": "recon", "rows": len(meta["rows"]), "skill": meta["skill"],
            "view": recon_view(c, j, root)}


def learning_layout(check: str, sponsor: str = "", root: Path | None = None) -> tuple[str, bytes]:
    if check not in R.SPEC or R.SPEC[check].get("pair"):
        raise LearningError("This check takes no client report.", 404)
    return f"client_report_{check}_layout.xlsx", R.layout_bytes(check, _rules(sponsor, root) if sponsor else None)
