"""The layout-program flow around the engine: propose, approve, reuse, replay, and the `derived` source.

* `Flex.propose` runs a program in the sandbox (twice), verifies it and keeps every input and output as a blob; it
  changes nothing a job can see.
* `Flex.approve` is the human's click. It is bound to the three hashes the person saw (grid, table, report); a
  stale report, an agent principal, an open BLOCK or an unticked WARN is refused. Only then is the artifact created
  and the approval recorded, each audited first.
* `Flex.reuse` offers an artifact only on an exact shape match within one sponsor and record type, and applies it
  with one click each time: a clean report is that click's approval, anything else opens the full card.
* `DerivedReader` is the engine's `Reader` for a `derived` source: it plays the approved table back from the blob
  store (no Docker, no program).
"""

from __future__ import annotations

import csv
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from worker.contracts import is_agent_principal
from worker.engine import Refused, SourceRef
from worker.flex.artifacts import Artifact, Artifacts, sha256_text
from worker.flex.derive import run_layout
from worker.flex.grid import canonical_json, layout_sig
from worker.flex.runner import Runner
from worker.flex.verify import FLEX_DEFAULTS, Verified
from worker.replay import BlobStore, ReplayError

HARNESS = "layout-harness/1"
LIMITS = {"cpu": 1, "memory_mb": 512, "pids": 64, "seconds": 60, "max_rows_out": FLEX_DEFAULTS["max_rows_out"]}
BOUND = ("input_grid_sha256", "output_table_sha256", "verification_report_sha256")


