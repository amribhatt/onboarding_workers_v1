"""The spool: row outcomes and staged rows spill to a `Store` as they are decided, and are paged back from it.

Nothing here holds a table. A `Spool` is one append-only sequence of JSON lines under one store key:

* `append` buffers a few records and writes them to the store in chunks (the engine's resume point is a sealed
  chunk, never row 1);
* `seal` flushes, records the running sha256 of every line written and marks the spool done;
* `read` and `page` stream it back one line at a time, so a caller pages a million rows without holding them.

Records are plain JSON with the cell types of a workbook kept (dates, decimals) by the same encoder that
`staging.Stage` uses to persist its tables, and the order of a row's fields is kept. The encoding of a record is
deterministic, so the digest of a spool is the same on every run of the same job.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from worker.staging import _dec, _enc

CHUNK_ROWS = 2000


# one encoder and one decoder for every line: `json.dumps` / `json.loads` with options build a new one per call
_ENCODER = json.JSONEncoder(default=_enc, ensure_ascii=False, separators=(",", ":"))
_DECODER = json.JSONDecoder(object_hook=_dec)
_PLAIN = json.JSONDecoder()  # a line with no typed value (`"__t"`) has nothing for the hook to do: skip the hook


def encode(record: Any) -> bytes:
    """One record as one line of bytes (no newline inside)."""
    return _ENCODER.encode(record).encode("utf-8")


def decode(line: bytes) -> Any:
    text = line.decode("utf-8")
    return (_DECODER if '"__t"' in text else _PLAIN).decode(text)


def write_json(store: Any, key: str, payload: Any) -> str:
    """A small document (a stage payload, an index): replaced whole and sealed. Returns its digest."""
    body = encode(payload)
    store.reset(key)
    store.append(key, body + b"\n")
    digest = hashlib.sha256(body).hexdigest()
    store.seal(key, digest)
    return digest


def read_json(store: Any, key: str) -> Any | None:
    """The document under `key` when it is sealed, else None (an unsealed document is a stage that did not finish)."""
    if store.sealed(key) is None:
        return None
    raw = store.get(key)
    return decode(raw.splitlines()[0]) if raw else None


class Spool:
    """Append-only JSON lines under `key`. Open a new one with `Spool(store, key)`; it starts empty. To continue
    a spool that was written in chunks and not sealed, use `Spool.resume`."""

    def __init__(self, store: Any, key: str, *, chunk: int = CHUNK_ROWS, resume: bool = False) -> None:
        self.store = store
        self.key = key
        self.chunk = max(1, chunk)
        self._buffer: list[bytes] = []
        self._hash = hashlib.sha256()
        self.count = 0
        if resume:
            for line in store.lines(key):
                self._hash.update(line)
                self.count += 1
        else:
            store.reset(key)

    @classmethod
    def resume(cls, store: Any, key: str, *, chunk: int = CHUNK_ROWS) -> Spool:
        """Re-open an unsealed spool at its last whole chunk (a partial line is dropped by the store)."""
        return cls(store, key, chunk=chunk, resume=True)

    def append(self, record: Mapping[str, Any] | Any) -> None:
        line = encode(record)
        self._buffer.append(line)
        self._hash.update(line)
        self.count += 1
        if len(self._buffer) >= self.chunk:
            self.flush()

    def extend(self, records: Iterable[Any]) -> None:
        for r in records:
            self.append(r)

    def flush(self) -> None:
        if self._buffer:
            self.store.append(self.key, b"\n".join(self._buffer) + b"\n")
            self._buffer = []

    def seal(self) -> str:
        """Flush and mark the spool done; returns the digest of every line written."""
        self.flush()
        digest = self._hash.hexdigest()
        self.store.seal(self.key, digest)
        return digest


def read(store: Any, key: str, skip: int = 0) -> Iterator[Any]:
    """The records of a spool in order, one at a time, after skipping `skip` of them."""
    for line in store.lines(key, skip):
        if line.strip():
            yield decode(line)


def page(store: Any, key: str, offset: int = 0, limit: int = 200, where: Any = None) -> Iterator[Any]:
    """`limit` records after `offset` of those that pass `where` (all when None), streamed. Without a filter the
    store skips `offset` lines without decoding them."""
    if limit <= 0:
        return
    if where is None:
        for n, rec in enumerate(read(store, key, offset)):
            if n >= limit:
                return
            yield rec
        return
    seen = given = 0
    for rec in read(store, key):
        if not where(rec):
            continue
        seen += 1
        if seen <= offset:
            continue
        yield rec
        given += 1
        if given >= limit:
            return
