"""The connection, the transaction rule and the SQL audit log.

* `Db` wraps one SQLAlchemy engine. `Db.begin()` is the only way to a transaction; on SQLite (tests, one file) a
  process lock serialises writers, on PostgreSQL the database does.
* `append` writes audit entries to a stream inside a given transaction. On PostgreSQL it takes a transaction-scoped
  advisory lock on the stream, so two processes can never fork a chain.
* `SqlAuditLog` is the `AuditSink` (and the whole `AuditLog` surface) over `audit_log`: insert and select only.
* `journal` is how a state change is audited in the SAME transaction as its effect; PostgreSQL refuses an effect
  with no such row (`require_audit_in_txn`).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from worker.audit import GENESIS_HASH, AuditError, chain_entries, first_break_of
from worker.db import schema as S
from worker.hashing import canonical_strict

# A request's repeated reads of the same state (one view asks for the sponsor's records, an ID scheme or a recipe
# several times) are answered once. The scope is one request (`read_scope`); a state change (`journal`) committed by
# anyone in this process ends what it holds. A read made in another process shows up at the next request.
_READS: ContextVar[dict[str, Any] | None] = ContextVar("db_reads", default=None)
_GENERATION = [0]


@contextmanager
def read_scope() -> Iterator[None]:
    """Answer a repeated read once for the duration of the block (one request). Nested use shares the outer scope."""
    if _READS.get() is not None:
        yield
        return
    token = _READS.set({"gen": _GENERATION[0], "memo": {}})
    try:
        yield
    finally:
        _READS.reset(token)


def in_read_scope() -> bool:
    return _READS.get() is not None


def remembered(key: tuple[Any, ...], load: Callable[[], Any]) -> Any:
    """`load()` once per read scope and generation. Outside a scope it just loads. The value must be immutable."""
    scope = _READS.get()
    if scope is None:
        return load()
    gen = _GENERATION[0]
    if scope["gen"] != gen:
        scope["memo"].clear()
        scope["gen"] = gen
    memo = scope["memo"]
    if key not in memo:
        memo[key] = load()
    return memo[key]


class Db:
    def __init__(self, url: str) -> None:
        self.url = url
        if url.startswith("sqlite"):
            kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False}}
            if url in ("sqlite://", "sqlite:///:memory:"):
                kwargs["poolclass"] = StaticPool
            self.engine = sa.create_engine(url, **kwargs)
            self._lock: threading.RLock | None = threading.RLock()
        else:
            self.engine = sa.create_engine(url, pool_size=10, max_overflow=5, pool_pre_ping=True)
            self._lock = None
        self.dialect = self.engine.dialect.name

    @contextmanager
    def begin(self) -> Iterator[Connection]:
        """One transaction: committed on a clean exit, rolled back on any exception."""
        if self._lock is None:
            with self.engine.begin() as conn:
                yield conn
                changed = bool(conn.info.pop("state_changed", False))
        else:
            with self._lock, self.engine.begin() as conn:
                yield conn
                changed = bool(conn.info.pop("state_changed", False))
        if changed:  # committed: what a read scope remembers is no longer true
            _GENERATION[0] += 1

    def create_all(self) -> None:
        """Make the schema directly (tests). Production runs the Alembic migration, which also adds the triggers
        and the grants."""
        from worker.db.migrate import upgrade

        upgrade(self)

    def dispose(self) -> None:
        self.engine.dispose()


def insert_ignore(db_dialect: str, table: sa.Table) -> Any:
    """`INSERT ... ON CONFLICT DO NOTHING` for the two dialects in use."""
    if db_dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(table).on_conflict_do_nothing()


def _lock_stream(conn: Connection, stream: str) -> None:
    if conn.dialect.name == "postgresql":
        conn.execute(sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:s, 0))"), {"s": stream})


def append(conn: Connection, stream: str, action: str, items: list[tuple[str, dict | None, dict | None]]
           ) -> list[dict[str, Any]]:
    """Chain `items` onto `stream` inside the transaction `conn`. Raises `AuditError` if it cannot be written."""
    for rationale, _i, _o in items:
        if not str(rationale).strip():
            raise AuditError(f"a reason is required for {action}; the action is refused")
    try:
        _lock_stream(conn, stream)
        tail = conn.execute(
            sa.select(S.audit_log.c.seq, S.audit_log.c.hash).where(S.audit_log.c.stream == stream)
            .order_by(S.audit_log.c.seq.desc()).limit(1)).first()
        seq, prev = (int(tail[0]), str(tail[1])) if tail else (0, GENESIS_HASH)
        entries = chain_entries(seq, prev, action, items)
        conn.execute(S.audit_log.insert(), [
            {"stream": stream, "seq": e["seq"], "ts": e["ts"], "action": action, "prev_hash": e["prev_hash"],
             "hash": e["hash"], "body": canonical_strict({k: v for k, v in e.items() if k != "hash"})}
            for e in entries])
    except SQLAlchemyError as exc:
        raise AuditError(f"cannot write audit record: {exc}") from exc
    return entries


def journal(conn: Connection, stream: str, action: str, rationale: str, inputs: Mapping[str, Any] | None = None,
            outputs: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The audit row of a state change, written in the transaction that makes the change."""
    conn.info["state_changed"] = True
    return append(conn, stream, action, [(rationale, dict(inputs or {}), dict(outputs or {}))])[0]


def _entry(row: Any) -> dict[str, Any]:
    body = json.loads(row.body)
    body["hash"] = row.hash
    return body


class SqlAuditLog:
    """The `AuditLog` surface over one stream of `audit_log`. Every read re-checks the rows it returns."""

    def __init__(self, db: Db, stream: str) -> None:
        self.db = db
        self.stream = stream

    def record(self, action: str, rationale: str, inputs: dict | None = None, outputs: dict | None = None) -> dict:
        return self.record_many(action, [(rationale, inputs, outputs)])[0]

    def record_many(self, action: str, items: list[tuple[str, dict | None, dict | None]]) -> list[dict]:
        try:
            with self.db.begin() as conn:
                return append(conn, self.stream, action, items)
        except SQLAlchemyError as exc:
            raise AuditError(f"cannot write audit record: {exc}") from exc

    def read(self) -> list[dict]:
        with self.db.begin() as conn:
            rows = conn.execute(sa.select(S.audit_log).where(S.audit_log.c.stream == self.stream)
                                .order_by(S.audit_log.c.seq)).all()
        return [_entry(r) for r in rows]

    def first_break(self) -> tuple[int, dict[str, Any] | None]:
        """(entries that verified, the first problem or None), recomputed from the stored text. Also compares the
        columns with the body, so an edit of a column is found as well as an edit of the body."""
        with self.db.begin() as conn:
            rows = conn.execute(sa.select(S.audit_log).where(S.audit_log.c.stream == self.stream)
                                .order_by(S.audit_log.c.seq)).all()

        def entries() -> Iterator[tuple[int, dict[str, Any] | None]]:
            for n, r in enumerate(rows, start=1):
                try:
                    entry = _entry(r)
                except ValueError:
                    yield n, None
                    continue
                columns_agree = (r.seq == entry.get("seq") and r.prev_hash == entry.get("prev_hash")
                                 and r.action == entry.get("action"))
                yield n, entry if columns_agree else None

        return first_break_of(entries())

    def verify(self) -> tuple[bool, int]:
        checked, problem = self.first_break()
        return problem is None, checked


def streams(db: Db) -> list[str]:
    with db.begin() as conn:
        return [r[0] for r in conn.execute(sa.select(S.audit_log.c.stream).distinct().order_by(S.audit_log.c.stream))]