class Flex:
    def __init__(self, sponsor: str, artifacts: Artifacts, blobs: BlobStore, runner: Runner | None, audit: Any, *,
                 controls_hash: str = "", flex: Mapping[str, int] | None = None) -> None:
        self.sponsor, self.artifacts, self.blobs, self.runner = sponsor, artifacts, blobs, runner
        self.audit, self.controls_hash, self.flex = audit, controls_hash, dict(flex or {})

    # ---- refusals are audited, then raised ------------------------------------------------------------
    def _refuse(self, code: str, sentence: str, why: str, **subject: Any) -> Refused:
        self.audit.record("flex.refused", why or code, {"sponsor": self.sponsor, **subject}, {"reason": code})
        return Refused(code, sentence)

    def _sandbox(self, why: str) -> Runner:
        if self.runner is None:
            raise self._refuse("no_sandbox", "Reshaping needs the sandbox, which is not running. Enter the values "
                               "by hand or raise a ticket; there is no fallback to an unsandboxed run.", why)
        return self.runner

    # ---- propose ------------------------------------------------------------------------------------------
    def propose(self, record_type: str, grid: Mapping[str, Any], program: str, why: str, *,
                proposed_by: Mapping[str, Any] | None = None, artifact: Artifact | None = None) -> dict[str, Any]:
        """Run and verify; returns the proposal record (`proposal_id` included) and the Verified as `verified`."""
        runner = self._sandbox(why)
        run = run_layout(runner=runner, audit=self.audit, grid=grid, program=program, why=why, flex=self.flex,
                         subject={"sponsor": self.sponsor, "record_type": record_type})
        v = run.verified
        schema = [[c["name"], c["role"]] for c in (v.table or {}).get("columns", [])]
        if artifact is not None and v.table is not None and schema != [list(c) for c in artifact.output_schema]:
            v.blocks.append({"code": "schema_drift", "count": 1, "examples": [],
                             "detail": "the output columns differ from the artifact's output_schema"})
            v.table = None
        rec: dict[str, Any] = {
            "sponsor": self.sponsor, "record_type": record_type, "artifact_id": artifact.artifact_id if artifact else "",
            "program_sha256": self.blobs.put(program.replace("\r\n", "\n").encode("utf-8")),
            "input_grid_sha256": self.blobs.put(canonical_json(grid)),
            "output_raw_sha256": self.blobs.put(run.raw_output) if run.raw_output is not None else "",
            "output_table_sha256": self.blobs.put(canonical_json(v.table)) if v.table is not None else "",
            "verification_report_sha256": self.blobs.put(canonical_json(v.report)),
            "image_digest": run.image, "input_sig": layout_sig(grid), "output_schema": schema,
            "case": v.report.get("info", {}).get("case", "other"), "exec_ids": list(run.exec_ids),
            "blocks": [b["code"] for b in v.blocks], "warns": [w["code"] for w in v.warns],
            "proposed_by": dict(proposed_by or {})}
        rec["proposal_id"] = self.blobs.put(canonical_json(rec))
        return {**rec, "verified": v}

    # ---- approve ------------------------------------------------------------------------------------------
    def _proposal(self, proposal_id: str, why: str) -> dict[str, Any]:
        try:
            rec = self.blobs.get_json(proposal_id)
        except ReplayError as exc:
            raise self._refuse("proposal_missing", "no such proposal.", why, proposal=proposal_id[:12]) from exc
        if rec.get("sponsor") != self.sponsor:
            raise self._refuse("proposal_missing", "no such proposal for this sponsor.", why)
        return rec

    def approve(self, proposal_id: str, *, by: str, scope: str, bound: Mapping[str, str],
                acknowledged: Sequence[str], reason: str) -> dict[str, Any]:
        """The click. Returns {artifact_id, approval_id, table_sha256, source}: `source` is the derived SourceRef."""
        rec = self._proposal(proposal_id, reason)
        if is_agent_principal(by):
            raise self._refuse("agent_cannot_decide", f"{by!r} is not a person.", reason, proposal=proposal_id[:12])
        if rec["blocks"] or not rec["output_table_sha256"]:
            raise self._refuse("blocked", "a BLOCK is open; no one can approve this, a controller included.", reason,
                               codes=rec["blocks"])
        if sorted(set(rec["warns"]) - set(acknowledged)):
            raise self._refuse("warn_unacknowledged", "every WARN must be ticked: "
                               + ", ".join(sorted(set(rec["warns"]) - set(acknowledged))), reason)
        if any(bound.get(k) != rec[k] for k in BOUND):
            raise self._refuse("stale_approval", "the click was bound to a different grid, table or report than "
                               "the one on record; open the card again.", reason, proposal=proposal_id[:12])
        if rec["artifact_id"]:
            aid = self.artifacts.load(self.sponsor, rec["artifact_id"]).artifact_id
        else:
            src = self.blobs.get(rec["program_sha256"]).decode("utf-8")
            aid = self.artifacts.create(Artifact(
                sponsor=self.sponsor, record_type=rec["record_type"], harness=HARNESS,
                image_digest=rec["image_digest"], source_sha256=sha256_text(src), input_sig=rec["input_sig"],
                output_schema=tuple((n, r) for n, r in rec["output_schema"]), controls_hash=self.controls_hash,
                limits=LIMITS, source_text=src, case=rec["case"], proposed_by=rec["proposed_by"]), reason)
        ap = self.artifacts.approve(self.sponsor, aid, by=by, scope=scope, bound={k: rec[k] for k in BOUND},
                                    acknowledged=acknowledged, reason=reason)
        self._register(rec, aid, ap["approval_id"])
        return {"artifact_id": aid, "approval_id": ap["approval_id"], "table_sha256": rec["output_table_sha256"],
                "source": derived_ref(rec["output_table_sha256"])}

    def _register(self, rec: Mapping[str, Any], artifact_id: str, approval_id: str) -> None:
        d = self.blobs.root / "derived"
        d.mkdir(parents=True, exist_ok=True)
        doc = {"artifact_id": artifact_id, "approval_id": approval_id, "sponsor": self.sponsor,
               **{k: rec[k] for k in ("program_sha256", "input_grid_sha256", "output_raw_sha256",
                                      "output_table_sha256", "verification_report_sha256", "image_digest",
                                      "exec_ids")}}
        p = d / derivation_name(rec["output_table_sha256"], approval_id)
        if not p.exists():
            p.write_text(canonical_json(doc).decode("utf-8"), encoding="utf-8")

    # ---- reuse --------------------------------------------------------------------------------------------
    def offer(self, record_type: str, grid: Mapping[str, Any], why: str = "a file arrived") -> list[Artifact]:
        """Approved artifacts of this sponsor and record type with exactly this shape; none from anywhere else."""
        heads = self.artifacts.heads(self.sponsor, record_type, layout_sig(grid))
        for h in heads:
            self.audit.record("flex.reuse.offered", why, {"sponsor": self.sponsor, "artifact_id": h.artifact_id,
                                                          "input_sig": h.input_sig}, {"heads": len(heads)})
        return heads

    def reuse(self, artifact_id: str, record_type: str, grid: Mapping[str, Any], *, by: str, why: str
              ) -> dict[str, Any]:
        """One click: run the saved program on the new grid under the whole suite. status: `approved` (clean: this
        click is the approval, scope job), `needs_card` (WARNs: approve the proposal with `approve`) or `failed`."""
        if is_agent_principal(by):
            raise self._refuse("agent_cannot_decide", f"{by!r} is not a person.", why, artifact_id=artifact_id)
        runner = self._sandbox(why)
        heads = {h.artifact_id: h for h in self.artifacts.heads(self.sponsor, record_type, layout_sig(grid))}
        a = heads.get(artifact_id)
        if a is None:
            raise self._refuse("not_offered", "that artifact is not offered for a file of this shape.", why,
                               artifact_id=artifact_id)
        if a.image_digest != runner.image:
            raise self._refuse("image_mismatch", "the saved program was approved in another sandbox image.", why,
                               artifact_id=artifact_id, approved=a.image_digest, running=runner.image)
        p = self.propose(record_type, grid, a.source_text, why, artifact=a)
        v: Verified = p["verified"]
        if v.blocks:
            self.audit.record("flex.reuse.failed", why, {"sponsor": self.sponsor, "artifact_id": a.artifact_id,
                                                         "input_sig": a.input_sig}, {"blocks": p["blocks"]})
            return {"status": "failed", "proposal_id": p["proposal_id"], "blocks": v.blocks, "warns": v.warns}
        if v.warns:
            return {"status": "needs_card", "proposal_id": p["proposal_id"], "blocks": [], "warns": v.warns}
        self.audit.record("flex.reuse.accepted", why, {"sponsor": self.sponsor, "artifact_id": a.artifact_id,
                                                       "input_grid_sha256": p["input_grid_sha256"]}, {})
        done = self.approve(p["proposal_id"], by=by, scope="job", bound={k: p[k] for k in BOUND}, acknowledged=(),
                            reason=why)
        return {"status": "approved", "proposal_id": p["proposal_id"], "blocks": [], "warns": [], **done}


