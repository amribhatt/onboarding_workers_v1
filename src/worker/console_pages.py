"""View models of the console's global pages: the left nav, the dashboard and the work lists.

Every figure is counted from the console state (sponsors, entities, jobs); nothing is a placeholder. A page the
state cannot fill says so rather than showing a number.
"""

from __future__ import annotations

import csv
import statistics
from datetime import date, datetime
from pathlib import Path
from typing import Any

from worker import templates as T
from worker.agent import PROVIDERS

AGING_DAYS = 7  # a job waiting for approval longer than this is aging
DUE_SOON_DAYS = 60  # a sponsor whose go-live is this close and not complete is due soon


def _days(iso: str) -> int | None:
    try:
        return (datetime.strptime(iso, "%Y-%m-%d").date() - date.today()).days
    except ValueError:
        return None


def entity_progress(f: dict[str, Any]) -> int:
    """Stages of the per-entity chain done, of five: static, GL, bundle, client services, migrated."""
    static = f["signed"]["static"] or sum(1 for v in f["static"].values() if v) >= 5
    gl = f["signed"]["gl"] or all(f["gl"].values())
    return sum([bool(static), bool(gl), bool(f["bundle"] or f["client"] == 1 or f["migrated"]),
                f["client"] == 1 or bool(f["migrated"]), bool(f["migrated"])])


def sponsor_progress(c: Any, name: str) -> tuple[str, int]:
    """(status, percent): Completed when every entity migrated; Due soon when go-live is near; Aging when it has
    passed or an approval has waited too long; else Active. Percent is the share of chain stages done."""
    ents = c.ents_of(name)
    pct = round(100 * sum(entity_progress(c.S["F"][e]) for e in ents) / (5 * len(ents))) if ents else 0
    if ents and all(c.S["F"][e]["migrated"] for e in ents):
        return "Completed", 100
    live = next((s["live"] for s in c.S["sponsors"] if s["n"] == name), "")
    left = _days(live)
    old = any(aging(j) for j in c.S["jobs"] if j.get("sp") == name and j.get("status") == "pending")
    if old or (left is not None and left < 0):
        return "Aging", pct
    if left is not None and left <= DUE_SOON_DAYS:
        return "Due soon", pct
    return "Active", pct


def aging(j: dict[str, Any]) -> bool:
    d = j.get("d") or ""
    days = _days(d)
    return days is not None and -days > AGING_DAYS


def pending_jobs(c: Any) -> list[dict[str, Any]]:
    return [j for j in c.S["jobs"] if j.get("status") == "pending"]


def rework(c: Any) -> list[dict[str, str]]:
    """Entities client services sent back with changes requested."""
    out = []
    for sponsor, names in c.S["ent"].items():
        for n in names:
            if c.S["F"][n]["client"] == -1:
                out.append({"sponsor": sponsor, "fund": n})
    return out


def nav_view(c: Any) -> dict[str, Any]:
    """The left nav: counts for the work lists and the sponsor switcher. `harness` is the extension point for the
    Harness group (Improvements, Health, Skills library): each entry is {view, label, icon, count}; the pages are
    drawn by static/harness.js."""
    return {
        "pending": len(pending_jobs(c)), "rejected": len(rework(c)),
        "aging": sum(1 for j in pending_jobs(c) if aging(j)),
        "harness": [{"view": "impr", "label": "Improvements", "icon": "bulb", "count": 0},
                    {"view": "health", "label": "Health", "icon": "pulse", "count": 0},
                    {"view": "skills", "label": "Skills library", "icon": "book", "count": 0}],
        "sponsors": [s["n"] for s in c.S["sponsors"]],
        "active_sponsor": c.S["sp"] if c.S.get("view") in ("sponsor", "job") else "",
        "pinned": {"fund": c.S["fund"], "type": (c.job() or {}).get("type", "")},
        "view": c.S.get("view", "dash"),
    }


def _share(n: int, total: int) -> str:
    return f"{round(100 * n / total)}%" if total else "0%"


