"""The Issues table of the job screen: one row per cause and pattern, exact counts, decisions applied to a group by
rule, and the same rows paged on request.

Code decides; the page only draws. Every option, label and recommended rule of a cause is read from the skill table
`check-data-quality/tables/issue-options.yaml`; nothing here names one. A group's rows never travel as a whole: the
model carries counts, `rows_page` carries one page, `source_window` one window of the source sheet read from the
Parquet cache (`worker.columnar`), never a re-parse of the workbook.

A decision is audited (job audit log, with the reviewer's name and the reason) BEFORE the job changes; if the record
cannot be written nothing changes. An undo is an audited revert. Rows parked for the client are left out of the
upload file ("on hold") until the person releases them.
"""

from __future__ import annotations

import bisect
import hashlib
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from worker import columnar, persist
from worker import console_engine as CE
from worker import console_view as V
from worker import engine_console as EC
from worker import record_facts as RF
from worker.audit import AuditError
from worker.decide import Table
from worker.errors import StatusError

OPTIONS_FILE = (EC.QUALITY_SKILL, "tables", "issue-options.yaml")
DEFAULT_PAGE = 50
CHANGES_LOGGED = 50  # before/after pairs written to the audit per decision (the first rows, the count is exact)
CHANGES_KEPT = 5  # and kept in the job's history for the Activity tab
CHOICE_SLOTS = ("dec", "dates", "fill", "dealfill", "acct", "qa", "cells")  # where a decision writes the job's choices
ROWS_PER_DECISION = 500  # a decision that names its rows; a whole group is decided without naming them
NOTE_MAX = 500  # a hold's reason, a release's note and a later decision's note: free text, kept as data
PAGE_MAX = 200
WINDOW_MAX = 60
CLOSING_DATE = "2025-06-30"  # what Console.set_date writes: the closing date of the batch's other rows
_STATE = {"fix": "fix", "edit": "fix", "out": "out", "park": "hold", "cell": "fix"}
_ID_CAUSES = ("idlong", "idcollide", "keyclash", "badchars", "collidegen")
_DUP_CAUSES = ("keyclash", "nameclash", "suffix", "similar")


class IssueError(StatusError):
    """A refusal of the issues surface, said in plain words; the web layer turns it into a 409."""


# ---- the cause table -------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Cause:
    id: str
    label: str
    family: str
    impact: str
    consequence: str
    options: tuple[tuple[str, str], ...]
    recommended: str
    when: str
    edit_many: bool
    example: tuple[str, str]
    types: tuple[str, ...]
    provisional: bool = False
    engine_code: str = ""


def _load(path: Path) -> dict[str, Cause]:
    out: dict[str, Cause] = {}
    for row in Table.load(path).rows[:-1]:  # the last row is the catch-all the table format requires
        t = row["then"]
        ex = str(t["example"]).split("|", 1) + [""]
        out[str(row["when"]["code"])] = Cause(
            str(row["id"]), t["label"], t["family"], t["impact"], t["consequence"],
            tuple((o.split(":", 1)[0], o.split(":", 1)[1]) for o in str(t["options"]).split("|")),
            t["recommended"], t["recommended_when"], t["edit_many"] == "yes", (ex[0], ex[1]),
            tuple(x for x in str(t["types"]).split("|") if x), t["provisional"] == "yes", t["engine_code"])
    return out


def causes() -> dict[str, Cause]:
    return EC._cached(EC._skill_file(*OPTIONS_FILE), _load)  # type: ignore[no-any-return]


# ---- what each row is -------------------------------------------------------------------------------------
def cause_of(x: dict[str, Any]) -> str:
    w = x.get("why") or ""
    if "l" in x:
        a = x.get("a0", "")
        return ("glref" if a == "quick" else ("glacct" if re.match(r"^Account ", w) else "gltt") if a == "acct"
                else "gldeal" if a == "deal" else "gljr" if a == "jr" else "other")
    f = x["r"].get("fl") or []
    for rx, cid in ((r"^Same key on file", "keyclash"), (r"^Same name on file under another key", "nameclash"),
                    (r"^Same name if Ltd", "suffix"), (r"^Commitment_Date is blank", "blankdate"),
                    (r" is blank$", "blankreq"), (r"is not on file", "depmissing"),
                    (r"characters Ledgerline does not accept", "badchars"), (r"^Generated ID", "collidegen"),
                    (r"^Same affiliate", "similar"), (r"^In the GL but not", "glonly"),
                    (r"after cutting", "idcollide"), (r"characters, limit", "idlong")):
        if re.search(rx, w):
            return cid
    if x["s"].get("c") and "fz|" in x["s"]["c"] or "fuzzy" in f:
        return "fuzzy"
    return "other"


def _norm(s: str) -> str:
    s = re.sub(r"\band\b", "", str(s or "").lower()).replace("&", "")
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\b(ltd|limited)\b", "", s))


def pattern_of(cid: str, x: dict[str, Any]) -> tuple[str, str, str]:
    """(pattern label, value before, proposed value after), copied from the item by code."""
    s = x["s"]
    short = s["t"] if len(s["t"]) <= 60 else s["t"][:57] + "…"
    if "l" in x:
        ln = x["l"]
        deal = " · deal blank" if ln.noDeal else ""
        return ((s.get("d") or "").split(" · ")[0] or cid, f"{ln.acct} {ln.an}{deal} ({ln.j})", short)
    r = x["r"]
    if cid == "idlong":
        m = re.match(r"^[A-Z]+-[A-Z]+-", r["id"])
        p = m.group(0) if m else ""
        return (f"{p} + {len(r['id']) - len(p)} characters" if p else "over the limit", r["id"], r["id"][len(p):])
    if cid in _DUP_CAUSES:
        h = (r.get("hit") or "").split(" · ")[-1]
        return ("differs only by punctuation, spacing or and/&" if _norm(r["nm"]) == _norm(h)
                else "a different word in the name", f"{r['id'] or ''} {r['nm']}".strip(), f"Ledgerline: {h}")
    if cid in ("blankreq", "blankdate"):
        m = re.match(r"^(\w+) is blank$", x.get("why") or "")
        return (f"{m.group(1) if m else 'value'} is blank", f"{r['id'] or '(no ID)'} · (blank)",
                CLOSING_DATE if cid == "blankdate" else "(needs a value)")
    if cid == "depmissing":
        return (f"investor {r['id']} not on file", r["id"], "(not on file)")
    if cid in ("badchars", "collidegen", "idcollide"):
        m = re.match(r"^(?:Clean the ID to|Use|Make this one) (\S+)", s["t"])
        lab = {"badchars": "characters outside letters, digits and _", "idcollide": "ID shared after cutting"}
        return (lab.get(cid, "first word shared"), r["id"] or r.get("hit") or r["nm"], m.group(1) if m else short)
    if cid == "fuzzy":
        return ("name nearly matches", r["nm"], (r.get("hit") or "").split(" · ")[-1])
    return (x.get("why") or cid, x["name"], short)