def derivation_name(table_sha256: str, approval_id: str) -> str:
    """The file a derivation is kept in: short, because the full hashes (140 characters) overflow a Windows path in a
    deep folder. The hashes stay whole inside the file, and `derivations_of` checks them there."""
    return f"{table_sha256[:16]}.{approval_id[:24]}.json"


def derivations_of(blobs: BlobStore, table_sha256: str) -> list[dict[str, Any]]:
    """Every approved derivation of this table: the short names and the long names written before them."""
    root = blobs.root / "derived"
    found = sorted({*root.glob(f"{table_sha256[:16]}.*.json"), *root.glob(f"{table_sha256}.*.json")})
    docs = [json.loads(p.read_text("utf-8")) for p in found]
    return [d for d in docs if d.get("output_table_sha256") == table_sha256]


def derived_ref(table_sha256: str) -> SourceRef:
    return SourceRef("derived", table_sha256, None, 1, table_sha256)


# ---- the engine's reader for a derived source -------------------------------------------------------------
class DerivedReader:
    """`Reader` over the approved tables in `blobs`; any other source goes to `inner`. The table is read from its
    blob (digest checked on every read): nothing runs, so replay needs neither Docker nor the program."""

    def __init__(self, inner: Any, blobs: BlobStore, cache: str | Path | None = None) -> None:
        self.inner, self.blobs = inner, blobs
        self.cache = Path(cache) if cache else blobs.root / "derived-csv"

    def __getattr__(self, name: str) -> Any:  # roots, pulled, ... of the wrapped reader
        return getattr(self.inner, name)

    def _table(self, ref: SourceRef) -> dict[str, Any]:
        try:
            return self.blobs.get_json(ref.ref)
        except ReplayError as exc:
            raise OSError(str(exc)) from exc

    def fingerprint(self, ref: SourceRef) -> str:
        if ref.kind != "derived":
            return self.inner.fingerprint(ref)
        self._table(ref)  # present, and its bytes still hash to the name
        return ref.ref

    def path(self, ref: SourceRef) -> Path:
        if ref.kind != "derived":
            return self.inner.path(ref)
        p = self.cache / f"{ref.ref}.csv"
        if not p.is_file():
            t = self._table(ref)
            self.cache.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            with open(tmp, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow([c["name"] for c in t["columns"]])
                w.writerows(t["rows"])
            os.replace(tmp, p)
        return p

    def rows(self, ref: SourceRef) -> Any:
        if ref.kind != "derived":
            yield from self.inner.rows(ref)
            return
        for n, row in enumerate(self._table(ref)["rows"], start=2):
            if any(str(v).strip() for v in row):
                yield n, list(row)

    def derivation(self, ref: SourceRef) -> dict[str, Any] | None:
        found = derivations_of(self.blobs, ref.ref)
        return found[0] if found else None
