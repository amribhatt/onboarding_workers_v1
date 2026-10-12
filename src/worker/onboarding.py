"""Onboarding sponsors and loading entities: the rows, their checks and how a list file is read.

A sponsor is an ID, a name and a go-live date. An entity is the same plus an optional fund group (ID and name).
All three are mandatory; a bulk row without a go-live date is kept so the person can set it, and blocks the save.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import openpyxl

from worker import engine_console as EC
from worker import rulefiles

_LISTS_FILE = Path(__file__).resolve().parents[2] / "templates" / "onboarding" / "synonyms.yaml"
_KEYS = {"id": "id", "name": "n", "go_live": "live", "group_id": "gid", "group_name": "gn"}  # file key -> row key
_HEADS: dict[str, dict[str, tuple[str, ...]]] = {}


def list_heads() -> dict[str, dict[str, tuple[str, ...]]]:
    """Header words per list: `{"Sponsors": {"id": (...), "n": (...), "live": (...)}, "Entities": {...}}`, from
    `templates/onboarding/synonyms.yaml`, strictly loaded (a refused file raises, nothing is half-read)."""
    if not _HEADS:
        got = rulefiles.load_file(_LISTS_FILE, repo_root=_LISTS_FILE.parents[2])
        for record, block in got.data["types"].items():
            _HEADS[record] = {_KEYS[k]: tuple(str(w) for w in words) for k, words in block.items()}
    return _HEADS


def _head(header: list[str], names: Iterable[str]) -> int | None:
    low = [h.strip().lower() for h in header]
    for n in names:
        if n in low:
            return low.index(n)
    return None


def _iso(v: Any) -> str:
    if v is None or v == "":
        return ""
    if hasattr(v, "date"):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    text = str(v).strip()
    return text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else ""


def read_list(path: str | Path, mode: str) -> list[dict[str, str]]:
    """Rows of a sponsor or entity list file (xlsx): ID, name, go-live date, and for entities the fund group."""
    heads = list_heads()["Sponsors" if mode == "new" else "Entities"]
    _sheet, header, rows = EC.read_sheet(path)
    at = {k: _head(header, names) for k, names in heads.items()}
    if at["id"] is None or at["n"] is None:
        want = "sponsor ID and name" if mode == "new" else "entity ID and name"
        raise ValueError(f"the list needs a column for the {want} (found: {', '.join(h for h in header if h) or 'none'})")
    out = []
    for _n, cells in rows:
        def cell(key: str, cells: list[Any] = cells) -> Any:
            k = at.get(key)
            return cells[k] if k is not None and k < len(cells) else None

        row = {"id": EC.cell_text(cell("id")), "n": EC.cell_text(cell("n")), "live": _iso(cell("live"))}
        if mode != "new":
            row |= {"gid": EC.cell_text(cell("gid")), "gn": EC.cell_text(cell("gn"))}
        if row["id"] or row["n"]:
            out.append(row)
    return out


def known(console: Any, mode: str, sponsor: str) -> list[dict[str, str]]:
    if mode == "new":
        return [{"id": s["id"], "n": s["n"]} for s in console.S["sponsors"]]
    return [{"id": console.S["EM"].get(n, {}).get("id", ""), "n": n} for names in console.S["ent"].values() for n in names]


def clash(known_rows: list[dict[str, str]], row: dict[str, str], in_list: list[dict[str, str]]) -> str:
    """Why a row cannot be added: its ID or name is already used, here or in this list. "" when it is fine."""
    def k(x: str) -> str:
        return str(x).strip().lower()

    for pool, where in ((known_rows, "onboarded"), (in_list, "in this list")):
        a = next((x for x in pool if x.get("id") and k(x["id"]) == k(row["id"])), None)
        if a:
            return f"ID {row['id']} is already used by {a['n']}"
        b = next((x for x in pool if k(x["n"]) == k(row["n"])), None)
        if b:
            return f"{row['n']} is already {where}"
    return ""


def check_groups(rows: list[dict[str, str]], have: list[dict[str, str]]) -> str:
    """A fund group has one ID and one name: a row may reuse a group's ID only with that group's name."""
    names: dict[str, str] = {}
    for r in have + rows:
        gid, gn = r.get("gid", ""), r.get("gn", "")
        if gn and not gid:
            return f"{r['n']}: a fund group needs its group ID as well as its name."
        if gid and not gn:
            if gid.lower() not in names:
                return f"{r['n']}: give fund group {gid} a name."
            r["gn"] = names[gid.lower()]
        if gid:
            first = names.setdefault(gid.lower(), r["gn"])
            if first != r["gn"]:
                return f"Fund group {gid} is already called {first}. Use that name or a new group ID."
    return ""


def sample_rows(console: Any, mode: str, sponsor: str) -> list[dict[str, str]]:
    """Rows of the bundled sample list: invented names, one without a go-live date to show the mandatory rule."""
    if mode == "new":
        return [{"id": "GP-006", "n": "Sponsor F", "live": "2027-03-01"},
                {"id": "GP-007", "n": "Sponsor G", "live": "2027-06-01"},
                {"id": "GP-008", "n": "Sponsor H", "live": ""}]
    prefix = re.sub(r"^Sponsor\s*", "", sponsor).strip()
    pre = prefix + " " if prefix else ""
    base = re.sub(r"\D", "", next((s["id"] for s in console.S["sponsors"] if s["n"] == sponsor), "") or "GP-000") + "90"
    if console.S["ent"].get(sponsor):
        return [{"id": base + "4", "n": pre + "Feeder I", "live": "", "gid": "", "gn": ""}]
    return [{"id": base + "1", "n": pre + "Fund I", "live": "2027-03-01", "gid": "FG-01", "gn": pre + "Main Funds"},
            {"id": base + "2", "n": pre + "Fund II", "live": "2027-06-01", "gid": "FG-01", "gn": pre + "Main Funds"},
            {"id": base + "3", "n": pre + "Co-Invest I", "live": "", "gid": "", "gn": ""}]


def write_sample(console: Any, mode: str, sponsor: str) -> Path:
    name = "Sponsor_List.xlsx" if mode == "new" else sponsor.replace(" ", "_") + "_Entity_List.xlsx"
    path = console.inbox / name
    book = openpyxl.Workbook()
    ws = book.active
    ws.append(["Sponsor ID", "Sponsor name", "Go-live date"] if mode == "new"
              else ["Entity ID", "Entity name", "Go-live date", "Fund group ID", "Fund group name"])
    for r in sample_rows(console, mode, sponsor):
        ws.append([r["id"], r["n"], r["live"] or None] + ([r.get("gid") or None, r.get("gn") or None] if mode != "new" else []))
    book.save(path)
    return path
