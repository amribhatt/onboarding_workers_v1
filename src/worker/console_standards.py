"""Replacing an all-sponsor standard from the console: a filled template, chart of accounts or type mapping.

An upload never changes the live file. It is read, compared with the live standard (a real header diff, and for the
chart and the mapping a row diff), and filed as an improvement card of kind `standard` in the same
`ImprovementStore` every other improvement uses. A named human approves it in Improvements; only then
`apply_standard` writes the file, keeping the previous bytes as a numbered snapshot. All of it is plain code.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import shutil
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import openpyxl

from worker import columnar, persist
from worker import templates as T
from worker.errors import StatusError
from worker.improver import ImprovementStore, guard

COA_FILE = "Golden_COA.csv"
TTM_FILE = "Transaction_Type_Mapping.csv"
SUFFIX = "_Upload_Template.xlsx"
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,29}$")
# The columns a standard cannot lose: the chart is keyed by account number, the mapping by type code.
REQUIRED = {"coa": ("Account_Number", "Account_Name"), "ttm": ("Transaction_Type_Code", "Allowed_Accounts")}
KEY = {"coa": "Account_Number", "ttm": "Transaction_Type_Code"}
FILES = {"coa": COA_FILE, "ttm": TTM_FILE}
SAMPLE = 8  # rows named in a row diff; the counts are always complete


class StandardsError(StatusError):
    """A request that cannot be honoured; `status` is the HTTP status to answer with."""


# ---- reading --------------------------------------------------------------------------------------------
def read_table(filename: str, data: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Headers and rows of the first sheet of a .xlsx or of a .csv. Blank rows are dropped."""
    ext = Path(filename).suffix.lower()
    grid: list[list[str]]
    if ext in (".xlsx", ".xlsm"):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "standards.xlsx"
            src.write_bytes(data)
            try:
                grid = [[columnar.cell_text(v) for v in row] for _n, row in columnar.rows(src)]
            except Exception as exc:
                raise StandardsError(
                    f"{filename} could not be opened as a workbook: {type(exc).__name__}.") from exc
    elif ext == ".csv":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise StandardsError(f"{filename} is not UTF-8 text.") from exc
        grid = [[c.strip() for c in row] for row in csv.reader(io.StringIO(text))]
    else:
        raise StandardsError(f"{filename}: upload a .xlsx or .csv file.", 400)
    grid = [r for r in grid if any(c for c in r)]
    if not grid:
        raise StandardsError(f"{filename} has no header row.")
    top = grid[0]
    last = max((i for i, c in enumerate(top) if c), default=-1)
    headers = top[: last + 1]
    if any(not h for h in headers):
        raise StandardsError(f"{filename}: the header row has an empty column name.")
    dup = sorted({h for h in headers if headers.count(h) > 1})
    if dup:
        raise StandardsError(f"{filename}: the column name is repeated: {', '.join(dup)}.")
    rows = [{h: (r[i] if i < len(r) else "") for i, h in enumerate(headers)} for r in grid[1:]]
    return headers, rows


def _live_table(kind: str) -> tuple[list[str], list[dict[str, str]]]:
    path = T.standards_dir() / FILES[kind]
    if not path.is_file():
        raise StandardsError(f"I can't find the live {FILES[kind]}, so I have nothing to compare it with.", 409)
    return read_table(path.name, path.read_bytes())


# ---- diffs ----------------------------------------------------------------------------------------------
def header_diff(current: Sequence[str], new: Sequence[str]) -> dict[str, Any]:
    """Added and removed columns, whether the order moved, and positional renames (a removed and an added column
    sitting in the same place): a hint a person confirms, never applied by itself."""
    cur, nxt = list(current), list(new)
    added = [h for h in nxt if h not in cur]
    removed = [h for h in cur if h not in nxt]
    kept_cur = [h for h in cur if h in nxt]
    kept_new = [h for h in nxt if h in cur]
    renames = [[cur[i], nxt[i]] for i in range(min(len(cur), len(nxt)))
               if cur[i] in removed and nxt[i] in added]
    return {"added": added, "removed": removed, "moved": kept_cur != kept_new, "renames": renames,
            "same": not added and not removed and kept_cur == kept_new, "before": cur, "after": nxt}


