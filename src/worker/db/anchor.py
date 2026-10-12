"""The anchor: the head of every audit stream, written OUTSIDE the database on a schedule.

Why: the triggers and the revoked privileges stop the application and the table owner; a database superuser can
still switch them off and rewrite a whole chain consistently. An anchor written to a sink the database host cannot
alter makes that rewrite detectable (`history_rewritten`) the next time `check` runs. Anchors chain to each other
(`previous_sha256`), so a deleted or replaced anchor is found as well.

* `AnchorSink` is the interface: `put` (create-only), `names`, `read`. There is no delete, ever.
* `DirectorySink` is the implementation shipped here: one file per anchor, created exclusively, never rewritten.
  Point it at an append-only location the database host cannot write (another host's `chattr +a` directory, a
  mounted object-lock bucket); an object-store sink with Object Lock in COMPLIANCE mode implements the same three
  methods. A sink on the database's own disk is a convenience, not a guarantee.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import sqlalchemy as sa

from worker.audit import GENESIS_HASH
from worker.db import schema as S
from worker.db.core import Db, SqlAuditLog
from worker.hashing import canonical_strict

ANCHOR_STREAM = "system/anchors"
DEFAULT_INTERVAL_SECONDS = 15 * 60


class AnchorSink(Protocol):
    def put(self, name: str, data: bytes) -> None:
        """Store `data` under `name`. Creating only: an existing name is an error, not an overwrite."""

    def names(self) -> list[str]:
        """Every anchor name, oldest first."""

    def read(self, name: str) -> bytes: ...


class DirectorySink:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)

    def put(self, name: str, data: bytes) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.dir / name, "xb") as fh:  # exclusive: never overwrites
            fh.write(data)
            fh.flush()

    def names(self) -> list[str]:
        return sorted(p.name for p in self.dir.glob("anchor-*.json")) if self.dir.is_dir() else []

    def read(self, name: str) -> bytes:
        return (self.dir / name).read_bytes()


def heads(db: Db) -> list[dict[str, Any]]:
    """The head (stream, seq, hash) of every stream."""
    t = S.audit_log
    last = sa.select(t.c.stream, sa.func.max(t.c.seq).label("seq")).group_by(t.c.stream).subquery()
    with db.begin() as conn:
        rows = conn.execute(sa.select(t.c.stream, t.c.seq, t.c.hash).join(
            last, sa.and_(t.c.stream == last.c.stream, t.c.seq == last.c.seq)).order_by(t.c.stream)).all()
    return [{"stream": r.stream, "seq": int(r.seq), "hash": r.hash} for r in rows]


def merkle_root(items: list[dict[str, Any]]) -> str:
    level = [hashlib.sha256(canonical_strict(i).encode("utf-8")).hexdigest() for i in items] or [GENESIS_HASH]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [hashlib.sha256((a + b).encode("ascii")).hexdigest() for a, b in zip(level[::2], level[1::2],
                                                                                     strict=True)]
    return level[0]


def build(items: list[dict[str, Any]], previous: str, number: int, now: datetime | None = None) -> dict[str, Any]:
    """The anchor document: pure, so a test can compare it with a database."""
    return {"version": 1, "number": number, "ts": (now or datetime.now(UTC)).isoformat(),
            "previous_sha256": previous, "merkle_root": merkle_root(items), "streams": items}


def sha_of(doc_bytes: bytes) -> str:
    return hashlib.sha256(doc_bytes).hexdigest()


def _last(sink: AnchorSink) -> tuple[int, str]:
    names = sink.names()
    if not names:
        return 0, GENESIS_HASH
    raw = sink.read(names[-1])
    return int(json.loads(raw)["number"]), sha_of(raw)


def write(db: Db, sink: AnchorSink, now: datetime | None = None) -> dict[str, Any]:
    """Write one anchor to the sink and note its digest in the chained stream `system/anchors`. The sink is
    written first: an anchor that exists outside the database counts even if the note fails."""
    number, previous = _last(sink)
    doc = build(heads(db), previous, number + 1, now)
    raw = canonical_strict(doc).encode("utf-8")
    name = f"anchor-{doc['number']:010d}.json"
    sink.put(name, raw)
    SqlAuditLog(db, ANCHOR_STREAM).record(
        "anchor.written", f"anchor {doc['number']} of {len(doc['streams'])} stream heads",
        {"name": name, "number": doc["number"]}, {"sha256": sha_of(raw), "merkle_root": doc["merkle_root"]})
    return doc


def check(db: Db, sink: AnchorSink, *, max_age_seconds: float | None = None,
          now: datetime | None = None) -> list[dict[str, Any]]:
    """Compare the database with every anchor. A problem is {code, ...}:

    * `history_rewritten`: an anchored (stream, seq, hash) has a different hash now, or is missing from inside
      the stream (a row removed);
    * `behind_anchor`: the stream ends before an anchored entry: rows were removed from its end, or a backup older
      than the anchor was restored (the restore drill expects the second and says so);
    * `anchor_chain_broken`: an anchor does not chain to the one before, or its number is out of order;
    * `anchor_unreadable`: an anchor file is not a valid document;
    * `anchor_stale`: the newest anchor is older than `max_age_seconds`; or there is none and the oldest audit
      entry is already older than that (a new system has not had a chance to anchor yet).
    """
    problems: list[dict[str, Any]] = []
    t = S.audit_log
    previous, expected, newest = GENESIS_HASH, 1, None
    for name in sink.names():
        raw = sink.read(name)
        try:
            doc = json.loads(raw)
            streams, number = doc["streams"], int(doc["number"])
        except (ValueError, KeyError, TypeError):
            problems.append({"code": "anchor_unreadable", "anchor": name})
            continue
        if doc.get("previous_sha256") != previous or number != expected:
            problems.append({"code": "anchor_chain_broken", "anchor": name})
        previous, expected, newest = sha_of(raw), number + 1, doc.get("ts")
        with db.begin() as conn:
            for s in streams:
                row = conn.execute(sa.select(t.c.hash).where(t.c.stream == s["stream"], t.c.seq == s["seq"])).first()
                if row is not None and row[0] == s["hash"]:
                    continue
                head = conn.execute(sa.select(sa.func.max(t.c.seq)).where(t.c.stream == s["stream"])).scalar_one()
                behind = row is None and (head is None or s["seq"] > head)
                problems.append({"code": "behind_anchor" if behind else "history_rewritten", "anchor": name,
                                 "stream": s["stream"], "seq": s["seq"], "present": row is not None,
                                 **({"entries_behind": s["seq"] - int(head or 0)} if behind else {})})
    if max_age_seconds is not None:
        with db.begin() as conn:
            oldest = conn.execute(sa.select(sa.func.min(t.c.ts))).scalar_one()
        since = newest or oldest  # no anchor yet: measure from the first thing there is to anchor
        age = ((now or datetime.now(UTC)) - datetime.fromisoformat(since)).total_seconds() if since else None
        if age is not None and age > max_age_seconds:
            problems.append({"code": "anchor_stale", "age_seconds": age})
    return problems


def follow(db: Db, sink: AnchorSink, *, interval: float, stop: threading.Event,
           on_error: Callable[[Exception], None] | None = None) -> None:
    """Write an anchor now and then every `interval` seconds until `stop`. A failed write is reported and tried
    again at the next tick; nothing is skipped silently."""
    while not stop.is_set():
        try:
            write(db, sink)
        except Exception as exc:  # noqa: BLE001  an unwritable sink must be loud, not fatal to the loop
            if on_error is not None:
                on_error(exc)
        stop.wait(interval)