def _holds(cid: str, x: dict[str, Any]) -> bool:
    """Whether the condition under which the cause's option is recommended holds for this row (by code)."""
    if "l" in x:
        return True
    if cid == "idlong":
        return str(x["s"].get("c") or "").startswith("rule|")
    if cid in _DUP_CAUSES:
        return _norm(x["r"]["nm"]) == _norm((x["r"].get("hit") or "").split(" · ")[-1])
    return True


def _items(c: Any, j: dict[str, Any]) -> list[dict[str, Any]]:
    """The open items of a job: one per row (or GL line) that needs a person, with the worker's proposal."""
    if RF.is_gl(j["type"]):
        out = V.gl_items(c, j)
        for x in out:
            x["i"] = x["row"]
        return out
    out = []
    for r in CE.static_rows(c, j):
        if r["st"] not in ("err", "warn"):
            continue
        s = V.sugg(j, r)
        if s is None:
            if r["st"] != "err":
                continue  # a warning with nothing to decide is noted, not asked
            s = {"t": "You decide", "c": None, "d": (r["why"] or "").split(":")[0]}
        am = re.match(r"^([\d,]+) ·", r["x"] or "")
        out.append({"name": r["nm"] or "(no name)", "id": r["id"] or "(no ID)", "why": r["why"], "s": s,
                    "err": r["st"] == "err", "r": r, "row": r["row"], "i": r["i"],
                    "amt": int(am.group(1).replace(",", "")) if am else 0})
    return out


def _gkey(x: dict[str, Any], cid: str) -> str:
    return f"{x['s'].get('d') or x['s']['t']}|{x['err']}|{cid}|{_holds(cid, x)}"


def _gid(cid: str, key: str) -> str:
    return f"{cid}-{hashlib.sha1(key.encode()).hexdigest()[:6]}"


def _units(j: dict[str, Any]) -> str:
    return "lines" if RF.is_gl(j["type"]) else "rows"