def row_diff(kind: str, cur: Sequence[dict[str, str]], new: Sequence[dict[str, str]]) -> dict[str, Any]:
    key = KEY[kind]
    a = {r.get(key, ""): r for r in cur if r.get(key)}
    b = {r.get(key, ""): r for r in new if r.get(key)}
    added = [k for k in b if k not in a]
    removed = [k for k in a if k not in b]
    changed = [k for k in b if k in a and any(b[k].get(c, "") != a[k].get(c, "") for c in b[k] if c in a[k])]
    return {"key": key, "added": len(added), "removed": len(removed), "changed": len(changed),
            "sample": {"added": added[:SAMPLE], "removed": removed[:SAMPLE], "changed": changed[:SAMPLE]},
            "removed_keys": removed}


def _csv_bytes(headers: Sequence[str], rows: Sequence[dict[str, str]]) -> bytes:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(headers)
    for r in rows:
        w.writerow([r.get(h, "") for h in headers])
    return out.getvalue().encode("utf-8")


def _header_only_xlsx(headers: Sequence[str]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(headers))
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _accounts_in(allowed: str) -> list[str]:
    return [a.strip() for a in re.split(r"[,;]", allowed) if a.strip()]


# ---- filing ---------------------------------------------------------------------------------------------
def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def live_path(kind: str, name: str = "") -> Path:
    if kind in FILES:
        return T.standards_dir() / FILES[kind]
    return T.templates_dir() / f"{name}{SUFFIX}"


def waiting(store: ImprovementStore) -> dict[str, dict[str, Any]]:
    """Standard cards still awaiting a human, by the file they would replace."""
    out: dict[str, dict[str, Any]] = {}
    for c in store.cards():
        if c["proposal"]["kind"] == "standard" and c["status"] in ("ready", "blocked"):
            ch = c["proposal"]["change"]
            out[ch["file"]] = {"id": c["id"], "status": c["status"], "summary": c["proposal"]["summary"],
                               "action": ch["action"]}
    return out


