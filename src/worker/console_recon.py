"""The Reconcile tab of the job screen in the grammar of the Issues tab: a table of checks (one row per check and
cause, exact counts, one status), and the affected records paged on request with the exact total.

Code decides; the page only draws. The figures are the ones `console_learning.recon_view` works out from the job's own
lines (the same engine as the tally and the written file); nothing here computes a figure or invents one. A cause's
label, options and recommended option come from the same skill table the Issues tab reads (the `rc*` rows of
`check-data-quality/tables/issue-options.yaml`), and the helpers that choose, note, audit and word a decision are the
Issues tab's own (`console_issues`).

A decision here is a recorded disposition of a break: it never changes a figure or the upload file. It needs a note,
it is written to the job's audit log with the reviewer's name BEFORE it takes effect, and an undo is an audited revert
(a hold's release needs its own note). Rows that are really decided elsewhere (rows still needing a person) point to
the Issues tab and take no decision here.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

from worker import console_engine as CE
from worker import console_issues as CI
from worker import console_learning as CL
from worker import persist
from worker import recon as R
from worker import record_facts as RF
from worker.audit import AuditError
from worker.console_issues import IssueError

GID_HEAD = "rc."
_REL = ("recon_apply|", "recon_undo|")
_RANK = {"open": 0, "hold": 1, "out": 2, "fix": 3}
_CACHE: dict[str, tuple[tuple[Any, ...], list[dict[str, Any]], dict[str, Any], tuple[Any, ...]]] = {}


# ---- the affected records, from the same figures the old card shows ----------------------------------------------
def _cause(row: dict[str, Any]) -> str:
    st, a, b = row["status"], row.get("a"), row.get("b")
    if st == "PASS":
        return ""
    if st == "":
        return "rcreport"
    if st == "REVIEW":
        return "rcjudge"
    return "rcone" if a is None or b is None else "rcdiff"


def _rec(ck: dict[str, Any], cause: str, mid: str, label: str, a: Any = None, b: Any = None, diff: Any = None,
         kind: str = "money", status: str = "", note: str = "") -> dict[str, Any]:
    key = hashlib.sha1(f"{ck['id']}|{mid}|{label}|{a}|{b}|{status}".encode()).hexdigest()[:10]
    return {"check": ck["id"], "name": ck["n"], "scope": ck["scope"], "cause": cause, "key": key, "label": label,
            "a": a, "b": b, "diff": diff, "kind": kind, "status": status, "note": note}


def _pending(c: Any, j: dict[str, Any]) -> list[tuple[str, str]]:
    """(label, why) of every line or row that still needs a person: the ones rows-in = rows-out is waiting on."""
    if RF.is_gl(j["type"]):
        return [(f"Journal {x['l'].j} line {x['l'].ln} · {x['l'].acct}", "; ".join(x.get("msgs") or []))
                for x in CE.gl_eval(c, j)["lines"] if x["st"] == "err"]
    return [(f"Row {r['row']} · {r['id'] or '(no ID)'} {r['nm'] or ''}".strip(), r["why"] or "")
            for r in CE.static_rows(c, j) if r["st"] == "err"]


def _signature(c: Any, j: dict[str, Any]) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """(what the records depend on, the objects whose identity it names, held so an id is never reused)."""
    other = CL._other_run(c, j) if RF.is_gl(j["type"]) else None
    folder = R.report_dir(c.job_dir(j))
    saved = tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in folder.glob("*")
                         if p.is_file())) if folder.is_dir() else ()
    ev = (CE.evaluation(c, j), CE.evaluation(c, other) if other else None)
    return (id(ev[0]), id(ev[1]), saved, CL._rules(j["sp"], c.root).digest), ev


def _load(c: Any, j: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every record of every check, and what the table needs to know about the checks, kept for as long as the job's
    evaluation, the other run and the loaded reports are the same, so paging costs a slice."""
    sig, ev = _signature(c, j)
    hit = _CACHE.get(str(j["id"]))
    if hit is not None and hit[0] == sig:
        return hit[1], hit[2]
    view = CL.recon_view(c, j, c.root, cap=None)
    recs: list[dict[str, Any]] = []
    if view.get("empty"):
        meta: dict[str, Any] = {"checks": [], "fund_types": [], "default_ft": ""}
        _CACHE[str(j["id"])] = (sig, recs, meta, ev)
        return recs, meta
    pend = _pending(c, j)
    layouts: dict[str, Any] = {}
    for ck in view["checks"]:
        if ck["id"] == "tie" and pend:
            recs += [_rec(ck, "rctie", "pending", lab, status="REVIEW", note=why) for lab, why in pend]
            continue
        if ck["state"] in ("gap", "waiting"):
            recs.append(_rec(ck, "rcgap", "check", ck["n"], status="", note=ck["note"]))
            continue
        for r in ck["rows"]:
            cause = _cause(r)
            if cause:
                recs.append(_rec(ck, cause, r.get("mid", ""), r["label"], r["a"], r["b"], r["diff"], r.get("kind", "money"),
                                 r["status"], r.get("note", "")))
        if ck["state"] == "needs-report" and not ck["rows"]:
            recs.append(_rec(ck, "rcreport", "report", ck["n"], note=ck["note"]))
        if ck.get("layout"):
            layouts[ck["id"]] = {"columns": ck["layout"]["columns"], "text": ck["layout"]["text"], "report": ck.get("report")}
    inv = [x for items in CI._grouped(c, j).values() for x in items if x["cid"] == "depmissing"]
    if inv:
        ck = {"id": "inv", "n": "Every investor is on file", "scope": ["DE", "DLP", "EL", "LP"]}
        recs += [_rec(ck, "rctie", "inv", f"Row {x['row']} · {x['r']['id']} {x['name']}".strip(), status="REVIEW",
                      note=x["why"] or "") for x in sorted(inv, key=lambda y: y["row"])]
    checks = [{"id": ck["id"], "n": ck["n"], "scope": ck["scope"]} for ck in view["checks"]]
    if inv:
        checks.append({"id": "inv", "n": "Every investor is on file", "scope": ["DE", "DLP", "EL", "LP"]})
    meta = {"checks": checks, "fund_types": view["fund_types"], "default_ft": view["default_ft"],
            "layouts": layouts, "skill": view.get("skill", {}).get("ident", "")}
    _CACHE[str(j["id"])] = (sig, recs, meta, ev)
    return recs, meta