def _opts(cz: Cause, n: int, items: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [o for o in cz.options if not (o[0] == "edit" and n > 1 and not cz.edit_many)
            and not (o[0] == "fix" and any(not x["s"].get("c") for x in items) and cz.id not in ("gljr",))]


_GROUPED: dict[str, tuple[Any, dict[str, list[dict[str, Any]]]]] = {}


def _grouped(c: Any, j: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The open items by group, each group in source-row order. Kept for as long as the job's evaluation is the same
    object, so paging a group costs a slice, not a pass over the rows."""
    ev = CE.evaluation(c, j)
    hit = _GROUPED.get(str(j["id"]))
    if hit is not None and hit[0] is ev:
        return hit[1]
    gm: dict[str, list[dict[str, Any]]] = {}
    for x in _items(c, j):
        x["cid"] = cause_of(x)
        gm.setdefault(_gid(x["cid"], _gkey(x, x["cid"])), []).append(x)
    for items in gm.values():
        items.sort(key=lambda y: y["row"])
    _GROUPED[str(j["id"])] = (ev, gm)
    return gm


def _ent(j: dict[str, Any], x: dict[str, Any]) -> str:
    return V.row_ents(j, x["r"])[0] if "r" in x else f"Journal {x['l'].j}"


# ---- decisions kept on the job -----------------------------------------------------------------------------
def _hist(j: dict[str, Any]) -> list[dict[str, Any]]:
    return j.setdefault("hist", [])


def _pack(rows: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for n in sorted(set(rows)):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out


def _unpack(ranges: list[list[int]]) -> list[int]:
    return [n for a, b in ranges for n in range(a, b + 1)]


def held_count(c: Any, j: dict[str, Any]) -> int:
    """Rows (or lines) parked until the client answers: left out of the upload file, and the job cannot be approved
    while any is held."""
    if RF.is_gl(j["type"]):
        return sum(1 for x in CE.gl_eval(c, j)["lines"] if x["st"] == "skip" and any("on hold" in m for m in x["msgs"]))
    return sum(1 for r in CE.static_rows(c, j) if r["st"] == "skip" and "on hold" in (r["res"] or ""))


def verdict(c: Any, j: dict[str, Any]) -> dict[str, Any]:
    """N, ready, blocked, on hold and left out: by construction they add up to N."""
    t = CE.tally(c, j)
    n, blocked = int(t["read"]), int(t["err"])
    hold = held_count(c, j)
    out = int(t["skip"]) - hold
    ready = n - blocked - hold - out
    hist = [h for h in _hist(j) if not h["rev"] and h["state"] in ("fix", "out", "acc")]
    open_n = sum(len(v) for v in _grouped(c, j).values())
    resolved = sum(h["rows"] for h in hist)
    return {"N": n, "ready": ready, "blocked": blocked, "hold": hold, "out": out, "sum": ready + blocked + hold + out,
            "units": _units(j), "resolved": resolved, "total": resolved + open_n}


def _state_row(g: dict[str, Any]) -> int:
    return (0 if g["state"] == "open" else 1 if g["state"] == "hold" else 2) * 10 + {"block": 0, "check": 1, "info": 2}[g["impact"]]


def _all_groups(c: Any, j: dict[str, Any]) -> list[dict[str, Any]]:
    """Every group with its exact count, counted once per evaluation and stored with the job (`issue_groups`), so the
    table and the verdict bar read the stored counts instead of counting on each request."""
    ev = CE.evaluation(c, j)
    hist = _hist(j)
    key = f"{ev.manifest.eval_key}|{len(hist)}|{sum(1 for h in hist if h['rev'])}"
    hit = j.get("issue_groups")
    if hit and hit.get("key") == key:
        return hit["groups"]  # type: ignore[no-any-return]
    got = _build_groups(c, j)
    j["issue_groups"] = {"key": key, "groups": got}
    return got


def refresh(c: Any, j: dict[str, Any]) -> None:
    """Count the groups now (at check time) and keep the counts with the job."""
    _all_groups(c, j)


def _hold_history(j: dict[str, Any], rows: list[int]) -> list[dict[str, Any]]:
    """The holds these rows were released from: parked by, when, why; released by, when, why."""
    freed = _released(j)
    ids = sorted({h for r in rows for h in freed.get(r, [])})
    return [{"id": h["id"], "parked_by": h["who"], "parked_at": h.get("at", ""), "why": h.get("note", ""),
             "released_by": h["released"]["by"], "released_at": h["released"]["at"], "note": h["released"]["note"]}
            for h in _hist(j) if h["id"] in ids]


def _build_groups(c: Any, j: dict[str, Any]) -> list[dict[str, Any]]:
    cz = causes()
    sheet = CE.sheet_facts(j)[0]
    groups: list[dict[str, Any]] = []
    for gid, items in _grouped(c, j).items():
        x = items[0]
        cause = cz[x["cid"]]
        pats: dict[str, list[Any]] = {}
        ents: Counter[str] = Counter()
        for y in items:
            lab, b, a = pattern_of(cause.id, y)
            pats.setdefault(lab, [lab, b, a, 0])[3] += 1
            ents[_ent(j, y)] += 1
        top = sorted(pats.values(), key=lambda p: -p[3])
        opts = _opts(cause, len(items), items)
        rec = cause.recommended if cause.recommended in dict(opts) and all(_holds(cause.id, y) for y in items) \
            and x["err"] else ""
        rows = sorted(y["row"] for y in items)
        groups.append({
            "id": gid, "cause": cause.id, "label": cause.label, "family": cause.family, "consequence": cause.consequence,
            "impact": cause.impact if x["err"] else "check", "state": "open", "n": len(items),
            "pattern": top[0][0], "before": top[0][1], "after": top[0][2], "patterns": len(top),
            "top": top[:5], "other": len(items) - sum(p[3] for p in top[:5]),
            "options": [{"id": k, "label": v} for k, v in opts], "recommended": rec,
            "recommended_when": cause.when, "entities": [list(e) for e in ents.most_common()],
            "source": {"file": j["up"]["file"], "sheet": sheet, "first": rows[0], "last": rows[-1]},
            "amount": sum(y.get("amt", 0) for y in items), "by": "", "undo": 0,
            "hold_history": _hold_history(j, rows),
        })
    for h in _hist(j):
        if h["rev"]:
            continue
        cause = cz.get(h["cause"], cz["other"])
        groups.append({
            "id": f"H{h['id']}", "cause": h["cause"], "label": cause.label, "family": cause.family,
            "consequence": cause.consequence, "impact": cause.impact, "state": h["state"], "n": h["rows"],
            "pattern": h["pat"][0], "before": h["pat"][1], "after": h["pat"][2], "patterns": 1, "top": [h["pat"] + [h["rows"]]],
            "other": 0, "options": [], "recommended": "", "recommended_when": cause.when, "entities": [],
            "source": {"file": j["up"]["file"], "sheet": sheet, "first": h["first"], "last": h["last"]},
            "amount": 0, "by": h["who"], "when": h["t"], "undo": h["id"] if not h.get("irreversible") else 0,
            "irreversible": h.get("irreversible", ""), "decision": h["lab"], "rule": h["rule"],
            "batch": h["batch"], "release": h["opt"] == "park", "note": h.get("note", ""), "at": h.get("at", "")})
    groups.sort(key=lambda g: (_state_row(g), -g["n"]))
    return groups


_CHIP = {"need": ("open",), "hold": ("hold",), "done": ("fix", "out")}


def model(c: Any, j: dict[str, Any], *, page: int = 0, per: int = DEFAULT_PAGE, status: str = "",
          q: str = "") -> dict[str, Any]:
    """One page of the groups (exact counts, no rows), the verdict, and where the next decision is. `status` is a
    chip: need, hold or done; `q` searches the cause and the pattern."""
    per = max(1, min(int(per), PAGE_MAX))
    every = _all_groups(c, j)
    shown = [g for g in every if not status or g["state"] in _CHIP.get(status, ())]
    needle = q.strip().lower()
    if needle:
        shown = [g for g in shown if needle in f"{g['label']} {g['pattern']} {g['before']} {g['after']}".lower()]
    pages = max(1, -(-len(shown) // per))
    p = min(max(0, int(page)), pages - 1)
    v = verdict(c, j)
    count = {k: sum(1 for g in every if g["state"] in st) for k, st in _CHIP.items()}
    return {"units": v["units"], "verdict": v, "groups": shown[p * per:(p + 1) * per], "total": len(shown),
            "page": p, "pages": pages, "per": per, "chips": count,
            "next": next((g["id"] for g in every if g["state"] == "open"), None), "history": history(j),
            "can_write": bool(v["blocked"] == 0 and not j.get("failed")), "approved": bool(j.get("approved"))}


# ---- one group's rows, paged ---------------------------------------------------------------------------------
def _find(c: Any, j: dict[str, Any], gid: str) -> tuple[str, list[dict[str, Any]]]:
    if gid.startswith("H"):
        h = next((x for x in _hist(j) if f"H{x['id']}" == gid and not x["rev"]), None)
        if h is None:
            raise IssueError("That group is not in this view any more.")
        want = set(_unpack(h["ranges"]))
        if RF.is_gl(j["type"]):
            lines = CE.gl_eval(c, j)["lines"]
            return gid, [{"row": x["row"], "i": x["row"], "name": f"{x['l'].j} line {x['l'].ln}",
                          "id": f"{x['l'].acct} {x['l'].an}", "l": x["l"], "cid": h["cause"], "err": False,
                          "s": {"t": h["lab"]}} for x in lines if x["row"] in want]
        return gid, [{"row": r["row"], "i": r["i"], "name": r["nm"] or "(no name)", "id": r["id"], "r": r,
                      "cid": h["cause"], "err": False, "s": {"t": h["lab"]}, "why": r["why"]}
                     for r in CE.static_rows(c, j) if r["row"] in want]
    got = _grouped(c, j).get(gid)
    if got is None:
        raise IssueError("That group is not in this view any more.")
    return gid, got


def rows_page(c: Any, j: dict[str, Any], gid: str, *, page: int = 0, per: int = DEFAULT_PAGE, q: str = "",
              after: int = 0, desc: bool = False) -> dict[str, Any]:
    """One page of a group's rows in source-row order, with the exact total. `after` is a keyset cursor (the last row
    number the person has seen); `page` an offset page. A filter `q` scans the group; without it a page is a slice."""
    per = max(1, min(int(per), PAGE_MAX))
    _g, items = _find(c, j, gid)
    needle = q.strip().lower()
    if needle:
        items = [x for x in items if needle in f"{x['id']} {x['name']} {_ent(j, x)}".lower()]
    total = len(items)
    row = lambda x: x["row"]
    if desc:  # last page first: walk the ascending list from its end
        end = bisect.bisect_left(items, after, key=row) if after else total - min(max(0, int(page)), total // per) * per
        end = max(0, min(end, total))
        chunk = items[max(0, end - per):end][::-1]
        start = total - end
    else:
        start = bisect.bisect_right(items, after, key=row) if after else             min(max(0, int(page)), max(0, -(-total // per) - 1)) * per
        chunk = items[start:start + per]
    out = []
    for x in chunk:
        lab, b, a = ("", x["id"], x["s"]["t"]) if gid.startswith("H") else pattern_of(x["cid"], x)
        out.append({"row": x["row"], "before": b, "name": x["name"], "entity": _ent(j, x), "after": a, "pattern": lab})
    one: list[dict[str, str]] = []  # what a single row of this group can be given, from the cause's own options
    if items and not gid.startswith("H"):
        cz = causes()[items[0]["cid"]]
        by_account = "l" in items[0] and cz.id in ("glacct", "gldeal")  # that value is the account's, not one line's
        one = [{"id": i, "label": lab} for i, lab in _opts(cz, 1, items[:1])
               if i in ("edit", "out", "park", "fix") and not (by_account and i == "edit")]
    path = (j.get("up") or {}).get("path")
    columns = [] if not path or RF.is_gl(str(j["type"])) or gid.startswith("H") else [
        h for h in EC.read_static_records(Path(str(path)))[1] if h and "|" not in h]  # any of these cells can be edited
    return {"total": total, "start": start + 1 if chunk else 0, "row_options": one, "columns": columns, "end": start + len(chunk), "per": per,
            "page": start // per, "pages": max(1, -(-total // per)), "rows": out,
            "next_after": chunk[-1]["row"] if start + len(chunk) < total and chunk else 0}


def detail(c: Any, j: dict[str, Any], gid: str) -> dict[str, Any]:
    """Where a group is in the file and how to find it again in Excel: asked for, never in the collapsed view."""
    _g, items = _find(c, j, gid)
    open_group = not gid.startswith("H")
    cz = causes()[items[0]["cid"]] if items else causes()["other"]
    out: dict[str, Any] = {"why": cz.consequence, "recommended_when": cz.when}
    if items and open_group:
        out["locator"] = V.src_loc(j, items)
        out["excel"] = V.xl_how(j, items, _units(j))
    return out


# ---- the source sheet, a window of it, from the Parquet cache ---------------------------------------------------
def source_window(c: Any, j: dict[str, Any], row: int = 1, count: int = 12) -> dict[str, Any]:
    """`count` rows of the job's own sheet starting at sheet row `row`, and the header. Read from the cached Parquet
    parts (skipping whole parts by their row counts), so a window at row 900,000 costs one part, not the file."""
    path = (j.get("up") or {}).get("path")
    if not path or not Path(path).is_file():
        raise IssueError("I can't find the source file.")
    sheet = columnar.first_sheet(path)
    count = max(1, min(int(count), WINDOW_MAX))
    start = max(1, int(row))
    parts = columnar.parquet_parts(path, sheet)
    seen, total = 0, 0
    sizes = [pq.ParquetFile(p).metadata.num_rows for p in parts]
    total = sum(sizes)
    header: list[Any] = []
    rows: list[dict[str, Any]] = []

    def part_rows(k: int) -> list[list[Any]]:
        pf = pq.ParquetFile(parts[k])
        tagged = [f.metadata is not None and f.metadata.get(columnar._TAGGED) == b"1" for f in pf.schema_arrow]
        cols = [col.to_pylist() for col in pf.read().columns]
        for i, t in enumerate(tagged):
            if t:
                cols[i] = [columnar._untag(v) for v in cols[i]]
        return [list(r) for r in zip(*cols)] if cols else []

    for k, size in enumerate(sizes):
        if seen + size >= 1 and seen < 1 <= seen + size:
            header = part_rows(k)[0]
        if seen + size >= start and len(rows) < count:
            for n, cells in enumerate(part_rows(k), start=seen + 1):
                if n >= start and len(rows) < count:
                    rows.append({"row": n, "cells": [_text(v) for v in cells]})
        seen += size
        if len(rows) >= count and header:
            break
    width = max([len(header)] + [len(r["cells"]) for r in rows]) if (rows or header) else 0
    return {"file": j["up"]["file"], "sheet": sheet, "total": total, "start": start, "count": count,
            "header": [_text(v) for v in header], "letters": [EC.col_letter(i + 1) for i in range(width)],
            "rows": rows}


def _text(v: Any) -> str:
    s = "" if v is None else str(v)
    return s if len(s) <= 80 else s[:79] + "…"


# ---- deciding ---------------------------------------------------------------------------------------------------
def _person(who: str) -> str:
    from worker.console_questions import _person as person

    try:
        return person(who)
    except Exception as exc:  # the console's own error carries the sentence
        raise IssueError(str(exc)) from exc


def _commands(j: dict[str, Any], cz: Cause, items: list[dict[str, Any]], opt: str, val: str, column: str = "") -> list[str]:
    if opt == "cell":
        return [f"cell|{items[0]['i']}|{column}|{val}"]
    gl = "l" in items[0]
    ids = list(dict.fromkeys(x["l"].j if gl else str(x["i"]) for x in items))
    lab = cz.label.lower()
    if opt == "fix":
        return list(dict.fromkeys(x["s"]["c"] for x in items if x["s"].get("c")))
    if opt == "out":
        return [f"dec|{i}|exclude: {dict(cz.options)['out']} ({lab})" for i in ids]
    if opt == "park":  # a flag and an optional note: no question is drafted, nothing else happens
        note = val.replace("|", "").strip()
        return [f"dec|{i}|exclude: on hold{': ' + note if note else ''} ({lab})" for i in ids]
    if opt == "edit":
        v = val.replace("|", "").strip()
        if not v:
            return []
        fd = re.match(r"^(\w+) is blank$", items[0].get("why") or "")
        if cz.id == "blankdate":
            return [f"datev|{i}|{v}" for i in ids]
        if fd and cz.id == "blankreq" and not gl:
            return [f"fill|{i}|{fd.group(1)}|{v}" for i in ids]
        if cz.id == "glacct":
            return [f"acctset|{items[0]['l'].acct}|{v}"]
        if cz.id == "gldeal":
            return [f"dealv|{a}|{v}" for a in dict.fromkeys(x["l"].acct for x in items)]
        if cz.id in _ID_CAUSES:
            return [f"dec|{i}|corrected ID to {v}" for i in ids]
    return []


def _note(text: str, what: str) -> str:
    """A free-text note, required: trimmed, never empty, bounded. It is data: nothing reads it as an instruction."""
    n = " ".join(str(text or "").split()).replace("|", "/")
    if not n:
        raise IssueError(f"{what} needs a note: give a reason, in a few words.")
    if len(n) > NOTE_MAX:
        raise IssueError(f"The note is {len(n)} characters; the limit is {NOTE_MAX}.")
    return n


def _released(j: dict[str, Any]) -> dict[int, list[int]]:
    """source row -> ids of the holds it was released from (so a later decision on it needs a note)."""
    out: dict[int, list[int]] = {}
    for h in _hist(j):
        if h["opt"] == "park" and h["rev"] and h.get("released"):
            for r in _unpack(h["ranges"]):
                out.setdefault(r, []).append(h["id"])
    return out


def _row_numbers(given: Any) -> set[int]:
    try:
        rows = {int(x) for x in given}
    except (TypeError, ValueError) as exc:
        raise IssueError("Rows must be a list of row numbers.") from exc
    if not rows or len(rows) > ROWS_PER_DECISION:
        raise IssueError(f"name between 1 and {ROWS_PER_DECISION:,} rows")
    return rows


def _cell_column(j: dict[str, Any], column: str) -> str:
    """The file's own header a cell edit is for. A cell is a record file's: GL lines are decided by account, deal or journal."""
    if RF.is_gl(str(j["type"])):
        raise IssueError("Cell edits are for record files. For a GL line, decide its account, deal or journal.")
    header = EC.read_static_records(Path(str(j["up"]["path"])))[1]
    if column not in header or "|" in column:
        raise IssueError(f"“{column}” is not a column I can edit in this file.")
    return column


def _cell_now(j: dict[str, Any], i: Any, column: str) -> str:
    """The cell as the job reads it now: the person's correction if there is one, else the file's own cell."""
    got = (j.get("cells") or {}).get(f"{i}|{column}")
    if got is not None:
        return str(got)
    rec = EC.read_static_records(Path(str(j["up"]["path"])))[2][int(i) - 1]
    return "" if rec["v"].get(column) is None else str(rec["v"][column])


def _choose(c: Any, j: dict[str, Any], pick: dict[str, Any]) -> tuple[dict[str, Any], Cause, list[dict[str, Any]], str, str]:
    gid, opt, val = str(pick.get("id", "")), str(pick.get("option", "")), str(pick.get("value") or "")
    if gid.startswith("H"):
        raise IssueError("That group is already decided.")
    _g, items = _find(c, j, gid)
    cz = causes()[items[0]["cid"]]
    if pick.get("rows") not in (None, []):  # decide these rows of the group only, not the whole group
        want = _row_numbers(pick["rows"])
        items = [x for x in items if x["row"] in want]
        if len(items) != len(want):
            raise IssueError("Some of those rows are not open in this group any more.")
        if "l" in items[0] and opt == "edit" and cz.id in ("glacct", "gldeal"):
            raise IssueError("That value belongs to the account, so it applies to every line of the account: decide the group.")
    if opt == "cell":  # any cell of one row, whatever the cause: the group offers it for every row
        if len(items) != 1:
            raise IssueError("A cell edit is for one row at a time: name the row.")
        if not val.strip():
            raise IssueError("Type the value to use first.")
        return {"id": gid, "column": _cell_column(j, str(pick.get("column") or ""))}, cz, items, opt, val.strip()
    ok = dict(_opts(cz, len(items), items))
    if opt not in ok:
        raise IssueError(f"“{opt}” is not an option for {cz.label.lower()}")
    if opt == "edit" and not val.strip():
        raise IssueError("Type the value to use first.")
    if opt == "park":
        val = _note(val, "Putting rows on hold")
    return {"id": gid}, cz, items, opt, val


def preview(c: Any, j: dict[str, Any], picks: list[dict[str, Any]]) -> dict[str, Any]:
    """What would change, in exact counts and on a sample of rows. Nothing is written and nothing is audited."""
    out = []
    for p in picks:
        g, cz, items, opt, val = _choose(c, j, p)
        column = g.get("column", "")
        cmds = _commands(j, cz, items, opt, val, column)
        opt_label = f"Edit {column}" if opt == "cell" else dict(cz.options)[opt]
        sample = sorted(items, key=lambda x: x["row"])[:3]
        rows = []
        for x in sample:
            _l, b, a = pattern_of(cz.id, x)
            after = {"out": "(left out of the upload file)", "park": "(on hold, not written)"}.get(
                opt, val if opt in ("edit", "cell") else a)
            rows.append({"row": x["row"], "before": _cell_now(j, x["i"], column) if opt == "cell" else b, "after": after})
        open_after = len(items) if not cmds else 0
        out.append({"id": g["id"], "cause": cz.label, "option": opt, "label": opt_label, "n": len(items),
                    "rows": rows, "stay_open": open_after,
                    "irreversible": any(m.startswith(("qa|", "quick|")) for m in cmds)})
    return {"groups": out, "n": sum(g["n"] for g in out), "units": _units(j)}


def _put(j: dict[str, Any], slot: str, key: str, value: Any, led: dict[str, Any]) -> None:
    j.setdefault(slot, {})[key] = value
    led.setdefault(slot, []).append(key)


def _run(c: Any, j: dict[str, Any], cmds: list[str], led: dict[str, Any]) -> str:
    """Apply commands to the job's own choices without evaluating between them. Returns a reason it cannot be
    undone, or ''."""
    irrev = ""
    people: list[dict[str, str]] = []
    refs: dict[str, list[dict[str, str]]] = {}
    for cmd in cmds:
        a = cmd.split("|")
        k = a[0]
        if k == "dec":
            _put(j, "dec", a[1], a[2], led)
        elif k == "date":
            _put(j, "dates", a[1], CLOSING_DATE, led)
        elif k == "datev":
            _put(j, "dates", a[1], a[2], led)
        elif k == "fill":
            _put(j, "fill", f"{a[1]}|{a[2]}", "|".join(a[3:]), led)
        elif k == "cell":  # cell|<record>|<column>|<value>: the person's correction of one source cell
            _put(j, "cells", f"{a[1]}|{a[2]}", "|".join(a[3:]), led)
        elif k == "dealv":
            _put(j, "dealfill", a[1], a[2], led)
        elif k == "acctset":
            _put(j, "acct", a[1], a[2], led)
        elif k == "fz":
            _put(j, "qa", f"f{a[1]}", 1, led)
        elif k == "rule":
            led["rule"] = j["rules"]["id"]
            j["rules"]["id"] = a[1]
            j["logR"].append({"scope": "job", "text": "Drop long ID prefixes"})
        elif k == "qa":
            _put(j, "qa", a[1], 1, led)
            people.append({"key": a[2], "name": a[3], "label": "a quick entry by you"})
            irrev = "added to the sponsor's existing records, which stay"
        elif k == "quick":
            _put(j, "qa", f"{a[1]}{a[2]}", 1, led)
            refs.setdefault(a[1], []).append({"key": a[2], "name": a[2], "label": "a quick entry by you"})
            irrev = "added to the sponsor's existing records, which stay"
        else:
            raise IssueError(f"unknown change {cmd!r}")
    if people:
        c.store_add(j["sp"], "Investors", people, "quick entry", j["id"])
    for kind, recs in refs.items():
        c.store_add(j["sp"], kind, recs, "quick entry", j["id"])
    return irrev


def _revert(j: dict[str, Any], led: dict[str, Any]) -> None:
    for slot in CHOICE_SLOTS:
        for key in led.get(slot, []):
            j.get(slot, {}).pop(key, None)
            j.get("by", {}).pop(f"{slot}|{key}", None)
    if "rule" in led:
        j["rules"]["id"] = led["rule"]
        j["logR"] = [r for r in j["logR"] if r.get("text") != "Drop long ID prefixes"]


def apply(c: Any, j: dict[str, Any], picks: list[dict[str, Any]], who: str, *, rule: bool = False,
          note: str = "") -> dict[str, Any]:
    """Decide groups. The reviewer is a named person. Each decision is written to the job's audit log first; if that
    fails nothing changes. One evaluation at the end."""
    person = _person(who)
    if j.get("approved"):
        raise IssueError("This job is approved, so it is final. Nothing changed.")
    if not picks:
        raise IssueError("Choose a group first.")
    chosen = [_choose(c, j, p) for p in picks]
    taken = [(g["id"], x["row"]) for g, _cz, items, *_ in chosen for x in items]  # parts of one group may be decided together
    if len(set(taken)) != len(taken):
        raise IssueError("A row can't be decided twice in one request.")
    units = _units(j)
    hid = max([h["id"] for h in _hist(j)] + [0])
    batch = max([h["batch"] for h in _hist(j)] + [0]) + 1
    entries = []
    plan = []
    freed = _released(j)
    note = " ".join(str(note or "").split()).replace("|", "/")
    if len(note) > NOTE_MAX:
        raise IssueError(f"The note is {len(note)} characters; the limit is {NOTE_MAX}.")
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for k, (g, cz, items, opt, val) in enumerate(chosen):
        back = sorted({h for x in items for h in freed.get(x["row"], [])})
        if back and opt != "park":
            note = _note(note, "Deciding rows that were on hold")
        column = g.get("column", "")
        cmds = _commands(j, cz, items, opt, val, column)
        if not cmds:
            raise IssueError(f"“{dict(cz.options)[opt]}” has nothing to apply to {cz.label.lower()}")
        label = f"Edit {column}" if opt == "cell" else dict(cz.options)[opt]
        lab, b, a = pattern_of(cz.id, items[0])
        saved = bool(rule and opt == "fix")
        reason = (f"{person} chose “{label}” for {len(items):,} {units}: {cz.label}"
                  + (f"; saved as a rule for {j['sp']}" if saved else "")
                  + (f". Reason: {val}" if opt == "park" else f". Note: {note}" if note else ""))
        rows = sorted(x["row"] for x in items)
        changes = [{"row": x["row"], "before": _cell_now(j, x["i"], column) if opt == "cell" else pattern_of(cz.id, x)[1],
                    **({"column": column} if opt == "cell" else {}),
                    "after": val if opt in ("edit", "cell") else {"out": "(left out)", "park": "(on hold)"}.get(opt, pattern_of(cz.id, x)[2])}
                   for x in items[:CHANGES_LOGGED]]
        entries.append((reason, {"job": j["id"], "group": g["id"], "cause": cz.id, "option": opt, "rows": len(items),
                                 "units": units, "reviewer": person, "rule": saved, "batch": batch, "at": now,
                                 "note": val if opt == "park" else note, "from_hold": back, "first_rows": rows[:5],
                                 "changes": changes, "named_rows": bool(picks[k].get("rows")),
                                 **({"column": column} if opt == "cell" else {})},
                        {"batch": batch}))
        plan.append((hid + k + 1, g, cz, items, opt, val, cmds, label, [lab, b, a], rows, saved, back,
                     val if opt == "park" else note, changes))
    try:
        persist.job_audit(c, j).record_many("issues.decided", entries)
    except (AuditError, OSError, ValueError) as exc:
        raise IssueError(f"I couldn't record the decision, so nothing changed: {exc}") from exc
    when = time.strftime("%H:%M:%S")
    done = []
    for n, g, cz, items, opt, val, cmds, label, pat, rows, saved, back, why_note, changes in plan:
        led: dict[str, Any] = {}
        irrev = _run(c, j, cmds, led)
        decider = getattr(c, "actor", "") or person
        for slot in CHOICE_SLOTS:
            for k in led.get(slot, []):
                c.mark(j, slot, k, decider)
        if saved:
            for x in items:
                s = x["s"]
                if s.get("L") and not str(s.get("d", "")).startswith("Already in Ledgerline? · row"):
                    c.learn(j, s["L"], "sponsor")
        _hist(j).append({
            "id": n, "t": when, "who": person, "rev": False, "cause": cz.id, "opt": opt, "lab": label, "pat": pat,
            "rows": len(items), "state": _STATE[opt], "rule": saved, "batch": batch, "ledger": led,
            "ranges": _pack(rows), "first": rows[0], "last": rows[-1], "irreversible": irrev, "note": why_note,
            "at": now, "from_hold": back, "released": None, "changes": changes[:CHANGES_KEPT]})
        c.log(f"{label}: {len(items):,} {units} · {cz.label}",
              f"{person}; " + (f"saved as a rule for {j['sp']}" if saved else "this file only"))
        done.append({"id": n, "group": g["id"], "rows": len(items), "label": label})
    c.retally(j)
    return {"batch": batch, "decisions": done, "verdict": verdict(c, j)}


def undo(c: Any, j: dict[str, Any], who: str, *, decision: int = 0, batch: int = 0, reason: str = "") -> dict[str, Any]:
    """Revert one decision, or every decision of a batch. Audited first; a quick add to the sponsor's records stays
    and says so."""
    person = _person(who)
    if j.get("approved"):
        raise IssueError("This job is approved, so it is final. Nothing changed.")
    todo = [h for h in _hist(j) if not h["rev"] and (h["id"] == decision if decision else h["batch"] == batch)]
    if not todo:
        raise IssueError("There is nothing to undo there.")
    stuck = next((h for h in todo if h.get("irreversible")), None)
    if stuck:
        raise IssueError(f"That decision cannot be undone: it was {stuck['irreversible']}.")
    if any(h["opt"] == "park" for h in todo):
        why = _note(reason, "Releasing a hold")
    else:
        why = " ".join(str(reason or "").split()) or f"undone by {person}"
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        persist.job_audit(c, j).record_many("issues.reverted", [
            (f"{person} reverted “{h['lab']}” for {h['rows']:,} {_units(j)}: {why}",
             {"job": j["id"], "decision": h["id"], "cause": h["cause"], "rows": h["rows"], "reviewer": person,
              "batch": h["batch"], "at": now, "note": why, "release": h["opt"] == "park"}, None) for h in todo])
    except (AuditError, OSError, ValueError) as exc:
        raise IssueError(f"I couldn't record the undo, so nothing changed: {exc}") from exc
    for h in reversed(todo):
        _revert(j, h["ledger"])
        h["rev"] = True
        h["released"] = {"by": person, "at": now, "note": why}
        c.log(f"Reverted: {h['lab']} for {h['rows']:,} {_units(j)} · {causes().get(h['cause'], causes()['other']).label}",
              f"{person} undid a decision; the rows are open again")
    c.retally(j)
    return {"reverted": [h["id"] for h in todo], "verdict": verdict(c, j)}


def history(j: dict[str, Any]) -> list[dict[str, Any]]:
    cz = causes()
    return [{"id": h["id"], "when": h["t"], "who": h["who"], "label": h["lab"], "cause": cz.get(h["cause"], cz["other"]).label,
             "pattern": h["pat"][0], "rows": h["rows"], "rule": h["rule"], "reverted": h["rev"], "batch": h["batch"],
             "irreversible": h.get("irreversible", ""), "release": h["opt"] == "park", "note": h.get("note", ""),
             "at": h.get("at", ""), "released": h.get("released"), "from_hold": h.get("from_hold", []),
             "kind": "hold" if h["opt"] == "park" else "decision", "changes": h.get("changes", [])}
            for h in reversed(_hist(j))]


# ---- the chat: a sentence about a group becomes a proposal with one Apply -----------------------------------------
def _match(opts: list[tuple[str, str]], text: str) -> tuple[str, str, str] | None:
    """(option id, typed value, note) when the sentence names one of the group's options by its label from the
    table. After the label: a park's text is its reason; an edit's is its value, then `because` and a note; any other
    option's text is its note."""
    low = " ".join(text.lower().split())
    for oid, label in sorted(opts, key=lambda o: -len(o[1])):
        lab = label.lower()
        if low == lab or low.startswith((lab + ":", lab + " ")):
            rest = text.strip()[len(label):].lstrip(" :").strip()
            if oid == "park":
                return oid, rest, ""
            m = re.search(r"\bbecause\b", rest, re.IGNORECASE)
            head, tail = (rest[:m.start()], rest[m.end():]) if m else (rest, "")
            if oid == "edit":
                return oid, head.strip(" :,"), tail.strip(" :,")
            return oid, "", (tail or head).strip(" :,")
    return None


def _chat_msg(j: dict[str, Any], who: str, text: str, role: str = "Checker", options: list[dict[str, Any]] | None = None) -> None:
    for m in j["chat"]:  # an older proposal's button no longer applies once there is a newer message
        if any(str(o.get("action", "")).startswith(("issue_apply|", "issue_undo|", "issue_apply_prop|", "issue_undo_batch|"))
               for o in m.get("options", [])):
            m["options"] = []
    j["chat"].append({"who": who, "role": role, "text": text, "options": options or []})


def _release_reply(c: Any, j: dict[str, Any], gid: str, text: str) -> bool:
    h = next((x for x in _hist(j) if f"H{x['id']}" == gid and not x["rev"] and x["opt"] == "park"), None)
    if h is None or not re.match(r"^\s*release\b", text, re.IGNORECASE):
        return False
    j["chat"].append({"who": "you", "text": text.strip()})
    note = re.sub(r"^\s*release\b[\s:,-]*", "", text, flags=re.IGNORECASE).strip()
    try:
        note = _note(note, "Releasing a hold")
    except IssueError as exc:
        _chat_msg(j, "w", f"{exc} Type it after “Release:”. Nothing changed.")
        return True
    _chat_msg(j, "w", f"Release {h['rows']:,} {_units(j)} from the hold? They return to Open and are not written until "
              f"decided. Your note is recorded with your name: “{note}”.\nNothing changes until you press Release.",
              "Checker", [{"label": "Release", "action": f"issue_undo|{h['id']}|{note}", "primary": True}])
    return True


def chat_reply(c: Any, j: dict[str, Any], gid: str, text: str) -> bool:
    """A sentence typed while a group is open in the chat. When it names one of the group's options (a suggestion's
    text, edited or not) the worker answers with a preview and ONE Apply button; nothing changes yet. A held group
    answers to "Release: why". Returns False when the sentence is not about an option, so the ordinary rule proposals
    take it."""
    if j.get("approved"):
        return False
    if gid.startswith("H"):
        return _release_reply(c, j, gid, text)
    try:
        _g, items = _find(c, j, gid)
    except IssueError:
        return False
    cz = causes()[items[0]["cid"]]
    got = _match(_opts(cz, len(items), items), text)
    if got is None:
        return False
    opt, value, note = got
    j["chat"].append({"who": "you", "text": text.strip()})
    try:
        pv = preview(c, j, [{"id": gid, "option": opt, "value": value}])
        freed = _released(j)
        if opt != "park" and any(x["row"] in freed for x in items):
            note = _note(note, "Deciding rows that were on hold")
    except IssueError as exc:
        _chat_msg(j, "w", f"{exc} Nothing changed.", "Checker")
        return True
    x = pv["groups"][0]
    lines = [f"{x['label']}: {x['n']:,} {pv['units']} in “{cz.label}” will change."]
    lines += [f"  row {r['row']:,}: {r['before']} → {r['after']}" for r in x["rows"]]
    if x["stay_open"]:
        lines.append(f"{x['stay_open']:,} {pv['units']} cannot take this change and stay open.")
    if opt == "park":
        lines.append(f"They are held, not written. Your reason is recorded with your name: “{value}”.")
    elif note:
        lines.append(f"Your note is recorded with your name: “{note}”.")
    if opt == "fix":
        lines.append(f"Applying also saves this as a rule for {j['sp']}, so future files do the same.")
    if x["irreversible"]:
        lines.append("This adds records to the sponsor's existing records; an undo does not remove them.")
    lines.append("Nothing changes until you press Apply.")
    _chat_msg(j, "w", "\n".join(lines), "Checker",
              [{"label": "Apply", "action": f"issue_apply|{gid}|{opt}|{value}|{note}", "primary": True}])
    return True


def chat_apply(c: Any, j: dict[str, Any], gid: str, opt: str, value: str, who: str, note: str = "") -> None:
    got = apply(c, j, [{"id": gid, "option": opt, "value": value}], who, rule=opt == "fix", note=note)
    d = got["decisions"][0]
    v = got["verdict"]
    undoable = opt != "park" and not _hist(j)[-1].get("irreversible")
    tail = " To release it, open the group in the chat and say “Release:” with a reason." if opt == "park" else ""
    _chat_msg(j, "w", f"Done. {d['label']}: {d['rows']:,} {v['units']} · recorded under your name. "
              f"{v['blocked']:,} still need you.{tail}", "Checker",
              [{"label": "Undo", "action": f"issue_undo|{d['id']}"}] if undoable else [])


def chat_apply_many(c: Any, j: dict[str, Any], picks: list[dict[str, Any]], who: str, note: str = "") -> None:
    """Apply the decisions a sentence was read as (already previewed): one request, one batch, one Undo."""
    rule = any(p["option"] == "fix" and not p.get("rows") for p in picks)  # a suggestion for a whole group is a rule
    got = apply(c, j, picks, who, rule=rule, note=note)
    v = got["verdict"]
    irrev = any(h.get("irreversible") for h in _hist(j)[-len(got["decisions"]):])
    parts = "; ".join(f"{d['label']}: {d['rows']:,}" for d in got["decisions"])
    _chat_msg(j, "w", f"Done. {parts} {v['units']} · recorded under your name. {v['blocked']:,} still need you.", "Checker",
              [] if irrev or any(p["option"] == "park" for p in picks)
              else [{"label": "Undo", "action": f"issue_undo_batch|{got['batch']}"}])


def chat_undo_batch(c: Any, j: dict[str, Any], batch: int, who: str, note: str = "") -> None:
    got = undo(c, j, who, batch=batch, reason=note)
    _chat_msg(j, "w", f"Reverted. The rows are open again; {got['verdict']['blocked']:,} need you.", "Checker")


def chat_undo(c: Any, j: dict[str, Any], decision: int, who: str, note: str = "") -> None:
    got = undo(c, j, who, decision=decision, reason=note)
    _chat_msg(j, "w", f"Reverted. The rows are open again; {got['verdict']['blocked']:,} need you.",
              "Checker")
