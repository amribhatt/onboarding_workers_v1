"""Replace a job's source file at any stage, as often as the person wants, and decide what that does to the work after it.

The new file is first COMPARED with the old one, so the system knows what it was given:
  - "same":      every row of the old file is in the new one (order and formatting aside): nothing changed in the data;
  - "modified":  mostly the same rows with some edited, added or removed (the same data with slight changes);
  - "different": few rows match, or the columns are mostly not the old ones: this is new data.
Rows are matched by CONTENT (identical cells, one-to-one), and a row that is not identical is paired with the row of
the new file it shares most cells with, so an edit is told apart from a removal plus an addition.

What the person decided follows from that. In every case a decision follows its row when the row is identical; a row that
was edited, or is gone, is not guessed at: its decision is dropped and the row is open again, and the report says which.
Only for "different" data is everything that belonged to the old data cleared (decisions, the issue and reconciliation
history, answers about unknown columns, column choices, the job's own field steps, a reshaped table, the learned items)
because none of it can apply to a new file. Whenever the columns changed, or the data is different, the job goes back to
the Map phase to be confirmed again; otherwise it is converted again by code and keeps its place.

The change is audited (`source.replaced`: file hashes, the upload number, the verdict and its counts, what was kept,
re-opened and cleared, who) BEFORE it takes effect; if the record cannot be written the new file is removed and the job is
as it was. An approved job never changes. A GL job that already has issue decisions is refused (its decisions are tied to
journal lines and their places; undo them first): a GL job without decisions swaps the file freely.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter, defaultdict, deque
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

AUDITED = ("from", "to", "upload_no", "verdict", "counts", "header_overlap", "kept", "reopened", "reopened_listed",
           "edited_listed", "column_choices_dropped", "reshape_dropped", "reset", "back_to_map", "note")
SHOWN = ("at", "by", "from", "to", "upload_no", "verdict", "counts", "kept", "reopened", "reset", "back_to_map", "note")
LISTED = 20  # re-opened decisions named one by one in the report; the count is exact
DIFFERENT_BELOW = 0.3  # under this share of rows that are the same or edited, the file is different data
HEADER_OVERLAP_MIN = 0.5  # under this share of shared column names, the file is different data
PAIR_CAP = 3000  # edited rows are paired with their new version only while this few rows are left unmatched on each side
PAIR_MIN_SHARE = 0.5  # a pair shares at least this share of its cells
CHANGED_SHOWN = 3  # edited cells named per edited row in the report
SLOT_LABELS = (("dec", "decisions on rows"), ("fill", "values you typed"), ("dates", "commitment dates"),
               ("qa", "confirmed matches and quick entries"), ("acct", "account mappings"), ("dealfill", "deals you supplied"),
               ("cells", "cell edits"))


class ReuploadError(StatusError):
    """A refusal, said in plain words; the web layer turns it into a 409."""


def _vals(rec: dict[str, Any]) -> list[str]:
    """The row's cells as text, in column order, without the column names: renaming a column does not change a row."""
    return [" ".join(str(v).split()) if v is not None else "" for v in rec["v"].values()]


def _sig(rec: dict[str, Any]) -> str:
    return json.dumps(_vals(rec), ensure_ascii=False)


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


def _cells(rec: dict[str, Any]) -> set[tuple[int, str]]:
    return {(k, v.casefold()) for k, v in enumerate(_vals(rec)) if v}


def _pairs(old: list[dict[str, Any]], new: list[dict[str, Any]], moved: dict[int, int]) -> dict[int, int]:
    """old record number -> new record number for rows that are NOT identical but are plainly the same row edited: the
    new row sharing the most cells with it, at least PAIR_MIN_SHARE of them. Each new row is used once. With very many
    unmatched rows on a side nothing is paired (they count as removed and added): the answer stays cheap and honest."""
    taken = set(moved.values())
    left_old = [r for r in old if int(r["i"]) not in moved]
    left_new = [r for r in new if int(r["i"]) not in taken]
    if not left_old or not left_new or max(len(left_old), len(left_new)) > PAIR_CAP:
        return {}
    theirs = {int(r["i"]): _cells(r) for r in left_new}
    index: dict[tuple[int, str], list[int]] = defaultdict(list)
    for i, cs in theirs.items():
        for cell in cs:
            index[cell].append(i)
    used: set[int] = set()
    out: dict[int, int] = {}
    for r in left_old:
        mine = _cells(r)
        votes = Counter(i for cell in mine for i in index.get(cell, ()) if i not in used)
        if not votes:
            continue
        best, hit = min(votes.items(), key=lambda kv: (-kv[1], kv[0]))
        if hit >= PAIR_MIN_SHARE * max(len(mine), len(theirs[best]), 1):
            out[int(r["i"])] = best
            used.add(best)
    return out


