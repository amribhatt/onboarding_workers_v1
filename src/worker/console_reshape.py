"""The job screen's "Reshape this sheet" card (ticket 11, tier 2): a person's side of a layout program.

The card appears when the reader cannot place the first sheet of the person's own file (a title instead of a header,
merged headers, one label repeated across columns). It shows, in order: the program in plain words (what the model
says and what code measured), the checks (a BLOCK has no override; each WARN is ticked), a before and after of a
sample with exact counts, and the approval. The approval is a named person's and is bound to the hashes of the grid,
the table and the report the person saw (`worker.flex.service.Flex.approve`); the agent can never make it.

What this module does:
* `reshape_view`: the card's data, built from blobs and the job; nothing here runs a model or a container.
* `reshape_act`: `propose` (the Layout Drafter, which runs and verifies in the sandbox), `reuse` (one click on an
  exact `layout_sig` match within the sponsor), `approve`, `open`.
* After an approval the job reads the approved table as a `derived` source (`console_engine.to_spec`); replay plays
  the table back from the blob store with no Docker.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from worker import persist
from worker import templates as T
from worker.engine import Refused
from worker.errors import StatusError
from worker.flex.artifacts import Artifacts
from worker.flex.grid import cell_index, col_letters, grid_from_workbook, layout_sig, ref, unplaced_reasons
from worker.flex.runner import runner_from_env
from worker.flex.service import BOUND, DerivedReader, Flex, derived_ref
from worker.replay import BlobStore
from worker.roles import RoleRunner, draft_layout

PLAIN = {
    "unaccounted_cells": "Some cells in the sheet were neither used nor set aside.",
    "double_accounted": "A cell was both used and set aside.",
    "double_count": "A number was used more than once.",
    "subtotal_unproven": "A subtotal that was set aside is not exactly the sum of what it covers.",
    "number_dropped": "A number was set aside without being a proven subtotal.",
    "control_total": "The numbers read do not tie to the numbers written.",
    "measure_not_number": "An amount column draws from a cell that is not a number.",
    "part_on_number": "A split was applied to a number.",
    "split_no_part": "A split asked for a part the cell does not have.",
    "nondeterministic": "Two runs of the program gave different tables.",
    "bad_output": "The program's answer is not in the required shape or names a cell that is not there.",
    "explode_too_wide": "One cell was split into too many rows.",
    "limits": "The program did not finish within its limits.",
    "schema_drift": "The columns differ from the saved program's.",
    "misaligned": "A label may not belong to the number it sits beside.",
    "possible_subtotal_kept": "A value equals the sum of the rows above it and may be a subtotal that was kept.",
    "key_distinct_changed": "The number of distinct keys changed between the sheet and the table.",
    "label_numbers_dropped": "A number was set aside as a label (a year in a title, say).",
    "explode_used": "Some rows come from splitting one cell.",
    "subtotal_rounded": "A printed subtotal equals the sum only after rounding to its own decimals.",
}
SAMPLE_ROWS, SAMPLE_COLS, AFTER_ROWS, TEXT = 12, 10, 8, 28
_GRIDS: dict[tuple[str, int, int], dict[str, Any]] = {}


class ReshapeError(StatusError):
    pass


# ---- the stack: one flex service per sponsor, under the console's state folder ---------------------------
def blobs_of(c: Any) -> BlobStore:
    return BlobStore(Path(c.root) / "flex" / "blobs")


def role_runner(c: Any, audit: Any) -> RoleRunner:
    """The Layout Drafter's model runner; the model comes from `.env` under the environment (`WORKER_MODEL_PLAN`),
    as for every other model of the console, and each draft is counted against a spend cap from the same settings."""
    from worker.cli import load_env  # lazy: cli pulls in the agent stack
    from worker.context import Budget

    env = load_env()
    return RoleRunner(audit=audit, budget=Budget.from_env(env), env=env)


_RUNNERS: dict[str, tuple[Any, float, Any]] = {}  # state folder -> (factory, made at, runner): docker is asked rarely


def sandbox_of(c: Any) -> Any:
    """The container runner, or None when the sandbox is off. Looked up at most every 30 s per state folder."""
    key = str(c.root)
    hit = _RUNNERS.get(key)
    if hit is None or hit[0] is not runner_from_env or time.monotonic() - hit[1] > 30:
        hit = (runner_from_env, time.monotonic(), runner_from_env(blobs_of(c)))
        _RUNNERS[key] = hit
    return hit[2]


def flex_of(c: Any, j: dict[str, Any]) -> Flex:
    slug = T.sponsor_slug(j["sp"])
    audit = persist.audit(Path(c.root) / "flex" / f"{slug}.audit.jsonl")
    return Flex(slug, Artifacts(persist.artifact_store(Path(c.root) / "flex" / "artifacts"), audit), blobs_of(c),
                sandbox_of(c), audit)


def source_path(j: dict[str, Any]) -> Path:
    """The person's own workbook, also after a derived table replaced it as the job's source."""
    up = j["up"]
    return Path(str(up.get("source_path") or up["path"]))


def grid_of(j: dict[str, Any]) -> dict[str, Any]:
    p = source_path(j)
    st = p.stat()
    key = (str(p), st.st_mtime_ns, st.st_size)
    if key not in _GRIDS:
        _GRIDS.clear()
        _GRIDS[key] = grid_from_workbook(p)
    return _GRIDS[key]


# ---- the card ------------------------------------------------------------------------------------------
def _issues(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"code": i["code"], "count": i.get("count", 1), "text": PLAIN.get(i["code"], i["code"]),
             "examples": [str(e)[:60] for e in i.get("examples", [])][:5]} for i in items]


def _preview(flex: Flex, rec: dict[str, Any], grid: dict[str, Any]) -> dict[str, Any]:
    """The sheet's top-left window coloured by each cell's fate, and the table's first rows, from the blobs."""
    table = flex.blobs.get_json(rec["output_table_sha256"]) if rec["output_table_sha256"] else None
    raw = flex.blobs.get_json(rec["output_raw_sha256"]) if rec["output_raw_sha256"] else None
    fate: dict[str, str] = {}
    if raw:
        fate.update({d["ref"]: d["reason"] for d in raw.get("dropped", [])})
    if table:
        fate.update({r: "used" for row in table["sources"] for r in row if r})
    s = grid["sheets"][0]
    idx = cell_index(grid)
    rows = sorted({r for r, *_ in s["cells"]})[:SAMPLE_ROWS]
    cols = sorted({c for _r, c, *_ in s["cells"]})[:SAMPLE_COLS]
    before = [{"row": r, "cells": [
        {"ref": ref(s["name"], r, c), "col": col_letters(c),
         "fate": fate.get(ref(s["name"], r, c), "open") if ref(s["name"], r, c) in idx else "blank",
         "text": (idx[ref(s["name"], r, c)][1][:TEXT] if ref(s["name"], r, c) in idx else None)}
        for c in cols]} for r in rows]
    after = None
    if table:
        after = {"columns": table["columns"], "rows": [[{"text": v, "from": src} for v, src in zip(row, srcs,
                                                                                                strict=True)]
                                                       for row, srcs in zip(table["rows"][:AFTER_ROWS],
                                                                            table["sources"][:AFTER_ROWS],
                                                                            strict=True)]}
    return {"before": before, "before_cols": [col_letters(c) for c in cols], "after": after}