def dashboard_view(c: Any) -> dict[str, Any]:
    names = [s["n"] for s in c.S["sponsors"]]
    st = {n: sponsor_progress(c, n) for n in names}
    order = ["Active", "Due soon", "Aging", "Completed"]
    count = {k: sum(1 for v in st.values() if v[0] == k) for k in order}
    ents = [e for n in names for e in c.ents_of(n)]
    ent_state: dict[str, int] = dict.fromkeys(order, 0)
    for n in names:
        for e in c.ents_of(n):
            f = c.S["F"][e]
            if f["migrated"]:
                ent_state["Completed"] += 1
                continue
            left = _days(c.S["EM"].get(e, {}).get("live", ""))
            if left is not None and left < 0:
                ent_state["Aging"] += 1
            elif left is not None and left <= DUE_SOON_DAYS:
                ent_state["Due soon"] += 1
            else:
                ent_state["Active"] += 1
    jobs = c.S["jobs"]
    pend = sum(1 for j in jobs if j.get("status") == "pending")
    appr = sum(1 for j in jobs if j.get("status") == "approved")
    back = len(rework(c))

    def legend(counts: dict[str, int], total: int) -> list[dict[str, Any]]:
        return [{"label": k, "n": counts[k], "pc": _share(counts[k], total), "color": "--" + "abcd"[i]}
                for i, k in enumerate(order)]

    cycle = _cycle_days(c)
    return {
        "tiles": [
            {"title": "Sponsors", "total": len(names), "rows": legend(count, len(names))},
            {"title": "Entities (funds)", "total": len(ents), "rows": legend(ent_state, len(ents))},
            {"title": "Jobs", "total": pend + appr + back, "rows": [
                {"label": "Pending approval", "n": pend, "pc": _share(pend, pend + appr + back), "color": "--b"},
                {"label": "Approved", "n": appr, "pc": _share(appr, pend + appr + back), "color": "--a"},
                {"label": "Needs rework", "n": back, "pc": _share(back, pend + appr + back), "color": "--c"}]},
        ],
        "cycle": cycle,
        "sponsors": [{"id": s["id"], "name": s["n"], "live": s["live"], "status": st[s["n"]][0],
                      "pct": st[s["n"]][1], "entities": len(c.ents_of(s["n"]))} for s in c.S["sponsors"]],
    }


def _cycle_days(c: Any) -> dict[str, Any]:
    """Median days from a sponsor's first recorded action to its last entity migrating. None until one completes."""
    done = []
    for s in c.S["sponsors"]:
        ents = c.ents_of(s["n"])
        if ents and all(c.S["F"][e]["migrated"] for e in ents):
            days = sorted(a["d"] for a in c.S["audit"] if a.get("sp") == s["n"] and a.get("d"))
            if len(days) >= 2:
                done.append((datetime.strptime(days[-1], "%Y-%m-%d") - datetime.strptime(days[0], "%Y-%m-%d")).days)
    return {"median": statistics.median(done) if done else None, "completed": len(done)}


def list_view(c: Any, kind: str) -> dict[str, Any]:
    if kind == "rejected":
        rows = [{"sponsor": r["sponsor"], "fund": r["fund"], "type": "Bundle", "rows": "", "by": "Client services",
                 "status": "Rework"} for r in rework(c)]
    else:
        jobs = pending_jobs(c) if kind == "pending" else [j for j in pending_jobs(c) if aging(j)]
        rows = [{"sponsor": j["sp"], "fund": j["fund"], "type": j["type"], "rows": j.get("rows", 0),
                 "by": "AI preparer", "status": "Awaiting you", "id": j["id"]} for j in jobs]
    sub = {"pending": "I prepared these. You approve them. Nothing is written until you do.",
           "rejected": "Entities client services sent back with changes requested.",
           "aging": f"Jobs waiting for approval for more than {AGING_DAYS} days."}[kind]
    title = {"pending": "Pending approval", "rejected": "Rejected", "aging": "Aging"}[kind]
    return {"title": title, "sub": sub, "rows": rows}


# ---- all-sponsor standards: the real template files, the Golden chart of accounts, the transaction type mapping ----
COA_FILE = "Golden_COA.csv"
TTM_FILE = "Transaction_Type_Mapping.csv"


def standard_files() -> dict[str, Path]:
    """Every file the standards pages offer for download, by file name. Real files only."""
    out: dict[str, Path] = {}
    for name in T.TEMPLATES:
        path = T.template_file("", name)
        if path is not None:
            out[path.name] = path
    for name in (COA_FILE, TTM_FILE):
        path = T.standards_dir() / name
        if path.is_file():
            out[name] = path
    return out


