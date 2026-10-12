"""Transform artifacts: a sponsor's approved, sandboxed layout programs, stored where nothing can edit them.

* An artifact's id is the sha256 of its identity (sponsor, record type, language, harness, image digest, source
  sha, input shape, output schema, controls hash, limits, parent). An edit is a child with `parent`.
* Approvals and retirements are separate append-only records that name the artifact; the artifact never changes.
* `Artifacts` is the only writer. It records in the audit log FIRST and stores second: a row that cannot be
  audited is never stored. Every load re-hashes; a mismatch is never run.
* The port (`ArtifactStore`) has insert and read methods only: there is no update and no delete. The SQL table
  refuses both in the database (migration 0003); the file adapter creates exclusively and sets the file read-only.
* Code lives here and in the engine, never in skills: the file adapter refuses a root under a `skills` folder.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import sqlalchemy as sa

from worker.contracts import is_agent_principal
from worker.engine import Refused
from worker.hashing import canonical_strict

SCOPES = ("job", "sponsor")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,59}$")
_ID = re.compile(r"^(ta|tap|tar)_[0-9a-f]{64}$")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Artifact:
    sponsor: str
    record_type: str
    harness: str
    image_digest: str
    source_sha256: str
    input_sig: str
    output_schema: tuple[tuple[str, str], ...]
    controls_hash: str
    limits: Mapping[str, Any]
    source_text: str
    parent: str = ""
    case: str = "other"  # a label for promotion and the card; not identity
    proposed_by: Mapping[str, Any] = field(default_factory=dict)  # not identity
    language: str = "python3.12"
    purpose: str = "layout"

    def identity(self) -> dict[str, Any]:
        return {"sponsor": self.sponsor, "record_type": self.record_type, "purpose": self.purpose,
                "language": self.language, "harness": self.harness, "image_digest": self.image_digest,
                "source_sha256": self.source_sha256, "input_sig": self.input_sig,
                "output_schema": [list(c) for c in self.output_schema], "controls_hash": self.controls_hash,
                "limits": dict(self.limits), "parent": self.parent}

    @property
    def artifact_id(self) -> str:
        return "ta_" + hashlib.sha256(canonical_strict(self.identity()).encode("utf-8")).hexdigest()

    def record(self) -> dict[str, Any]:
        return {"schema_version": 1, "kind": "transform_artifact", "artifact_id": self.artifact_id,
                "identity": self.identity(), "source_text": self.source_text, "case": self.case,
                "proposed_by": dict(self.proposed_by)}

    @classmethod
    def from_record(cls, doc: Mapping[str, Any]) -> Artifact:
        i = doc["identity"]
        return cls(sponsor=i["sponsor"], record_type=i["record_type"], harness=i["harness"],
                   image_digest=i["image_digest"], source_sha256=i["source_sha256"], input_sig=i["input_sig"],
                   output_schema=tuple((c[0], c[1]) for c in i["output_schema"]), controls_hash=i["controls_hash"],
                   limits=i["limits"], source_text=doc["source_text"], parent=i["parent"], case=doc.get("case", ""),
                   proposed_by=doc.get("proposed_by", {}), language=i["language"], purpose=i["purpose"])


# ---- the port and its two adapters ---------------------------------------------------------------------
class ArtifactStore(Protocol):
    """Insert and read. Nothing here changes or removes a record."""

    def insert_artifact(self, record: dict[str, Any]) -> None: ...
    def insert_decision(self, sponsor: str, decision: dict[str, Any]) -> None: ...
    def get_artifact(self, sponsor: str, artifact_id: str) -> dict[str, Any] | None: ...
    def artifacts(self, sponsor: str, record_type: str) -> list[dict[str, Any]]: ...
    def decisions(self, sponsor: str, artifact_id: str) -> list[dict[str, Any]]: ...


def _check(sponsor: str, artifact_id: str = "") -> None:
    if not _SLUG.match(sponsor) or (artifact_id and not _ID.match(artifact_id)):
        raise ValueError(f"bad sponsor or artifact id: {sponsor!r}, {artifact_id!r}")


class FileArtifactStore:
    """`<root>/<sponsor>/artifacts/<id>.json` and `<root>/<sponsor>/decisions/<id>.json`, each created
    exclusively and made read-only."""

    def __init__(self, root: str | Path) -> None:
        if "skills" in Path(root).parts:
            raise ValueError("transform artifacts are never stored under skills")
        self.root = Path(root)

    def _put(self, path: Path, doc: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "x", encoding="utf-8") as fh:  # FileExistsError: no in-place edit
            fh.write(canonical_strict(doc))
            fh.flush()
            os.fsync(fh.fileno())
        path.chmod(0o444)

    def insert_artifact(self, record: dict[str, Any]) -> None:
        sponsor, aid = record["identity"]["sponsor"], record["artifact_id"]
        _check(sponsor, aid)
        self._put(self.root / sponsor / "artifacts" / f"{aid}.json", record)

    def insert_decision(self, sponsor: str, decision: dict[str, Any]) -> None:
        did = decision.get("approval_id") or decision["retirement_id"]
        _check(sponsor, did)
        self._put(self.root / sponsor / "decisions" / f"{did}.json", decision)

    def get_artifact(self, sponsor: str, artifact_id: str) -> dict[str, Any] | None:
        _check(sponsor, artifact_id)
        p = self.root / sponsor / "artifacts" / f"{artifact_id}.json"
        return json.loads(p.read_text("utf-8")) if p.is_file() else None

    def _all(self, sponsor: str, kind: str) -> list[dict[str, Any]]:
        _check(sponsor)
        d = self.root / sponsor / kind
        return [json.loads(p.read_text("utf-8")) for p in sorted(d.glob("*.json"))] if d.is_dir() else []

    def artifacts(self, sponsor: str, record_type: str) -> list[dict[str, Any]]:
        return [r for r in self._all(sponsor, "artifacts") if r["identity"]["record_type"] == record_type]

    def decisions(self, sponsor: str, artifact_id: str) -> list[dict[str, Any]]:
        return [d for d in self._all(sponsor, "decisions") if d["artifact_id"] == artifact_id]


class SqlArtifactStore:
    """The same over `flex_artifacts` and `flex_decisions`: INSERT-only tables (migration 0003 adds the triggers
    and, on PostgreSQL, the revoked privileges)."""

    def __init__(self, db: Any) -> None:
        from worker.db import schema as S

        self.db, self._a, self._d = db, S.flex_artifacts, S.flex_decisions

    def insert_artifact(self, record: dict[str, Any]) -> None:
        i = record["identity"]
        with self.db.begin() as conn:
            conn.execute(self._a.insert(), {
                "sponsor": i["sponsor"], "artifact_id": record["artifact_id"], "record_type": i["record_type"],
                "input_sig": i["input_sig"], "parent": i["parent"], "doc": canonical_strict(record)})

    def insert_decision(self, sponsor: str, decision: dict[str, Any]) -> None:
        did = decision.get("approval_id") or decision["retirement_id"]
        with self.db.begin() as conn:
            conn.execute(self._d.insert(), {"sponsor": sponsor, "decision_id": did,
                                            "artifact_id": decision["artifact_id"], "doc": canonical_strict(decision)})

    def get_artifact(self, sponsor: str, artifact_id: str) -> dict[str, Any] | None:
        with self.db.begin() as conn:
            row = conn.execute(sa.select(self._a.c.doc).where(self._a.c.sponsor == sponsor,
                                                              self._a.c.artifact_id == artifact_id)).first()
        return json.loads(row[0]) if row else None

    def artifacts(self, sponsor: str, record_type: str) -> list[dict[str, Any]]:
        with self.db.begin() as conn:
            rows = conn.execute(sa.select(self._a.c.doc).where(
                self._a.c.sponsor == sponsor, self._a.c.record_type == record_type).order_by(self._a.c.artifact_id))
        return [json.loads(r[0]) for r in rows]

    def decisions(self, sponsor: str, artifact_id: str) -> list[dict[str, Any]]:
        with self.db.begin() as conn:
            rows = conn.execute(sa.select(self._d.c.doc).where(
                self._d.c.sponsor == sponsor, self._d.c.artifact_id == artifact_id).order_by(self._d.c.decision_id))
        return [json.loads(r[0]) for r in rows]


# ---- the service: audit first, then store ---------------------------------------------------------------
class Artifacts:
    def __init__(self, store: ArtifactStore, audit: Any) -> None:
        self.store, self.audit = store, audit

    def create(self, a: Artifact, reason: str) -> str:
        """Audit `flex.artifact.created`, then store. The same artifact again is the same id and no new record."""
        if sha256_text(a.source_text) != a.source_sha256:
            raise Refused("artifact_invalid", "the source text does not match its source_sha256.")
        if self.store.get_artifact(a.sponsor, a.artifact_id) is not None:
            return a.artifact_id
        if a.parent and self.store.get_artifact(a.sponsor, a.parent) is None:
            raise Refused("artifact_invalid", "the parent artifact is not one of this sponsor's.")
        self.audit.record("flex.artifact.created", reason, {
            "artifact_id": a.artifact_id, "sponsor": a.sponsor, "record_type": a.record_type, "parent": a.parent,
            "source_sha256": a.source_sha256, "image_digest": a.image_digest, "input_sig": a.input_sig,
            "case": a.case, "proposed_by": dict(a.proposed_by)}, {"record_sha256": hashlib.sha256(
                canonical_strict(a.record()).encode("utf-8")).hexdigest()})
        self.store.insert_artifact(a.record())
        return a.artifact_id

    def load(self, sponsor: str, artifact_id: str) -> Artifact:
        """The artifact, after re-hashing identity and source. Another sponsor's, or a missing one, is refused."""
        doc = self.store.get_artifact(sponsor, artifact_id)
        if doc is None:
            raise Refused("artifact_missing", f"no artifact {artifact_id[:12]} for sponsor {sponsor}.")
        try:
            a = Artifact.from_record(doc)
            ok = a.artifact_id == artifact_id == doc["artifact_id"] and sha256_text(a.source_text) == a.source_sha256 \
                and a.sponsor == sponsor
        except (KeyError, TypeError, ValueError):
            ok = False
        if not ok:
            self.audit.record("flex.artifact.tampered", "an artifact failed its hash check on load",
                              {"artifact_id": artifact_id, "sponsor": sponsor}, {})
            raise Refused("artifact_tampered", f"artifact {artifact_id[:12]} does not match its hash; never run.")
        return a

    def _decide(self, sponsor: str, artifact_id: str, by: str, action: str, body: dict[str, Any], id_key: str,
                id_prefix: str, reason: str) -> dict[str, Any]:
        if is_agent_principal(by):
            raise Refused("agent_cannot_decide", f"{by!r} is not a person; only a human decides on an artifact.")
        if not reason.strip():
            raise Refused("reason_required", "a reason is required.")
        self.load(sponsor, artifact_id)
        rec = {"artifact_id": artifact_id, "sponsor": sponsor, "by": by.strip(), "reason": reason[:500], **body}
        rec[id_key] = id_prefix + hashlib.sha256(canonical_strict(rec).encode("utf-8")).hexdigest()
        self.audit.record(action, reason, {k: v for k, v in rec.items() if k != "reason"}, {})
        # the same person deciding the same thing again (the same program on the same grid, table and report)
        # has the same content id: it is already on record, so there is nothing more to store
        if not any(d.get(id_key) == rec[id_key] for d in self.store.decisions(sponsor, artifact_id)):
            self.store.insert_decision(sponsor, rec)
        return rec

    def approve(self, sponsor: str, artifact_id: str, *, by: str, scope: str, bound: Mapping[str, str],
                acknowledged: Sequence[str], reason: str) -> dict[str, Any]:
        if scope not in SCOPES:
            raise Refused("bad_scope", f"scope must be one of {SCOPES}.")
        return self._decide(sponsor, artifact_id, by, "flex.artifact.approved", {
            "kind": "approval", "scope": scope, "bound": dict(bound), "acknowledged": sorted(acknowledged)},
            "approval_id", "tap_", reason)

    def retire(self, sponsor: str, artifact_id: str, *, by: str, reason: str, replacement: str = "") -> dict[str, Any]:
        return self._decide(sponsor, artifact_id, by, "flex.artifact.retired", {
            "kind": "retirement", "replacement": replacement}, "retirement_id", "tar_", reason)

    def heads(self, sponsor: str, record_type: str, input_sig: str) -> list[Artifact]:
        """What may be offered for a file of exactly this shape: not retired, approved for the sponsor, and the end
        of its chain (nothing was derived from it, retired or not). Two heads are a fork, both returned."""
        docs = self.store.artifacts(sponsor, record_type)
        parents = {d["identity"]["parent"] for d in docs}
        out: list[Artifact] = []
        for d in docs:
            if d["identity"]["input_sig"] != input_sig or d["artifact_id"] in parents:
                continue
            kinds = self.store.decisions(sponsor, d["artifact_id"])
            if any(k["kind"] == "retirement" for k in kinds):
                continue
            if any(k["kind"] == "approval" and k["scope"] == "sponsor" for k in kinds):
                out.append(self.load(sponsor, d["artifact_id"]))
        return out