def propose_upload(store: ImprovementStore, kind: str, filename: str, data: bytes, who: str,
                   name: str = "") -> dict[str, Any]:
    """Read an upload, compare it with the live standard and file the card. Nothing live changes here.

    kind: `tmpls` (replace the template `name`), `new` (add a template; `name` defaults to the file name),
    `coa` or `ttm`. Raises StandardsError when the upload cannot be read or changes nothing.
    """
    if kind not in ("tmpls", "new", "coa", "ttm"):
        raise StandardsError(f"Unknown standard {kind!r}.", 404)
    headers, rows = read_table(filename, data)
    action = "add" if kind == "new" else "replace"
    gates: list[list[str]] = []
    reasons: list[str] = []
    if kind in ("tmpls", "new"):
        if not filename.lower().endswith((".xlsx", ".xlsm")):
            raise StandardsError("A template is an .xlsx file.", 400)
        stem = re.sub(r"(?i)_Upload_Template$", "", Path(filename).stem)
        name = name or stem
        if not NAME_RE.match(name):
            raise StandardsError(f"{name!r} is not a usable template name: letters and digits, starting with a letter.",
                                 400)
        if kind == "tmpls" and name not in T.TEMPLATES:
            raise StandardsError(f"There is no template named {name}.", 404)
        if kind == "new" and name in T.TEMPLATES:
            raise StandardsError(f"{name} already exists; upload it from its own row to replace it.")
        target = f"{name}{SUFFIX}"
        current = list(T.TEMPLATES[name].fields) if kind == "tmpls" else []
        diff = header_diff(current, headers)
        required = T.TEMPLATES[name].required if kind == "tmpls" else tuple(headers[:2])
        payload, rdiff = _header_only_xlsx(headers), None
        if len(headers) < 2:
            reasons.append("a template needs at least two columns")
    else:
        target = FILES[kind]
        cur_headers, cur_rows = _live_table(kind)
        diff = header_diff(cur_headers, headers)
        required = REQUIRED[kind]
        rdiff = row_diff(kind, cur_rows, rows)
        payload = _csv_bytes(headers, rows)
        if diff["same"] and cur_rows == rows:
            raise StandardsError(f"{filename} is the same as the live {target}: nothing to change.")
        keys = [r.get(KEY[kind], "") for r in rows]
        dups = sorted({k for k in keys if k and keys.count(k) > 1})
        blank = sum(1 for k in keys if not k)
        bad = (f"{len(dups)} repeated ({', '.join(dups[:3])})" if dups else "") or (f"{blank} blank" if blank else "")
        gates.append([f"Unique {KEY[kind]}", bad or "every row has its own", "err" if bad else "ok"])
        if bad:
            reasons.append(f"{KEY[kind]} is not unique and filled on every row")
    if kind in ("tmpls", "new") and diff["same"]:
        raise StandardsError(f"{filename} has the same columns as the live {target}: nothing to change.")

    missing = [c for c in required if c not in headers]
    gates.insert(0, ["Required columns kept", ("missing " + ", ".join(missing)) if missing else
                     f"all {len(required)} present", "err" if missing else "ok"])
    if missing:
        reasons.append("a required column is missing: " + ", ".join(missing))
    gates.insert(0, ["Header read", f"{len(headers)} columns, {len(rows)} data row{'s' if len(rows) != 1 else ''}",
                     "ok"])
    if kind == "coa" and rdiff:
        _, ttm_rows = _live_table("ttm")
        gone = set(rdiff["removed_keys"])
        used = sorted({a for r in ttm_rows for a in _accounts_in(r.get("Allowed_Accounts", "")) if a in gone})
        gates.append(["Removed accounts still mapped", f"{len(used)} in the type mapping ({', '.join(used[:4])})"
                      if used else "none", "err" if used else "ok"])
        if used:
            reasons.append("accounts the transaction type mapping still allows would disappear: " + ", ".join(used[:6]))
    if kind == "ttm" and rdiff:
        coa_headers, coa_rows = _live_table("coa")
        have = {r.get("Account_Number", "") for r in coa_rows}
        unknown = sorted({a for r in rows for a in _accounts_in(r.get("Allowed_Accounts", "")) if a not in have})
        gates.append(["Accounts known to the chart", f"{len(unknown)} not in the Golden COA ({', '.join(unknown[:4])})"
                      if unknown else "all known", "err" if unknown else "ok"])
        if unknown:
            reasons.append("accounts not in the Golden chart: " + ", ".join(unknown[:6]))
        _ = coa_headers
    if rdiff:
        gates.append(["Rows", f"{rdiff['added']} added, {rdiff['removed']} removed, {rdiff['changed']} changed",
                      "ok"])
    if kind in ("tmpls", "new") and rows:
        gates.append(["Data rows in the upload", f"{len(rows)} dropped: a template keeps its header row only", "ok"])
    gates.append(["Finished work", "unchanged; applies to jobs started after approval", "ok"])

    live = live_path(kind, name)
    noun = {"tmpls": f"{name} template", "new": f"new template {name}", "coa": "Golden COA",
            "ttm": "Transaction type mapping"}[kind]
    bits = []
    if diff["added"]:
        bits.append(f"{len(diff['added'])} column(s) added")
    if diff["removed"]:
        bits.append(f"{len(diff['removed'])} removed")
    if diff["moved"]:
        bits.append("order changed")
    if rdiff and (rdiff["added"] or rdiff["removed"] or rdiff["changed"]):
        bits.append(f"rows +{rdiff['added']} -{rdiff['removed']} ~{rdiff['changed']}")
    summary = f"{who or 'console'} uploads {noun}: " + (", ".join(bits) or "header unchanged")
    cid = f"std-{int(time.time() * 1000) % 10**10}-{len(store.cards()) + 1}"
    stored = store.root / "standards" / cid / target
    before_txt = (f"{len(diff['before'])} columns: " + ", ".join(diff["before"])) if diff["before"] else (
        f"No {name} template." if kind == "new" else "")
    after_txt = f"{len(headers)} columns: " + ", ".join(headers)
    if diff["added"]:
        after_txt += ". New: " + ", ".join(diff["added"])
    if diff["removed"]:
        after_txt += ". Dropped: " + ", ".join(diff["removed"])
    if diff["renames"]:
        after_txt += ". Possible renames: " + ", ".join(f"{a} → {b}" for a, b in diff["renames"])
    proposal = {
        "kind": "standard", "target": f"templates/standards/{target}", "scope": "organisation",
        "change": {"action": action, "standard": kind, "name": name, "file": target, "upload": f"standards/{cid}/{target}",
                   "base_sha": _sha(live), "source_file": filename, "header_diff": diff, "row_diff": rdiff,
                   "before": before_txt, "after": after_txt},
        "evidence": {"sponsors": [], "count": 1, "skill": "map-accounts" if kind in FILES else "write-output"},
        "proposed_by": who or "console", "speed": "fast", "summary": summary,
    }
    guard(proposal["kind"], proposal["target"])
    card = {"id": cid, "proposal": proposal, "predicted": {}, "replay": {"reasons": reasons}, "gates": gates,
            "status": "blocked" if reasons else "ready", "filed": time.strftime("%Y-%m-%dT%H:%M:%S"), "history": []}
    clash = waiting(store).get(target)
    if clash and clash["status"] == "ready":
        raise StandardsError(f"a change to {target} is already waiting in Improvements ({clash['id']}); "
                             "approve or reject it before uploading another")
    if clash:  # a blocked card can never be approved: the corrected upload supersedes it
        old = store.load(clash["id"])
        old.update(status="rejected")
        old["history"].append({"event": "rejected", "by": who or "console", "reason": "superseded by a new upload"})
        store.save(old)
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(payload)
    store.save(card)
    if store.audit is not None:
        store.audit.record("standard.filed", summary, {"card": cid, "file": target, "by": who or "console"},
                           {"status": card["status"], "reasons": reasons})
    return {"card": cid, "status": card["status"], "file": target, "action": action, "summary": summary,
            "header_diff": {k: diff[k] for k in ("added", "removed", "moved", "renames")}, "row_diff": rdiff,
            "gates": gates, "reasons": reasons}


