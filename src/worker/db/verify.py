"""Verification of the database's audit chains: every stream, from the stored text, outside the application path.

`verify_streams` recomputes every hash and link of each stream (in full, or from a head verified earlier), and
compares the chain with the anchors written outside the database (`worker.db.anchor`). It only reads. Its own
result is appended to a chained stream (`system/verification`) so the checks themselves are on the record.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa

from worker.audit import GENESIS_HASH
from worker.db import schema as S
from worker.db.core import Db, SqlAuditLog, streams

VERIFICATION_STREAM = "system/verification"
_PAGE = 5000


@dataclass
class StreamResult:
    stream: str
    checked: int = 0  # entries verified now, from `start` on
    head_seq: int = 0
    head_hash: str = GENESIS_HASH
    problem: dict[str, Any] | None = None


@dataclass
class Report:
    streams: list[StreamResult] = field(default_factory=list)
    anchor_problems: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.anchor_problems and all(r.problem is None for r in self.streams)

    def summary(self) -> dict[str, Any]:
        return {"ok": self.ok, "streams": len(self.streams), "entries": sum(r.checked for r in self.streams),
                "broken": [{"stream": r.stream, **(r.problem or {})} for r in self.streams if r.problem],
                "anchor_problems": self.anchor_problems}


def verify_stream(db: Db, stream: str, *, after_seq: int = 0, after_hash: str = GENESIS_HASH) -> StreamResult:
    """Walk `stream` in pages from just after (`after_seq`, `after_hash`); report the first row whose text, hash,
    sequence, link or columns are wrong. Nothing is loaded whole."""
    t = S.audit_log
    result = StreamResult(stream, head_seq=after_seq, head_hash=after_hash)
    seq, prev = after_seq, after_hash
    while True:
        with db.begin() as conn:
            rows = conn.execute(sa.select(t).where(t.c.stream == stream, t.c.seq > seq).order_by(t.c.seq)
                                .limit(_PAGE)).all()
        if not rows:
            return result
        for r in rows:
            position = {"line": int(r.seq), "seq": int(r.seq)}
            try:
                body = json.loads(r.body)
            except ValueError:
                result.problem = {**position, "reason": "line is not a valid audit record"}
                return result
            if r.seq != seq + 1:
                result.problem = {**position, "reason": f"sequence {r.seq} where {seq + 1} was expected"}
                return result
            if (body.get("seq"), body.get("prev_hash"), body.get("action")) != (r.seq, r.prev_hash, r.action):
                result.problem = {**position, "reason": "columns disagree with the stored text"}
                return result
            if r.prev_hash != prev:
                result.problem = {**position, "reason": "chain broken: prev_hash does not match"}
                return result
            if r.hash != hashlib.sha256(r.body.encode("utf-8")).hexdigest():
                result.problem = {**position, "reason": "record was altered: hash does not match"}
                return result
            seq, prev = int(r.seq), str(r.hash)
            result.checked += 1
            result.head_seq, result.head_hash = seq, prev


def verify_streams(db: Db, *, heads: dict[str, tuple[int, str]] | None = None,
                   only: Iterable[str] | None = None) -> Report:
    """Verify every stream (or `only`). `heads` maps a stream to a head verified before: the walk starts there."""
    report = Report()
    for name in sorted(only) if only is not None else streams(db):
        after = (heads or {}).get(name, (0, GENESIS_HASH))
        report.streams.append(verify_stream(db, name, after_seq=after[0], after_hash=after[1]))
    return report


def record(db: Db, report: Report, *, kind: str) -> None:
    """Append the outcome to the verifier's own chained stream."""
    summary = report.summary()
    SqlAuditLog(db, VERIFICATION_STREAM).record(
        "audit.verified" if report.ok else "audit.verification_failed",
        f"{kind} verification of {summary['streams']} streams, {summary['entries']} entries",
        {"kind": kind, "streams": summary["streams"]},
        {k: summary[k] for k in ("ok", "entries", "broken", "anchor_problems")})


def follow(db: Db, *, interval: float, full_every: int, stop: threading.Event,
           check_anchors: Callable[[], list[dict[str, Any]]] | None = None,
           on_report: Callable[[Report], None] | None = None) -> None:
    """Incremental verification every `interval` seconds and a full one every `full_every` rounds, until `stop`.
    A break is reported through `on_report` (the caller alerts and `/readyz` goes 503); it is never repaired."""
    heads: dict[str, tuple[int, str]] = {}
    rounds = 0
    while not stop.is_set():
        full = rounds % max(1, full_every) == 0
        report = verify_streams(db, heads=None if full else heads)
        if check_anchors is not None:
            report.anchor_problems = check_anchors()
        if report.ok:
            heads.update({r.stream: (r.head_seq, r.head_hash) for r in report.streams})
        record(db, report, kind="full" if full else "incremental")
        if on_report is not None:
            on_report(report)
        rounds += 1
        stop.wait(interval)