def _header_overlap(old: list[str], new: list[str]) -> float:
    a, b = {" ".join(str(h).split()).casefold() for h in old if h}, {" ".join(str(h).split()).casefold() for h in new if h}
    return len(a & b) / len(a | b) if a | b else 0.0


def classify(old: list[dict[str, Any]], old_header: list[str], new: list[dict[str, Any]], new_header: list[str],
             moved: dict[int, int], pairs: dict[int, int]) -> dict[str, Any]:
    """What the new file is to the old one: {"verdict": same | modified | different, "counts", "header_overlap"}."""
    base = max(len(old), len(new)) or 1
    counts = {"old_rows": len(old), "new_rows": len(new), "unchanged": len(moved), "edited": len(pairs),
              "removed": len(old) - len(moved) - len(pairs), "added": len(new) - len(moved) - len(pairs)}
    overlap = round(_header_overlap(old_header, new_header), 3)
    if not old or not new or overlap < HEADER_OVERLAP_MIN or (len(moved) + len(pairs)) / base < DIFFERENT_BELOW:
        verdict = "different"
    elif len(moved) == len(old) == len(new):
        verdict = "same"
    else:
        verdict = "modified"
    return {"verdict": verdict, "counts": counts, "header_overlap": overlap}


def _edits(old: list[dict[str, Any]], new: list[dict[str, Any]], pairs: dict[int, int]) -> list[dict[str, Any]]:
    """The first edited rows, each with the cells that changed (the first few), for the report."""
    by_old = {int(r["i"]): r for r in old}
    by_new = {int(r["i"]): r for r in new}
    out: list[dict[str, Any]] = []
    for o, n in sorted(pairs.items())[:LISTED]:
        a, b = _vals(by_old[o]), _vals(by_new[n])
        names = list(by_new[n]["v"])
        out.append({"was": int(by_old[o]["row"]), "now": int(by_new[n]["row"]),
                    "changed": [{"field": names[k] if k < len(names) else f"column {k + 1}", "before": a[k][:60] if k < len(a) else "",
                                 "after": b[k][:60] if k < len(b) else ""}
                                for k in range(max(len(a), len(b)))
                                if (a[k] if k < len(a) else "") != (b[k] if k < len(b) else "")][:CHANGED_SHOWN]})
    return out


def _clear_for_new_data(j: dict[str, Any], person: str, when: str, *, dry: bool = False) -> list[str]:
    """The data is different: nothing decided about the old data applies. Returns what is (or, with `dry`, would be)
    cleared, for the report; with `dry` nothing changes, so the record can name it before it happens."""
    cleared: list[str] = []

    def said(label: str) -> None:
        if label not in cleared:
            cleared.append(label)

    for slot, label in SLOT_LABELS:
        if j.get(slot):
            said(label)
        if not dry:
            j[slot] = {}
    for h in CI._hist(j):
        if not h["rev"]:
            said("issue decisions")
            if not dry:
                h["rev"], h["released"] = True, {"by": person, "at": when, "note": "the source file was replaced by different data"}
    for h in j.get("recon_hist") or []:
        if not h["rev"]:
            said("reconciliation decisions")
            if not dry:
                h["rev"] = True
    for ans in j.get("answers") or []:
        if not ans.get("undone"):
            said("answers about unknown columns")
            if not dry:
                ans["undone"] = True
    for slot, label in (("steps", "field steps of this job"), ("reshape", "the reshaped table")):
        if j.get(slot):
            said(label)
        if not dry:
            j.pop(slot, None)
    if not dry:
        for slot in ("chat_read", "chat_miss", "issue_prop", "issue_groups", "empty_ok"):
            j.pop(slot, None)
        j["draft"], j["fb"], j["trace"], j["learn"], j["by"] = None, None, None, [], {}  # as a new job starts
    return cleared


def _remap_by(j: dict[str, Any], moved: dict[int, int]) -> None:
    """Who decided each thing follows the thing: a row that moved takes its decider with it, one that is gone loses it."""
    out: dict[str, str] = {}
    for k, who in (j.get("by") or {}).items():
        slot, _, key = k.partition("|")
        if slot in ("dec", "dates") and key.isdigit():
            if int(key) in moved:
                out[f"{slot}|{moved[int(key)]}"] = who
        elif slot in ("fill", "cells"):
            i, _, field = key.partition("|")
            if i.isdigit() and int(i) in moved:
                out[f"{slot}|{moved[int(i)]}|{field}"] = who
        elif slot == "qa" and key[:1] == "f" and key[1:].isdigit():
            if int(key[1:]) in moved:
                out[f"qa|f{moved[int(key[1:])]}"] = who
        else:
            out[k] = who  # named by its own value (a column, an account, a quick entry), not by a place
    j["by"] = out