# ---- approval -------------------------------------------------------------------------------------------
def apply_standard(store: ImprovementStore, card: dict[str, Any]) -> int:
    """Write the approved file. Called only by `ImprovementStore.promote`, after a named human approved.

    The previous file is kept as `<state>/standards_versions/<file>/v<N>`. Returns N, the file's version.
    """
    ch = card["proposal"]["change"]
    src = store.root / ch["upload"]
    if not src.is_file():
        raise ValueError(f"The uploaded file for {card['id']} is missing; upload it again.")
    dest = live_path(ch["standard"], ch["name"])
    if ch["action"] == "replace" and _sha(dest) != ch["base_sha"]:
        raise ValueError(f"{ch['file']} changed since this was filed; upload it again so the diff is current.")
    if ch["action"] == "add" and dest.is_file():
        raise ValueError(f"{ch['file']} now exists; upload it again as a replacement.")
    snaps = store.root.parent / "standards_versions" / ch["file"]
    snaps.mkdir(parents=True, exist_ok=True)
    n = len(list(snaps.glob("v*"))) + 1
    if dest.is_file():
        shutil.copy2(dest, snaps / f"v{n}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    if ch["standard"] in ("tmpls", "new"):
        try:  # the running process sees the new column list; an unreadable template is put back
            fresh = T.load_templates()
        except ValueError as exc:
            if (snaps / f"v{n}").is_file():
                shutil.copy2(snaps / f"v{n}", dest)
            else:
                dest.unlink(missing_ok=True)
            raise ValueError(f"{ch['file']} is not a usable template: {exc}.") from exc
        T.TEMPLATES.clear()
        T.TEMPLATES.update(fresh)
    return n


def waiting_for(root: Path | None) -> dict[str, dict[str, Any]]:
    """`waiting` for a console that only knows the state folder; empty when there is none."""
    if root is None:
        return {}
    return waiting(persist.improvements(Path(root) / "improvements"))


__all__ = ["StandardsError", "apply_standard", "header_diff", "propose_upload", "read_table", "row_diff", "waiting",
           "waiting_for"]
