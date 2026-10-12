"""Running a layout program: audit first, run twice, audit the result, verify, and only then trust the table.

Order is the guarantee. Each row below is written BEFORE the thing it describes happens, and an `AuditError` stops
the step it guards:

    flex.exec.started   -> the runner is called          (a sink that fails: the runner is never called)
    flex.exec.finished  -> the output is read or used    (a sink that fails: the output is discarded)
    flex.verify         -> the report is shown

The program is never trusted: its output is parsed and every reference checked against the grid by `verify`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from worker.engine import Refused
from worker.flex.grid import canonical_json, sha256_bytes
from worker.flex.runner import Runner, new_exec_id
from worker.flex.verify import Verified, lint_program, verify

MAX_GRID_BYTES = 200 * 1024 * 1024
STATUS_BLOCK = {"timeout": "limits", "oom": "limits", "exit": "limits", "no_output": "bad_output",
                "violation": "limits"}


@dataclass
class LayoutRun:
    verified: Verified
    program_sha256: str
    grid_sha256: str
    image: str
    exec_ids: tuple[str, ...]
    statuses: tuple[str, ...]
    output_sha256s: tuple[str, ...]
    seconds: float = 0.0
    stderr: str = ""
    raw_output: bytes | None = None  # the first run's output, kept so a replay can verify without Docker


def run_layout(*, runner: Runner, audit: Any, grid: Mapping[str, Any], program: str, why: str,
               flex: Mapping[str, int] | None = None, subject: Mapping[str, Any] | None = None) -> LayoutRun:
    """Run `program` on `grid` twice (hash seeds 0 and 1) and verify. Raises `Refused` (lint, size) or
    `AuditError`; any failure of the container itself is a BLOCK in the returned report, never an exception."""
    source = program.replace("\r\n", "\n")
    program_sha = sha256_bytes(source.encode("utf-8"))
    problems = lint_program(source)
    if problems:
        audit.record("flex.lint_refused", why, {"program_sha256": program_sha, **(subject or {})},
                     {"problems": problems[:10]})
        raise Refused("lint_refused", "the program is refused before any run: " + "; ".join(problems[:3]))
    grid_bytes = canonical_json(grid)
    if len(grid_bytes) > MAX_GRID_BYTES:
        audit.record("flex.refused", why, {"program_sha256": program_sha}, {"reason": "too_large"})
        raise Refused("too_large", "the grid is too large for a layout program.")
    grid_sha = sha256_bytes(grid_bytes)
    ids = (new_exec_id(), new_exec_id())
    base = {"program_sha256": program_sha, "input_grid_sha256": grid_sha, "image_digest": runner.image,
            "harness": "layout-harness/1", **(subject or {})}
    audit.record("flex.exec.started", why, {**base, "exec_ids": list(ids), "kind": "layout", "seeds": [0, 1]}, {})
    results = [runner.run("layout", source.encode("utf-8"), {"grid.json": grid_bytes}, seed, eid)
               for seed, eid in enumerate(ids)]
    outs = tuple(sha256_bytes(r.output) if r.output is not None else "" for r in results)
    stderr_sha = sha256_bytes("".join(r.stderr for r in results).encode("utf-8"))
    audit.record("flex.exec.finished", why, {**base, "exec_ids": list(ids)}, {
        "statuses": [r.status for r in results], "output_sha256s": list(outs), "stderr_sha256": stderr_sha,
        "seconds": round(sum(r.seconds for r in results), 3)})
    bad = next((r for r in results if r.status != "ok"), None)
    if any(r.status == "violation" for r in results):
        audit.record("flex.refused", why, base, {"reason": "sandbox_violation", "stderr": results[0].stderr[:200]})
    if bad is not None:
        v = Verified(report={"version": 1, "blocks": [], "warns": [], "info": {}}, table=None)
        v.blocks.append({"code": STATUS_BLOCK.get(bad.status, "limits"), "count": 1, "examples": [],
                         "detail": f"the program run ended as {bad.status}" + (f" (exit {bad.exit_code})"
                                                                              if bad.exit_code else "")})
        v.report["blocks"] = v.blocks
    else:
        assert results[0].output is not None and results[1].output is not None
        v = verify(grid, results[0].output, results[1].output, flex)
    audit.record("flex.verify", why, {**base, "report_sha256": v.report_sha256},
                 {"blocks": [b["code"] for b in v.blocks], "warns": [w["code"] for w in v.warns],
                  "rows_out": len(v.table["rows"]) if v.table else 0})
    return LayoutRun(v, program_sha, grid_sha, runner.image, ids, tuple(r.status for r in results), outs,
                     round(sum(r.seconds for r in results), 3), results[0].stderr[:2000], results[0].output)
