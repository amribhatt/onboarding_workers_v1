"""UI parity: the existing-records page, the entity (fund) popover and the sponsor-specific settings.

View models read the one store (`worker.registry`, through `Console.reg_of`) and the console's own state; actions
write only through `Console.ext_load` and the sponsor's own settings, and every action is logged with its reason.
Nothing here invents a number: an empty store says so.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from worker import record_facts as RF
from worker.errors import StatusError

TAIL = 4  # the mockup shows the last four records of each type


class RecordsError(StatusError):
    pass


def _sponsor(c: Any, sponsor: str | None) -> str:
    name = sponsor or c.S["sp"]
    if name not in c.S["ent"]:
        raise RecordsError(f"unknown sponsor {name!r}", 404)
    return name


# ---- existing records ---------------------------------------------------------------------------------------
def existing_view(c: Any, sponsor: str | None = None) -> dict[str, Any]:
    sp = _sponsor(c, sponsor)
    reg = c.reg_of(sp)
    types = []
    for t in RF.stored_types():
        rows = reg.get(t, [])
        by: dict[str, int] = {}
        for r in rows:
            by[r["src"]] = by.get(r["src"], 0) + 1
        e = c.ext_of(t, sp) or {}
        types.append({
            "type": t, "count": len(rows), "text": c.store_text(t, sp),
            "ext": {"ans": e.get("ans", ""), "file": e.get("file", "")},
            "sources": [{"source": k, "n": n} for k, n in by.items()],
            "tail": [{"id": r["id"], "name": r["name"], "src": r["src"]} for r in rows[-TAIL:]],
        })
    return {"sponsor": sp, "types": types, "entities": c.ents_of(sp), "total": sum(x["count"] for x in types)}


# ---- the entity (fund) popover ------------------------------------------------------------------------------
def fund_view(c: Any, sponsor: str | None = None) -> dict[str, Any]:
    sp = _sponsor(c, sponsor)
    ents = c.ents_of(sp)
    cur = c.S["fund"] if c.S["fund"] in ents else (ents[0] if ents else "")
    out = []
    for n in ents:
        em = c.S["EM"].get(n, {})
        done = c.static_done(n, sp)
        out.append({"name": n, "id": em.get("id", ""), "live": em.get("live", ""), "gid": em.get("gid", ""),
                    "gn": em.get("gn", ""), "static_done": done, "static_complete": done == len(RF.static_types())})
    return {"sponsor": sp, "current": cur, "total": len(ents), "entities": out}


# ---- sponsor-specific settings ------------------------------------------------------------------------------
def settings_view(c: Any, sponsor: str | None = None) -> dict[str, Any]:
    sp = _sponsor(c, sponsor)
    rec = next((s for s in c.S["sponsors"] if s["n"] == sp), {})
    req = []
    for k, v in c.S["req"].items():
        parts = k.split("|")
        if len(parts) == 3 and parts[0] == sp:
            req.append({"type": parts[1], "field": parts[2], "required": bool(v)})
    return {
        "sponsor": sp, "id": rec.get("id", ""), "name": sp, "live": rec.get("live", ""),
        "counters": c.counters_of(sp), "suffix": bool(c.S["lsuffix"].get(sp)), "required": req,
        "preferences": [str(x.get("t", "")) for x in c.S["book"].get(sp, [])],
    }


# ---- actions ------------------------------------------------------------------------------------------------
def records_act(c: Any, body: dict[str, Any]) -> dict[str, Any]:
    """One write. `op`: sample | none | golive | suffix | reset_req. Returns the refreshed view of the touched page."""
    op, rtype, value = str(body.get("op", "")), str(body.get("type", "")), str(body.get("value", "")).strip()
    sp = _sponsor(c, body.get("sponsor"))
    prev = c.S["sp"]
    c.S["sp"] = sp  # Console.ext_load and Console.log read the current sponsor
    try:
        if op in ("sample", "none"):
            if rtype not in RF.stored_types():
                raise RecordsError(f"unknown record type {rtype!r}")
            if op == "sample":
                c.ext_load(rtype)
            else:
                c.S["ext"][f"{sp}|{rtype}"] = {"ans": "none"}
                c.log(f"No {rtype.lower()} in Ledgerline outside the app for {sp}",
                      "answered on the existing-records page", "")
            c.save()
            return existing_view(c, sp)
        if op == "golive":
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise RecordsError("The go-live date is mandatory: give it as YYYY-MM-DD.") from exc
            rec = next(s for s in c.S["sponsors"] if s["n"] == sp)
            old, rec["live"] = rec.get("live", ""), value
            c.log(f"Changed {sp} go-live from {old or 'not set'} to {value}",
                  "edited in sponsor-specific settings", "")
        elif op == "suffix":
            on = value.lower() in ("1", "true", "on", "yes")
            c.S["lsuffix"][sp] = on
            c.log(f"{'Treat' if on else 'Stopped treating'} Ltd and Limited as one word for {sp}",
                  "edited in sponsor-specific settings", "")
        elif op == "reset_req":
            key = f"{sp}|{rtype}|{value}"
            if key not in c.S["req"]:
                raise RecordsError("no such override")
            del c.S["req"][key]
            c.S.get("req_ctl", {}).pop(key, None)
            c.log(f"Reset the Required? override of {value} ({rtype}) for {sp}",
                  "edited in sponsor-specific settings", "")
        else:
            raise RecordsError(f"unknown operation {op!r}")
        c.save()
        return settings_view(c, sp)
    finally:
        c.S["sp"] = prev


def load_export(c: Any, sponsor: str, rtype: str, path: Path) -> dict[str, Any]:
    """An uploaded Ledgerline export for one record type; the rows enter the one store as `outside export`."""
    sp = _sponsor(c, sponsor)
    if rtype not in RF.stored_types():
        raise RecordsError(f"unknown record type {rtype!r}")
    prev = c.S["sp"]
    c.S["sp"] = sp
    try:
        c.ext_load(rtype, path)
        c.save()
    finally:
        c.S["sp"] = prev
    return existing_view(c, sp)