def _back_to_map(j: dict[str, Any]) -> None:
    """The phases after Map are not done any more: the plan is to be confirmed again and the file converted again."""
    j["runs"], j["failed"], j["failedOnce"], j["retries"] = [], None, False, 0
    j["up"]["stage"] = "ready"
    j.pop("eval_key", None)


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
    out: dict[str, Any] = {"dec": {}, "dates": {}, "fill": {}, "qa": {}, "cells": {}}
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
    corrected = j.get("cells") or {}
    for k, v in corrected.items():  # a corrected cell goes with its row; if the row changed, the correction was about the old one
        i, _, column = str(k).partition("|")
        if int(i) in moved:
            out["cells"][f"{moved[int(i)]}|{column}"] = v
            kept += 1
        else:
            lost.append({"slot": f"cell edit ({column})", "was": int(i), "what": str(v)[:80]})
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
                if slot in ("dec", "dates", "fill", "cells") and head.isdigit():
                    if int(head) in moved:
                        n = moved[int(head)]
                        new[slot].append(str(key).replace(head, str(n), 1) if slot in ("fill", "cells") else str(n))
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
        raise ReuploadError("This job is approved, so it is final. Nothing changed.")
    gl = RF.is_gl(str(j["type"]))
    if gl and any(not h["rev"] for h in CI._hist(j)):
        raise ReuploadError("This GL job already has issue decisions, which belong to its journal lines: undo them on the "
                            "Activity tab first, then replace the file.")
    note = " ".join(str(note or "").split())[:CI.NOTE_MAX]
    c.check_upload(name, data)
    old_path = Path(str(j["up"]["path"]))
    old_bytes = old_path.read_bytes() if old_path.exists() else b""
    if old_bytes == data:
        raise ReuploadError("That is the file this job already reads.")
    path, sheet, header, records, fresh = c.store_upload(name, data, static=not gl)

    # ---- what is the new file to the old one
    old_records: list[dict[str, Any]] = []
    old_header: list[str] = []
    moved: dict[int, int] = {}
    pairs: dict[int, int] = {}
    lost: list[dict[str, Any]] = []
    kept = 0
    carried: dict[str, Any] = {}
    cls: dict[str, Any] = {"verdict": "unknown", "counts": {"new_rows": len(records)}, "header_overlap": None}
    if not gl:
        try:
            if old_path.exists():
                _s, old_header, old_records = EC.read_static_records(old_path)
        except (OSError, ValueError, KeyError, RuntimeError):  # the old file cannot be read any more: nothing can be matched
            old_records, old_header = [], []
        moved = _moves(old_records, records)
        pairs = _pairs(old_records, records, moved)
        cls = classify(old_records, old_header, records, header, moved, pairs)
        carried, lost, kept = _carry(j, moved)
        for item in lost:  # an edited row says so: its decision was about the old version of it
            item["edited"] = item["was"] in pairs
    different = cls["verdict"] == "different"
    keep_map = {f: h for f, h in (j.get("map") or {}).items() if h in header}
    dropped_map = {f: h for f, h in (j.get("map") or {}).items() if h not in header}
    layout_changed = bool(old_header) and _header_overlap(old_header, header) < 1.0
    back = bool(j.get("runs")) and (different or layout_changed or bool(dropped_map))
    reopened = _lost_for(j, lost, different)
    log = j.setdefault("upload_log", [])
    if not log:  # a job from before uploads were counted: its first file is upload 1
        log.append({"no": 1, "at": j.get("t", ""), "file": j["up"].get("file", ""), "kind": "initial", "sha256": _sha(old_bytes)})
    upload_no = len(log) + 1
    report = {
        "from": {"file": j["up"].get("file", ""), "sha256": _sha(old_bytes), "rows": len(old_records)},
        "to": {"file": path.name, "sha256": _sha(data), "sheet": sheet, "rows": len(records)},
        "upload_no": upload_no, "verdict": cls["verdict"], "counts": cls["counts"], "header_overlap": cls["header_overlap"],
        "kept": 0 if different else kept, "reopened": len(reopened), "reopened_listed": reopened[:LISTED], "edited_listed": _edits(old_records, records, pairs),
        "column_choices_dropped": dropped_map, "reshape_dropped": bool((j["up"].get("derived") or {}).get("table_sha256")),
        "reset": _clear_for_new_data(j, person, "", dry=True) if different else [], "back_to_map": back, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "by": person, "note": note,
    }
    what = {"same": "the same data", "modified": "the same data with changes", "different": "different data",
            "unknown": "a new file"}[cls["verdict"]]
    reason = (f"{person} replaced the source file {report['from']['file'] or '(none)'} with {path.name} (upload {upload_no}): "
              f"{what}; {report['kept']} decisions kept, {report['reopened']} re-opened"
              + ("; the data is different, so everything decided about the old data was cleared" if different else "")
              + ("; back to the Map phase" if back else "") + (f". Note: {note}" if note else ""))
    try:  # recorded first: if it cannot be, nothing changes and the new file is not kept
        persist.job_audit(c, j).record("source.replaced", reason, {"job": j["id"], "reviewer": person,
                                                                    **{k: report[k] for k in AUDITED}}, {})
    except (AuditError, OSError, ValueError) as exc:
        if fresh:
            path.unlink(missing_ok=True)
        raise ReuploadError(f"I couldn't record the replacement, so nothing changed: {exc}") from exc

    # ---- now it takes effect
    def row_of(n: int) -> int:  # the new file's records are numbered 1, 2, 3 ... in sheet order
        return int(records[n - 1]["row"])

    if different:
        _clear_for_new_data(j, person, report["at"])
    elif not gl:
        j["dec"], j["dates"], j["fill"], j["qa"] = carried["dec"], carried["dates"], carried["fill"], carried["qa"]
        j["cells"] = carried["cells"]
        _remap_history(j, moved, row_of, person, report["at"])
        _remap_by(j, moved)
    j["map"] = {} if different else keep_map
    up = j["up"]
    up.update({"file": path.name, "path": str(path), "size": len(data), "sheet": sheet, "own": True, "stage": "ready"})
    up.pop("derived", None)
    log.append({"no": upload_no, "at": report["at"], "by": person, "file": path.name, "kind": "replacement",
                "sha256": report["to"]["sha256"], "verdict": cls["verdict"]})
    j.setdefault("replaced", []).append({k: report[k] for k in SHOWN})
    c.log(f"Replaced the source file with {path.name}", reason)
    j["chat"].append({"who": "w", "role": "Checker", "text": _say(report, path.name, len(records), gl)})
    if not gl:
        CE.refresh_recipe(c, j)
    if back:
        _back_to_map(j)
    elif j.get("runs"):
        c.run_job(j, "the source file was replaced; converted again by code")
    c.save()
    return report