# ---- decisions kept on the job -----------------------------------------------------------------------------------
def _hist(j: dict[str, Any]) -> list[dict[str, Any]]:
    return j.setdefault("recon_hist", [])


def _decided(j: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {k: h for h in _hist(j) if not h["rev"] for k in h["keys"]}


def _gid(check: str, cause: str) -> str:
    return f"{GID_HEAD}{check}.{cause}"


def _groups(c: Any, j: dict[str, Any], ft: str = "") -> list[dict[str, Any]]:
    recs, meta = _load(c, j)
    ft = ft or meta["default_ft"]
    done = _decided(j)
    cz = CI.causes()
    by: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in recs:
        if not ft or ft in r["scope"]:
            by.setdefault((r["check"], r["cause"]), []).append(r)
    out: list[dict[str, Any]] = []
    for (check, cause), items in by.items():
        items.sort(key=lambda r: (-abs(r["diff"] or 0), r["label"]))
        z = cz[cause]
        n = {"open": 0, "fix": 0, "out": 0, "hold": 0}
        for r in items:
            n[done[r["key"]]["state"] if r["key"] in done else "open"] += 1
        state = next(s for s in ("open", "hold", "out", "fix") if n[s])
        worst = {"ERROR": 2, "WARN": 1, "REVIEW": 1}
        impact = z.impact if cause in ("rcreport", "rcgap", "rctie") else (
            "block" if any(worst.get(r["status"], 0) == 2 for r in items) else "check")
        top = items[0]
        decisions = [{"id": h["id"], "who": h["who"], "label": h["lab"], "state": h["state"], "rows": h["rows"],
                      "note": h["note"], "at": h["at"], "release": h["opt"] == "park"}
                     for h in _hist(j) if not h["rev"] and h["check"] == check and h["cause"] == cause]
        out.append({
            "id": _gid(check, cause), "check": check, "name": top["name"], "cause": cause, "label": z.label,
            "family": z.family, "consequence": z.consequence, "impact": impact, "state": state, "n": len(items),
            "counts": n, "options": [{"id": k, "label": v} for k, v in CI._opts(z, n["open"], [])] if n["open"] else [],
            "recommended": "", "recommended_when": z.when,
            "example": {"label": top["label"], "a": top["a"], "b": top["b"], "diff": top["diff"], "kind": top["kind"]},
            "amount": round(sum(abs(r["diff"] or 0) for r in items if r["kind"] == "money"), 2),
            "decisions": decisions, "by": decisions[-1]["who"] if decisions else "",
            "layout": meta.get("layouts", {}).get(check) if cause == "rcreport" else None})
    order = {c_["id"]: i for i, c_ in enumerate(meta["checks"])}
    out.sort(key=lambda g: (_RANK[g["state"]], {"block": 0, "check": 1, "info": 2}[g["impact"]], order.get(g["check"], 99)))
    return out


def model(c: Any, j: dict[str, Any], *, page: int = 0, per: int = CI.DEFAULT_PAGE, status: str = "",
          ft: str = "") -> dict[str, Any]:
    """One page of the table (exact counts, no records), what the checks add up to, and the decisions made."""
    per = max(1, min(int(per), CI.PAGE_MAX))
    _recs, meta = _load(c, j)
    ft = ft or meta["default_ft"]
    every = _groups(c, j, ft)
    shown = [g for g in every if not status or g["state"] in CI._CHIP.get(status, ())]
    pages = max(1, -(-len(shown) // per))
    p = min(max(0, int(page)), pages - 1)
    on = [k for k in meta["checks"] if not ft or ft in k["scope"]]
    hit = {g["check"] for g in every}
    return {"units": "records", "groups": shown[p * per:(p + 1) * per], "total": len(shown), "page": p, "pages": pages,
            "per": per, "ft": ft, "fund_types": meta["fund_types"],
            "skill": meta.get("skill", "") if RF.is_gl(str(j["type"])) else "",  # the GL checker's name means nothing on a record file,
            "chips": {k: sum(1 for g in every if g["state"] in st) for k, st in CI._CHIP.items()},
            "checks": {"applies": len(on), "clean": sum(1 for k in on if k["id"] not in hit),
                       "records": sum(g["n"] for g in every), "open": sum(g["counts"]["open"] for g in every)},
            "next": next((g["id"] for g in every if g["state"] == "open"), None), "history": history(j),
            "approved": bool(j.get("approved"))}


def _find(c: Any, j: dict[str, Any], gid: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    recs, _meta = _load(c, j)
    check, _, cause = gid[len(GID_HEAD):].rpartition(".")
    items = [r for r in recs if r["check"] == check and r["cause"] == cause]
    if not gid.startswith(GID_HEAD) or not items:
        raise IssueError("That group is not in this view any more.")
    items.sort(key=lambda r: (-abs(r["diff"] or 0), r["label"]))
    return {"id": gid, "check": check, "cause": cause}, items


def rows_page(c: Any, j: dict[str, Any], gid: str, *, page: int = 0, per: int = CI.DEFAULT_PAGE, q: str = "",
              state: str = "") -> dict[str, Any]:
    """One page (default 50, at most 200) of a group's records, with the exact total. `state` narrows to one
    disposition (open, fix, out, hold); `q` filters on the label."""
    per = max(1, min(int(per), CI.PAGE_MAX))
    _g, items = _find(c, j, gid)
    done = _decided(j)
    rows = [dict(r, state=done[r["key"]]["state"] if r["key"] in done else "open") for r in items]
    needle = q.strip().lower()
    if needle:
        rows = [r for r in rows if needle in f"{r['label']} {r['note']}".lower()]
    if state:
        rows = [r for r in rows if r["state"] == state]
    total = len(rows)
    pages = max(1, -(-total // per))
    p = min(max(0, int(page)), pages - 1)
    chunk = rows[p * per:(p + 1) * per]
    keep = ("label", "a", "b", "diff", "kind", "status", "note", "state")
    return {"total": total, "start": p * per + 1 if chunk else 0, "end": p * per + len(chunk), "per": per, "page": p,
            "pages": pages, "rows": [{k: r[k] for k in keep} for r in chunk]}


# ---- deciding ----------------------------------------------------------------------------------------------------
def _choose(c: Any, j: dict[str, Any], pick: dict[str, Any], note: str = ""
            ) -> tuple[dict[str, Any], CI.Cause, list[dict[str, Any]], str, str]:
    gid, opt = str(pick.get("id", "")), str(pick.get("option", ""))
    g, items = _find(c, j, gid)
    done = _decided(j)
    open_items = [r for r in items if r["key"] not in done]
    cz = CI.causes()[g["cause"]]
    if opt == "go":
        raise IssueError("these rows are decided on the Issues tab, where the change takes effect")
    if opt not in dict(CI._opts(cz, len(open_items), [])):
        raise IssueError(f"“{opt}” is not an option for {cz.label.lower()}")
    if not open_items:
        raise IssueError("Every record of this group is already decided.")
    text = CI._note(str(pick.get("value") or note), "A reconciliation decision")
    return g, cz, open_items, opt, text


def preview(c: Any, j: dict[str, Any], picks: list[dict[str, Any]], note: str = "") -> dict[str, Any]:
    """What would be recorded, in exact counts and on a sample. Nothing is written and nothing is audited."""
    out = []
    for p in picks:
        g, cz, items, opt, text = _choose(c, j, p, note)
        out.append({"id": g["id"], "cause": cz.label, "check": items[0]["name"], "option": opt,
                    "label": dict(cz.options)[opt], "n": len(items), "note": text,
                    "rows": [{"label": r["label"], "a": r["a"], "b": r["b"], "diff": r["diff"]} for r in items[:3]]})
    return {"groups": out, "n": sum(g["n"] for g in out), "units": "records"}


def apply(c: Any, j: dict[str, Any], picks: list[dict[str, Any]], who: str, *, note: str = "") -> dict[str, Any]:
    """Decide groups. The reviewer is a named person and a note is required. Each decision is written to the job's
    audit log first; if that fails nothing changes."""
    person = CI._person(who)
    if j.get("approved"):
        raise IssueError("This job is approved, so it is final. Nothing changed.")
    if not picks:
        raise IssueError("Choose a group first.")
    chosen = [_choose(c, j, p, note) for p in picks]
    if len({g["id"] for g, *_ in chosen}) != len(chosen):
        raise IssueError("a group is listed twice")
    hid = max([h["id"] for h in _hist(j)] + [0])
    batch = max([h["batch"] for h in _hist(j)] + [0]) + 1
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    entries, plan = [], []
    for k, (g, cz, items, opt, text) in enumerate(chosen):
        label = dict(cz.options)[opt]
        why = f"{person} chose “{label}” for {len(items):,} records of {items[0]['name']} ({cz.label}). Note: {text}"
        entries.append((why,
                        {"job": j["id"], "group": g["id"], "check": g["check"], "cause": g["cause"], "option": opt,
                         "records": len(items), "reviewer": person, "batch": batch, "at": now, "note": text,
                         "first": [r["label"] for r in items[:5]]}, {"batch": batch}))
        plan.append((hid + k + 1, g, items, opt, label, text))
    try:
        persist.job_audit(c, j).record_many("recon.decided", entries)
    except (AuditError, OSError, ValueError) as exc:
        raise IssueError(f"I couldn't record the decision, so nothing changed: {exc}") from exc
    done = []
    for n, g, items, opt, label, text in plan:
        _hist(j).append({"id": n, "t": time.strftime("%H:%M:%S"), "who": person, "rev": False, "check": g["check"],
                         "name": items[0]["name"], "cause": g["cause"], "opt": opt, "lab": label,
                         "state": CI._STATE[opt], "rows": len(items), "keys": [r["key"] for r in items],
                         "note": text, "at": now, "batch": batch, "released": None})
        c.log(f"{label}: {len(items):,} records · {items[0]['name']}", f"{person}; reconciliation, no figure changed")
        done.append({"id": n, "group": g["id"], "rows": len(items), "label": label})
    return {"batch": batch, "decisions": done}


def undo(c: Any, j: dict[str, Any], who: str, *, decision: int = 0, batch: int = 0, reason: str = "") -> dict[str, Any]:
    """Revert one decision, or every decision of a batch, audited first. A hold's release needs a note."""
    person = CI._person(who)
    if j.get("approved"):
        raise IssueError("This job is approved, so it is final. Nothing changed.")
    todo = [h for h in _hist(j) if not h["rev"] and (h["id"] == decision if decision else h["batch"] == batch)]
    if not todo:
        raise IssueError("There is nothing to undo there.")
    why = CI._note(reason, "Releasing a hold") if any(h["opt"] == "park" for h in todo) else \
        " ".join(str(reason or "").split()) or f"undone by {person}"
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        persist.job_audit(c, j).record_many("recon.reverted", [
            (f"{person} reverted “{h['lab']}” for {h['rows']:,} records of {h['name']}: {why}",
             {"job": j["id"], "decision": h["id"], "check": h["check"], "cause": h["cause"], "records": h["rows"],
              "reviewer": person, "batch": h["batch"], "at": now, "note": why, "release": h["opt"] == "park"}, None)
            for h in todo])
    except (AuditError, OSError, ValueError) as exc:
        raise IssueError(f"I couldn't record the undo, so nothing changed: {exc}") from exc
    for h in reversed(todo):
        h["rev"], h["released"] = True, {"by": person, "at": now, "note": why}
        c.log(f"Reverted: {h['lab']} for {h['rows']:,} records · {h['name']}", f"{person} undid a reconciliation decision")
    return {"reverted": [h["id"] for h in todo]}


def history(j: dict[str, Any]) -> list[dict[str, Any]]:
    cz = CI.causes()
    return [{"id": h["id"], "when": h["t"], "who": h["who"], "label": h["lab"], "check": h["name"],
             "cause": cz[h["cause"]].label, "rows": h["rows"], "note": h["note"], "at": h["at"],
             "reverted": h["rev"], "released": h["released"], "release": h["opt"] == "park"} for h in reversed(_hist(j))]


# ---- the chat: a sentence about a group becomes a proposal with one Apply ----------------------------------------
def _say(j: dict[str, Any], text: str, options: list[dict[str, Any]] | None = None) -> None:
    for m in j["chat"]:  # an older proposal's button no longer applies once there is a newer message
        if any(str(o.get("action", "")).startswith(_REL) for o in m.get("options", [])):
            m["options"] = []
    j["chat"].append({"who": "w", "role": "Checker", "text": text, "options": options or []})


def chat_reply(c: Any, j: dict[str, Any], gid: str, text: str) -> bool:
    """A sentence typed while a reconciliation group is open in the chat. When it names one of the group's options the
    worker answers with a preview and ONE Apply; a held group answers to "Release: why". False when it is not about
    a reconciliation group or an option, so the ordinary paths take it."""
    if j.get("approved") or not gid.startswith(GID_HEAD):
        return False
    try:
        g, items = _find(c, j, gid)
    except IssueError:
        return False
    cz = CI.causes()[g["cause"]]
    done = _decided(j)
    open_items = [r for r in items if r["key"] not in done]
    hold = next((h for h in reversed(_hist(j)) if not h["rev"] and h["opt"] == "park" and h["check"] == g["check"]
                 and h["cause"] == g["cause"]), None)
    got = CI._match(CI._opts(cz, len(open_items), []), text) if open_items else None
    if got is None and hold is not None and text.strip().lower().startswith("release"):
        j["chat"].append({"who": "you", "text": text.strip()})
        rest = text.strip()[len("release"):].lstrip(" :,-").strip()
        try:
            note = CI._note(rest, "Releasing a hold")
        except IssueError as exc:
            _say(j, f"{exc} Type it after “Release:”. Nothing changed.")
            return True
        _say(j, f"Release {hold['rows']:,} records from the hold? They return to Open. Your note is recorded with your "
                f"name: “{note}”.\nNothing changes until you press Release.",
             [{"label": "Release", "action": f"recon_undo|{hold['id']}|{note}", "primary": True}])
        return True
    if got is None:
        return False
    opt, value, note = got
    j["chat"].append({"who": "you", "text": text.strip()})
    note = value if opt == "park" else note or value
    try:
        pv = preview(c, j, [{"id": gid, "option": opt, "value": note}])
    except IssueError as exc:
        _say(j, f"{exc} Nothing changed.")
        return True
    x = pv["groups"][0]
    lines = [f"{x['label']}: {x['n']:,} records of {x['check']} in “{cz.label}” will be recorded."]
    lines += [f"  {r['label']}: {r['a']} against {r['b']}" for r in x["rows"]]
    lines.append(f"No figure changes. Your note is recorded with your name: “{x['note']}”.")
    lines.append("Nothing changes until you press Apply.")
    _say(j, "\n".join(lines), [{"label": "Apply", "action": f"recon_apply|{gid}|{opt}|{x['note']}", "primary": True}])
    return True


def chat_apply(c: Any, j: dict[str, Any], gid: str, opt: str, who: str, note: str) -> None:
    d = apply(c, j, [{"id": gid, "option": opt, "value": note}], who)["decisions"][0]
    _say(j, f"Done. {d['label']}: {d['rows']:,} records · recorded for {who}."
         + (" To release it, open the group in the chat and say “Release:” with a reason." if opt == "park" else ""),
         [] if opt == "park" else [{"label": "Undo", "action": f"recon_undo|{d['id']}"}])


def chat_undo(c: Any, j: dict[str, Any], decision: int, who: str, note: str = "") -> None:
    undo(c, j, who, decision=decision, reason=note)
    _say(j, "Reverted. The records are open again.")
