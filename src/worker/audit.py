"""Append-only, hash-chained audit log (one JSON object per line)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from worker.hashing import canonical_strict

MAX_PAYLOAD_BYTES = 20_000
GENESIS_HASH = "0" * 64


class AuditError(Exception):
    """The audit record could not be written; the action must not happen."""


def _entry_hash(entry: dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(canonical_strict(body).encode("utf-8")).hexdigest()


def _bounded(payload: dict | None, name: str) -> dict:
    if payload is None:
        return {}
    try:
        raw = canonical_strict(payload).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AuditError(f"{name} is not JSON-serialisable: {exc}") from exc
    if len(raw) > MAX_PAYLOAD_BYTES:
        return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "oversize": True}
    return json.loads(raw)


def chain_entries(seq: int, prev_hash: str, action: str, items: list[tuple[str, dict | None, dict | None]]
                  ) -> list[dict[str, Any]]:
    """The entries that follow (seq, prev_hash): numbered, timestamped, bounded and hash-chained. Shared by the file
    log and the SQL log, so both hash exactly the same text."""
    entries: list[dict[str, Any]] = []
    for rationale, inputs, outputs in items:
        if not str(rationale).strip():
            raise AuditError(f"a reason is required for {action}; the action is refused")
        seq += 1
        entry: dict[str, Any] = {
            "seq": seq,
            "ts": datetime.now(UTC).isoformat(),
            "action": action,
            "rationale": rationale,
            "inputs": _bounded(inputs, "inputs"),
            "outputs": _bounded(outputs, "outputs"),
            "prev_hash": prev_hash,
        }
        entry["hash"] = prev_hash = _entry_hash(entry)
        entries.append(entry)
    return entries


def first_break_of(rows: Iterable[tuple[int, dict[str, Any] | None]]) -> tuple[int, dict[str, Any] | None]:
    """(entries that verified, the first problem or None) over (position, entry) pairs in order; an entry that is
    not a record is None. A problem is {line, seq, reason}."""
    prev = GENESIS_HASH
    checked = 0
    for number, entry in rows:
        try:
            if entry is None:
                raise ValueError
            seq = entry["seq"]
        except (ValueError, KeyError, TypeError):
            return checked, {"line": number, "seq": None, "reason": "line is not a valid audit record"}
        if seq != checked + 1:
            return checked, {"line": number, "seq": seq, "reason": f"sequence {seq} where {checked + 1} was expected"}
        if entry.get("prev_hash") != prev:
            return checked, {"line": number, "seq": seq, "reason": "chain broken: prev_hash does not match"}
        if entry.get("hash") != _entry_hash(entry):
            return checked, {"line": number, "seq": seq, "reason": "record was altered: hash does not match"}
        prev = entry["hash"]
        checked += 1
    return checked, None


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        entries: list[dict] = []
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    entries.append(json.loads(line))
        return entries

    def _tail(self) -> tuple[int, str]:
        """(seq, hash) of the last entry, read from the end of the file (not the whole log)."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return 0, GENESIS_HASH
        with open(self.path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            size = min(end, 8192)
            while True:
                fh.seek(end - size)
                chunk = fh.read(size)
                lines = [ln for ln in chunk.split(b"\n") if ln.strip()]
                if len(lines) >= 2 or size == end:
                    break
                size = min(end, size * 4)
        last = json.loads(lines[-1])
        return last["seq"], last["hash"]

    def record(
        self,
        action: str,
        rationale: str,
        inputs: dict | None = None,
        outputs: dict | None = None,
    ) -> dict:
        return self.record_many(action, [(rationale, inputs, outputs)])[0]

    def record_many(
        self, action: str, items: list[tuple[str, dict | None, dict | None]]
    ) -> list[dict]:
        """Append several entries of one action in a single locked write (one fsync); chained as usual."""
        for rationale, _inputs, _outputs in items:
            if not str(rationale).strip():
                raise AuditError(f"a reason is required for {action}; the action is refused")
        with self._lock:
            try:
                seq, prev_hash = self._tail()
            except (OSError, ValueError, KeyError) as exc:
                raise AuditError(f"cannot read audit log: {exc}") from exc
            entries = chain_entries(seq, prev_hash, action, items)
            data = "".join(canonical_strict(e) + "\n" for e in entries)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:
                raise AuditError(f"cannot write audit record: {exc}") from exc
            return entries

    def first_break(self) -> tuple[int, dict[str, Any] | None]:
        """(entries that verified, the first problem or None). A problem is {line, seq, reason}."""
        if not self.path.exists():
            return 0, None
        return first_break_of(self._lines())

    def _lines(self) -> Iterable[tuple[int, dict[str, Any] | None]]:
        with open(self.path, encoding="utf-8") as fh:
            for number, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    entry = None
                yield number, entry if isinstance(entry, dict) else None

    def verify(self) -> tuple[bool, int]:
        """Return (ok, entries checked); ok is False at the first break."""
        checked, problem = self.first_break()
        return problem is None, checked