def _proposal_view(flex: Flex, state: dict[str, Any], grid: dict[str, Any]) -> dict[str, Any] | None:
    pid = state.get("proposal_id")
    if not pid:
        return None
    rec = flex.blobs.get_json(pid)
    report = flex.blobs.get_json(rec["verification_report_sha256"])
    info = report.get("info", {})
    return {
        "id": pid, "case": rec["case"], "steps": state.get("steps", []), "summary": state.get("summary", ""),
        "program": flex.blobs.get(rec["program_sha256"]).decode("utf-8"), "reused": rec["artifact_id"],
        "blocks": _issues(report["blocks"]), "warns": _issues(report["warns"]),
        "bound": {k: rec[k] for k in BOUND},
        "counts": {"cells_in": info.get("input_cells", 0), "used": info.get("used", 0),
                   "set_aside": info.get("dropped", 0), "set_aside_by_reason": info.get("case_counts", {}),
                   "rows_out": info.get("rows_out", 0), "numbers_in": info.get("numbers_in", ""),
                   "numbers_out": info.get("numbers_out", ""), "subtotals_proven": info.get("subtotals_proven", 0)},
        **_preview(flex, rec, grid)}


def reshape_view(c: Any, j: dict[str, Any]) -> dict[str, Any]:
    """What the card shows for this job. `show` is False for a job whose reader places its sheet (and for a sample
    file), unless the person opened it."""
    up = j.get("up") or {}
    state = dict(j.get("reshape") or {})
    base: dict[str, Any] = {"show": False, "sandbox": False, "reasons": [], "reuse": [], "proposal": None,
                            "applied": up.get("derived"), "note": state.get("note", ""), "can_open": False}
    if not up.get("own") or not up.get("path") or not Path(str(up["path"])).is_file():
        return base
    try:
        grid = grid_of(j)
    except (OSError, ValueError, Refused, KeyError) as exc:
        return {**base, "note": f"the sheet cannot be read for reshaping: {exc}"}
    reasons = unplaced_reasons(grid)
    flex = flex_of(c, j)
    base.update(sandbox=flex.runner is not None, reasons=reasons, can_open=True,
                show=bool(reasons) or bool(state.get("open")) or bool(up.get("derived")),
                layout_sig=layout_sig(grid), file=source_path(j).name)
    if not base["show"]:
        return base
    heads = [] if up.get("derived") else flex.artifacts.heads(flex.sponsor, str(j["type"]), base["layout_sig"])
    base["reuse"] = [{"artifact_id": a.artifact_id, "case": a.case, "proposed_by": a.proposed_by.get("role", ""),
                      "columns": [n for n, _r in a.output_schema]} for a in heads]
    base["proposal"] = _proposal_view(flex, state, grid)
    return base


