"""Replace a job's source file at any stage, as often as the person wants, and keep what they already decided.

The job's decisions are keyed by record number (the row's place in the old file). A new file moves, adds and removes
rows, so each decision follows its row by CONTENT: a row of the new file with the same cells as the old row takes the
decision over, at its new place. A row whose cells changed, or that is gone, is not guessed at: its decision is dropped
and the row is open again, and the report says which. Nothing is carried silently and nothing is invented.

The change is audited (`source.replaced`, old and new file hashes, what was kept and what was re-opened, who) BEFORE it
takes effect; if the record cannot be written the new file is removed and the job is as it was. An approved job never
changes. A GL job that already has issue decisions is refused (its decisions are tied to journal lines and their
places; undo them first): a GL job without decisions swaps the file freely.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from worker import console_engine as CE
from worker import console_issues as CI
from worker import engine_console as EC
from worker import persist
from worker import record_facts as RF
from worker.audit import AuditError
from worker.errors import StatusError

AUDITED = ("from", "to", "kept", "reopened", "reopened_listed", "column_choices_dropped", "reshape_dropped", "note")
SHOWN = ("at", "by", "from", "to", "kept", "reopened", "note")
LISTED = 20  # re-opened decisions named one by one in the report; the count is exact


class ReuploadError(StatusError):
    """A refusal, said in plain words; the web layer turns it into a 409."""


def _sig(rec: dict[str, Any]) -> str:
    return json.dumps(rec["v"], sort_keys=True, default=str, ensure_ascii=False)


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _moves(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> dict[int, int]:
    """old record number -> new record number, row by row, for rows whose cells are identical. A row repeated n times
    in both files is matched in order, so each old copy has its own new copy."""
    free: dict[str, deque[int]] = defaultdict(deque)
    for rec in new:
        free[_sig(rec)].append(int(rec["i"]))
    out: dict[int, int] = {}
    for rec in old:
        left = free.get(_sig(rec))
        if left:
            out[int(rec["i"])] = left.popleft()
    return out


def _decided(j: dict[str, Any]) -> dict[str, set[int]]:
    """The record numbers the job's own choices name, by slot."""
    return {
        "dec": {int(k) for k in j.get("dec", {}) if str(k).isdigit()},
        "dates": {int(k) for k in j.get("dates", {}) if str(k).isdigit()},
        "fill": {int(str(k).partition("|")[0]) for k in j.get("fill", {}) if str(k).partition("|")[0].isdigit()},
        "qa": {int(str(k)[1:]) for k in j.get("qa", {}) if str(k)[:1] == "f" and str(k)[1:].isdigit()},
    }


def _carry(j: dict[str, Any], moved: dict[int, int]) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """(the job's choices after the move, the decisions that lost their row, how many were kept)."""
    lost: list[dict[str, Any]] = []
    kept = 0
    out: dict[str, Any] = {"dec": {}, "dates": {}, "fill": {}, "qa": {}}
    for k, v in j.get("dec", {}).items():
        if not str(k).isdigit():
            out["dec"][k] = v  # a journal: named by its own id, not by a place
        elif int(k) in moved:
            out["dec"][str(moved[int(k)])] = v
            kept += 1
        else:
            lost.append({"slot": "decision", "was": int(k), "what": str(v)[:80]})
    for k, v in j.get("dates", {}).items():
        if int(k) in moved:
            out["dates"][str(moved[int(k)])] = v
            kept += 1
        else:
            lost.append({"slot": "commitment date", "was": int(k), "what": str(v)})
    for k, v in j.get("fill", {}).items():
        i, _, field = str(k).partition("|")
        if int(i) in moved:
            out["fill"][f"{moved[int(i)]}|{field}"] = v
            kept += 1
        else:
            lost.append({"slot": f"value for {field}", "was": int(i), "what": str(v)[:80]})
    for k, v in j.get("qa", {}).items():
        key = str(k)
        if key[:1] == "f" and key[1:].isdigit():  # a fuzzy match confirmed for a row
            if int(key[1:]) in moved:
                out["qa"][f"f{moved[int(key[1:])]}"] = v
                kept += 1
            else:
                lost.append({"slot": "confirmed match", "was": int(key[1:]), "what": ""})
        else:
            out["qa"][key] = v  # a quick entry to the sponsor's records: named by its value, not a place
    return out, lost, kept