def _csv(name: str) -> list[dict[str, str]] | None:
    path = T.standards_dir() / name
    if not path.is_file():
        return None
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [{k: (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(fh)]


def _pending(root: Path | None) -> dict[str, dict[str, Any]]:
    """Uploaded replacements still waiting for a human in Improvements, by file name."""
    from worker.console_standards import waiting_for

    return waiting_for(root)


def templates_view(root: Path | None = None) -> dict[str, Any]:
    wait = _pending(root)
    rows = []
    for name, spec in T.TEMPLATES.items():
        path = T.template_file("", name)
        rows.append({
            "name": "GL Fund and GL LP" if name == "GL" else name, "type": name,
            "file": path.name if path else "", "columns": len(spec.fields), "required": len(spec.required),
            "note": spec.description or f"{len(spec.required)} required of {len(spec.fields)} columns.",
            "status": "Configured" if path else "File missing",
            "review": wait.get(path.name if path else f"{name}_Upload_Template.xlsx")})
    return {"kind": "tmpls", "count": len(rows), "rows": rows,
            "added": [w for f, w in wait.items() if w["action"] == "add"]}


def coa_view(q: str = "", group: str = "All", root: Path | None = None) -> dict[str, Any]:
    all_rows = _csv(COA_FILE)
    if all_rows is None:
        return {"kind": "coa", "file": COA_FILE, "missing": True, "total": 0, "shown": 0, "groups": ["All"], "rows": [],
                "review": _pending(root).get(COA_FILE)}
    groups = ["All", *dict.fromkeys(r.get("FS_Grouping", "") for r in all_rows if r.get("FS_Grouping"))]
    needle = q.strip().lower()
    rows = []
    for r in all_rows:
        if group != "All" and r.get("FS_Grouping") != group:
            continue
        text = f"{r.get('Account_Number', '')} {r.get('Account_Name', '')} {r.get('FS_Line_Item', '')}".lower()
        if needle and needle not in text:
            continue
        rows.append({"account": r.get("Account_Number", ""), "title": r.get("Account_Name", ""),
                     "grouping": r.get("FS_Grouping", ""), "line": r.get("FS_Line_Item", ""),
                     "inside": r.get("Inside_Commitment", ""), "closing_to": r.get("Closing_Account", ""),
                     "closing_type": r.get("Closing_Type", "")})
    return {"kind": "coa", "file": COA_FILE, "missing": False, "total": len(all_rows), "shown": len(rows),
            "groups": groups, "rows": rows, "review": _pending(root).get(COA_FILE)}


def ttm_view(q: str = "", root: Path | None = None) -> dict[str, Any]:
    all_rows = _csv(TTM_FILE)
    if all_rows is None:
        return {"kind": "ttm", "file": TTM_FILE, "missing": True, "total": 0, "shown": 0, "rows": [],
                "review": _pending(root).get(TTM_FILE)}
    needle = q.strip().lower()
    rows = [{"code": r.get("Transaction_Type_Code", ""), "name": r.get("Transaction_Type_Name", ""),
             "accounts": r.get("Allowed_Accounts", "")} for r in all_rows
            if not needle or needle in " ".join(r.values()).lower()]
    return {"kind": "ttm", "file": TTM_FILE, "missing": False, "total": len(all_rows), "shown": len(rows), "rows": rows,
            "review": _pending(root).get(TTM_FILE)}


# ---- the model roles the worker really has: agent.ROLES, each with the model its env settings resolve to ----
ROLE_DOES = {
    "plan": "Planning steps. Set with WORKER_MODEL_PLAN.",
    "exec": "Execution steps such as batch key decisions (resolve.ModelDecider). Set with WORKER_MODEL_EXEC.",
    "search": "Search and lookup steps. Set with WORKER_MODEL_SEARCH.",
    "small": "Small, cheap steps. Set with WORKER_MODEL_SMALL.",
}


def model_roles(env: Any) -> list[dict[str, Any]]:
    """One row per role in `worker.agent.ROLES` with the model and fallbacks its settings resolve to. `explicit`
    is False when the role has no setting of its own and inherits WORKER_MODEL. No step of the console's agent
    path requests a role yet (the worker agent uses WORKER_MODEL), so `used` is False for each: said, not hidden."""
    from worker.agent import DEFAULT_MODEL, ROLES, role_setting

    base = env.get("WORKER_MODEL") or DEFAULT_MODEL
    demo = (env.get("WORKER_MODEL_PROVIDER") or "").lower() == "demo"  # the offline model ignores every setting
    out = []
    for role in ROLES:
        own = env.get(f"WORKER_MODEL_{role.upper()}") or ""
        shown = "demo (offline scripted model)" if demo else own or base
        out.append({"role": role, "does": ROLE_DOES[role], "model": shown, "explicit": bool(own),
                    "fallbacks": role_setting(env, role, "WORKER_MODEL_FALLBACKS"),
                    "env": f"WORKER_MODEL_{role.upper()}", "used": False})
    return out


def settings_view(health: dict[str, Any] | None) -> dict[str, Any]:
    """The model settings as the service reports them. One model serves every role, so there is one row."""
    h = health or {}
    return {"provider": h.get("provider", ""), "providers": list(PROVIDERS),
            "model": h.get("model", ""), "fallbacks": h.get("fallbacks", ""),
            "key_configured": bool(h.get("key_configured")), "available": health is not None,
            "roles": h.get("roles", [])}


def pages_view(c: Any, health: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything the shell needs for the current view, computed from state. Only the current page is built."""
    view = c.S.get("view", "dash")
    out: dict[str, Any] = {"nav": nav_view(c), "dashboard": None, "list": None, "standards": None, "settings": None}
    if view == "dash":
        out["dashboard"] = dashboard_view(c)
    elif view in ("pending", "rejected", "aging"):
        out["list"] = list_view(c, view)
    elif view == "tmpls":
        out["standards"] = templates_view(getattr(c, "standards_root", None))
    elif view == "coa":
        out["standards"] = coa_view(root=getattr(c, "standards_root", None))
    elif view == "ttm":
        out["standards"] = ttm_view(root=getattr(c, "standards_root", None))
    elif view == "settings":
        out["settings"] = settings_view(health)
    return out


__all__ = ["coa_view", "dashboard_view", "list_view", "model_roles", "nav_view", "pages_view", "settings_view",
           "sponsor_progress", "standard_files", "templates_view", "ttm_view"]