# ---- the person's actions -----------------------------------------------------------------------------
def _apply(c: Any, j: dict[str, Any], flex: Flex, done: dict[str, Any], who: str, reason: str) -> None:
    """Make the approved table this job's source: a `derived` source the engine reads from the blob store."""
    reader = DerivedReader(None, flex.blobs)
    ref = derived_ref(done["table_sha256"])
    csv_path = reader.path(ref)
    table = flex.blobs.get_json(done["table_sha256"])
    up = j["up"]
    up.setdefault("source_path", up["path"])
    up["path"], up["own"] = str(csv_path), True
    up["derived"] = {"table_sha256": done["table_sha256"], "artifact_id": done["artifact_id"],
                     "approval_id": done["approval_id"], "rows": len(table["rows"]), "by": who}
    j["map"] = {}  # the plan's column choices belonged to the sheet as it was read
    c.log(f"Reshaped {source_path(j).name} into {len(table['rows'])} rows",
          f"program {done['artifact_id'][:14]} approved by {who}: {reason}", j["type"])
    c.save()


def _person(name: str) -> None:
    from worker.improver import reserved_name

    if reserved_name(name):
        raise ReshapeError(f"{name!r} isn't a person's name. Use your own name.", 409)


def reshape_act(c: Any, j: dict[str, Any], body: dict[str, Any], who: str) -> dict[str, Any]:
    """`body.action`: open | propose | reuse | approve. Returns the card's view after the action."""
    action = str(body.get("action", ""))
    state = j.setdefault("reshape", {})
    flex = flex_of(c, j)
    grid = grid_of(j)
    by = " ".join(str(who or "").split()) or "analyst"  # an agent name is refused by `Flex.approve`, not mapped
    if action == "open":
        state["open"] = True
    elif action == "propose":
        why = f"{by} asked to reshape {source_path(j).name}: {'; '.join(unplaced_reasons(grid)) or 'asked by hand'}"
        out = draft_layout(role_runner(c, flex.audit), flex, unit_id=str(j["id"]), record_type=str(j["type"]),
                           grid=grid, why=why)
        p = out.decided or {}
        state.clear()
        state.update(open=True, proposal_id=p.get("proposal_id", ""), steps=p.get("steps", []),
                     summary=p.get("summary", ""), note="" if out.status == "ok" else
                     out.questions[0].options[0].note)
    elif action == "reuse":
        _person(by)
        got = flex.reuse(str(body.get("artifact_id", "")), str(j["type"]), grid, by=by,
                         why=f"{by} reused a saved program for {source_path(j).name}")
        if got["status"] == "approved":
            _apply(c, j, flex, got, by, "an exact layout match, run again with every check clean")
            state.clear()
        else:
            state.clear()
            state.update(open=True, proposal_id=got["proposal_id"], steps=[], summary="",
                         note="The saved program ran on this file and the checks need you."
                         if got["status"] == "needs_card" else "The saved program does not fit this file.")
    elif action == "approve":
        _person(by)
        done = flex.approve(str(body.get("proposal_id", "")), by=by, scope=str(body.get("scope", "job")),
                            bound=dict(body.get("bound") or {}), acknowledged=list(body.get("acknowledged") or []),
                            reason=str(body.get("reason", "")).strip() or f"{by} checked the preview")
        _apply(c, j, flex, done, by, str(body.get("reason", "")).strip() or "checked the preview")
        state.clear()
    else:
        raise ReshapeError(f"no action {action!r}", 404)
    c.save()
    return reshape_view(c, j)