def _remap_history(j: dict[str, Any], moved: dict[int, int], row_of: Callable[[int], int], person: str, when: str) -> int:
    """Follow every open issue decision to its new rows; one whose rows are all gone is reverted with the reason.
    Returns how many were reverted."""
    gone = 0
    for h in CI._hist(j):
        if h["rev"]:
            continue
        led = h.get("ledger", {})
        new = {slot: [] for slot in led}
        rows: list[int] = []
        for slot, keys in led.items():
            if slot == "rule":
                new[slot] = keys
                continue
            for key in keys:
                head = str(key).partition("|")[0]
                if slot in ("dec", "dates", "fill") and head.isdigit():
                    if int(head) in moved:
                        n = moved[int(head)]
                        new[slot].append(str(key).replace(head, str(n), 1) if slot == "fill" else str(n))
                        rows.append(row_of(n))
                elif slot == "qa" and head[:1] == "f" and head[1:].isdigit():
                    if int(head[1:]) in moved:
                        new[slot].append(f"f{moved[int(head[1:])]}")
                        rows.append(row_of(moved[int(head[1:])]))
                else:
                    new[slot].append(key)  # named by its own value, not by a place
        h["ledger"] = new
        if rows:
            h["rows"], h["ranges"] = len(set(rows)), CI._pack(sorted(set(rows)))
            h["first"], h["last"] = min(rows), max(rows)
        elif not any(v for s, v in new.items() if s != "rule" and v):
            h["rev"] = True
            h["released"] = {"by": person, "at": when, "note": "the source file was replaced and this row is not in it"}
            gone += 1
    return gone


def replace(c: Any, j: dict[str, Any], name: str, data: bytes, who: str, note: str = "") -> dict[str, Any]:
    """Make `data` the job's source file. Returns the report the page shows."""
    person = CI._person(who)
    if j.get("approved"):
        raise ReuploadError("this job is approved; finished work never changes")
    gl = RF.is_gl(str(j["type"]))
    if gl and any(not h["rev"] for h in CI._hist(j)):
        raise ReuploadError("this GL job already has issue decisions, which belong to its journal lines: undo them on the "
                            "Activity tab first, then replace the file")
    note = " ".join(str(note or "").split())[:CI.NOTE_MAX]
    c.check_upload(name, data)
    old_path = Path(str(j["up"]["path"]))
    old_bytes = old_path.read_bytes() if old_path.exists() else b""
    if old_bytes == data:
        raise ReuploadError("that is the file this job already reads")
    path, sheet, header, records, fresh = c.store_upload(name, data, static=not gl)

    moved: dict[int, int] = {}
    lost: list[dict[str, Any]] = []
    kept = 0
    carried: dict[str, Any] = {}
    old_records: list[dict[str, Any]] = []
    if not gl:
        try:
            old_records = EC.read_static_records(old_path)[2] if old_path.exists() else []
        except (OSError, ValueError, KeyError, RuntimeError):  # the old file cannot be read any more: nothing can be matched, and the report says so
            old_records = []
        moved = _moves(old_records, records)
        carried, lost, kept = _carry(j, moved)
    keep_map = {f: h for f, h in (j.get("map") or {}).items() if h in header}
    dropped_map = {f: h for f, h in (j.get("map") or {}).items() if h not in header}
    report = {
        "from": {"file": j["up"].get("file", ""), "sha256": _sha(old_bytes), "rows": len(old_records)},
        "to": {"file": path.name, "sha256": _sha(data), "sheet": sheet, "rows": len(records)},
        "kept": kept, "reopened": len(lost), "reopened_listed": lost[:LISTED],
        "column_choices_dropped": dropped_map, "reshape_dropped": bool((j["up"].get("derived") or {}).get("table_sha256")),
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "by": person, "note": note,
    }
    reason = (f"{person} replaced the source file {report['from']['file'] or '(none)'} with {path.name}: "
              f"{kept} decisions kept, {len(lost)} re-opened" + (f". Note: {note}" if note else ""))
    try:  # recorded first: if it cannot be, nothing changes and the new file is not kept
        persist.job_audit(c, j).record("source.replaced", reason, {"job": j["id"], "reviewer": person,
                                                                    **{k: report[k] for k in AUDITED}}, {})
    except (AuditError, OSError, ValueError) as exc:
        if fresh:
            path.unlink(missing_ok=True)
        raise ReuploadError(f"Nothing was changed, because the replacement could not be recorded: {exc}") from exc

    def row_of(n: int) -> int:  # the new file's records are numbered 1, 2, 3 ... in sheet order
        return int(records[n - 1]["row"])

    if not gl:
        j["dec"], j["dates"], j["fill"], j["qa"] = carried["dec"], carried["dates"], carried["fill"], carried["qa"]
        _remap_history(j, moved, row_of, person, report["at"])
    j["map"] = keep_map
    up = j["up"]
    up.update({"file": path.name, "path": str(path), "size": len(data), "sheet": sheet, "own": True, "stage": "ready"})
    up.pop("derived", None)
    j.setdefault("replaced", []).append({k: report[k] for k in SHOWN})
    c.log(f"Replaced the source file with {path.name}", reason)
    j["chat"].append({"who": "w", "role": "Checker", "text": (
        f"I read {path.name} ({len(records):,} rows). {kept} of your earlier decisions follow their rows; "
        f"{len(lost)} {'is' if len(lost) == 1 else 'are'} open again because the row changed or is gone."
        if not gl else f"I read {path.name} ({len(records):,} rows).")})
    if not gl:
        CE.refresh_recipe(c, j)
    if j.get("runs"):
        c.run_job(j, "the source file was replaced; converted again by code")
    c.save()
    return report