def corrected_cells(j: dict[str, Any]) -> dict[str, Any]:
    return dict(j.get("cells") or {})


def _lost_for(j: dict[str, Any], lost: list[dict[str, Any]], different: bool) -> list[dict[str, Any]]:
    """The decisions that did not survive: for different data every one of them; otherwise the ones whose row changed."""
    if not different:
        return lost
    return [{"slot": "decision", "was": int(k) if str(k).isdigit() else 0, "what": str(v)[:80]}
            for k, v in j.get("dec", {}).items()][:10_000] + [
        {"slot": f"value for {str(k).partition('|')[2]}", "was": int(str(k).partition('|')[0]), "what": str(v)[:80]}
        for k, v in j.get("fill", {}).items() if str(k).partition("|")[0].isdigit()] + [
        {"slot": "commitment date", "was": int(k), "what": str(v)} for k, v in j.get("dates", {}).items() if str(k).isdigit()] + [
        {"slot": f"cell edit ({str(k).partition('|')[2]})", "was": int(str(k).partition('|')[0]), "what": str(v)[:80]}
        for k, v in corrected_cells(j).items()]


def _say(rep: dict[str, Any], name: str, rows: int, gl: bool) -> str:
    """What the worker tells the person about the new file, in plain words."""
    c, v = rep["counts"], rep["verdict"]
    if gl or v == "unknown":
        return f"I read {name} ({rows:,} rows)."
    if v == "same":
        return (f"I read {name} ({rows:,} rows). It is the same data as before: no row changed. "
                f"All {rep['kept']} of your decisions stay.")
    if v == "modified":
        return (f"I read {name} ({rows:,} rows). It looks like the same data with changes: {c['unchanged']:,} rows are "
                f"unchanged, {c['edited']:,} edited, {c['added']:,} new, {c['removed']:,} gone. {rep['kept']} of your decisions "
                f"follow their rows; {rep['reopened']} are open again because their row changed or is gone."
                + (" The columns changed, so please check the plan again." if rep["back_to_map"] else ""))
    return (f"I read {name} ({rows:,} rows). This looks like different data: only {c['unchanged'] + c['edited']:,} of its rows "
            f"match the old file. What you decided about the old data no longer applies, so I cleared it"
            + (f" ({', '.join(rep['reset'])})" if rep["reset"] else "") + ". Check the plan and press Looks right to convert it again.")
