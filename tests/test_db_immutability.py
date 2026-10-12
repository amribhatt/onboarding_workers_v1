"""Audit immutability: what the storage itself refuses, and what the code never builds.

The first group runs everywhere. The second runs against a real PostgreSQL (`integration`).
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa

from conftest import PgSession
from worker.audit import GENESIS_HASH
from worker.db import schema as S
from worker.db.core import Db, SqlAuditLog
from worker.hashing import canonical_strict

SRC = Path(__file__).resolve().parents[1] / "src" / "worker"


def test_no_update_or_delete_construct_is_built_for_the_audit_table() -> None:
    """The adapters expose insert and select only: nothing in the package builds a change to `audit_log`."""
    offenders = []
    for path in (SRC / "db").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"audit_log\.(update|delete)\(|update\(\s*S\.audit_log|delete\(\s*S\.audit_log", text):
            offenders.append(path.name)
    assert offenders == []


def test_sqlite_refuses_update_and_delete_of_an_audit_row() -> None:
    db = Db("sqlite://")
    db.create_all()
    SqlAuditLog(db, "s").record("a", "r")
    for statement in ("UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log"):
        with pytest.raises(sa.exc.DatabaseError), db.begin() as conn:
            conn.execute(sa.text(statement))
    assert SqlAuditLog(db, "s").verify() == (True, 1)


def test_the_guarded_tables_are_state_tables_that_exist() -> None:
    assert set(S.GUARDED_TABLES) <= set(S.metadata.tables)
    assert "audit_log" not in S.GUARDED_TABLES


# ---- PostgreSQL ------------------------------------------------------------------------------------------
def _refused(db: Db, statement: str, match: str) -> None:
    with pytest.raises(sa.exc.DBAPIError, match=match), db.begin() as conn:
        conn.execute(sa.text(statement))


def _entry_row(stream: str, seq: int, prev: str, action: str = "x") -> dict[str, object]:
    body = canonical_strict({"seq": seq, "ts": "2026-01-01T00:00:00+00:00", "action": action, "rationale": "r",
                       "inputs": {}, "outputs": {}, "prev_hash": prev})
    return {"s": stream, "q": seq, "t": "2026-01-01T00:00:00+00:00", "a": action, "p": prev,
            "h": hashlib.sha256(body.encode()).hexdigest(), "b": body}


_INSERT = ("INSERT INTO audit_log (stream, seq, ts, action, prev_hash, hash, body) "
           "VALUES (:s, :q, :t, :a, :p, :h, :b)")


@pytest.mark.integration
def test_the_app_role_cannot_update_delete_or_truncate_the_audit_log(pg: PgSession) -> None:
    SqlAuditLog(pg.app, "s").record("a", "r")
    for statement in ("UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
        _refused(pg.app, statement, "permission denied")
    assert SqlAuditLog(pg.app, "s").verify() == (True, 1)


@pytest.mark.integration
def test_even_the_owner_is_refused_by_the_triggers(pg: PgSession) -> None:
    SqlAuditLog(pg.owner, "s").record("a", "r")
    for statement in ("UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
        _refused(pg.owner, statement, "append-only")


@pytest.mark.integration
def test_the_database_refuses_a_broken_chain_or_a_wrong_hash(pg: PgSession) -> None:
    stream = f"s/{uuid.uuid4().hex}"
    first = _entry_row(stream, 1, GENESIS_HASH)
    with pg.app.begin() as conn:
        conn.execute(sa.text(_INSERT), first)
    with pytest.raises(sa.exc.DBAPIError, match="chain broken"), pg.app.begin() as conn:
        conn.execute(sa.text(_INSERT), _entry_row(stream, 2, "f" * 64))
    with pytest.raises(sa.exc.DBAPIError, match="hash does not match"), pg.app.begin() as conn:
        bad = _entry_row(stream, 2, first["h"])  # type: ignore[arg-type]
        conn.execute(sa.text(_INSERT), {**bad, "h": "0" * 64})
    with pytest.raises(sa.exc.DBAPIError, match="columns disagree"), pg.app.begin() as conn:
        bad = _entry_row(stream, 2, first["h"])  # type: ignore[arg-type]
        conn.execute(sa.text(_INSERT), {**bad, "a": "other"})
    with pytest.raises(sa.exc.DBAPIError), pg.app.begin() as conn:
        conn.execute(sa.text(_INSERT), first)  # a second seq 1


@pytest.mark.integration
def test_a_state_write_without_an_audit_row_in_the_transaction_is_refused(pg: PgSession) -> None:
    _refused(pg.app, "INSERT INTO account_mappings (store, key, doc) VALUES ('s', 'k', '{}')",
             "no audit row in this transaction")
    with pg.app.begin() as conn:  # the same write is fine once its audit row is in the transaction
        conn.execute(sa.text(_INSERT), _entry_row(f"s/{uuid.uuid4().hex}", 1, GENESIS_HASH))
        conn.execute(sa.text("INSERT INTO account_mappings (store, key, doc) VALUES ('s', 'k', '{}')"))


@pytest.mark.integration
def test_the_audit_table_cannot_be_dropped_and_its_triggers_cannot_be_disabled(pg: PgSession) -> None:
    _refused(pg.owner, "DROP TABLE audit_log", "cannot be dropped")
    _refused(pg.owner, "ALTER TABLE audit_log DISABLE TRIGGER audit_no_change", "cannot be disabled")
    SqlAuditLog(pg.owner, "s").record("a", "r")  # still working


@pytest.mark.integration
def test_a_rewritten_row_is_found_by_verification(pg: PgSession) -> None:
    """Outside the guarantee on its own (a superuser can turn triggers off), but detectable: the stored text no
    longer matches its hash. The anchor (test_db_anchor.py) is what catches a consistent rewrite."""
    log = SqlAuditLog(pg.owner, "s")
    log.record("a", "r")
    log.record("b", "r")
    with pg.owner.begin() as conn:
        conn.execute(sa.text("SET LOCAL session_replication_role = replica"))
        conn.execute(sa.text("UPDATE audit_log SET body = replace(body, '\"r\"', '\"edited\"') WHERE seq = 1"))
    checked, problem = log.first_break()
    assert checked == 0 and problem is not None and "altered" in problem["reason"]


_APPENDER = """
import sys
from worker.db.core import Db, SqlAuditLog
log = SqlAuditLog(Db(sys.argv[1]), sys.argv[2])
for i in range(25):
    log.record("tick", f"process {sys.argv[3]} entry {i}", {"i": i})
"""


@pytest.mark.integration
def test_two_processes_appending_to_one_stream_keep_one_chain(pg: PgSession) -> None:
    stream = f"hot/{uuid.uuid4().hex}"
    env = {"PYTHONPATH": str(SRC.parent), "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    procs = [subprocess.Popen([sys.executable, "-c", _APPENDER, pg.app_url, stream, str(n)], env=env,
                              stderr=subprocess.PIPE) for n in range(2)]
    errors = [p.communicate()[1].decode() for p in procs]
    assert [p.returncode for p in procs] == [0, 0], errors
    assert SqlAuditLog(pg.app, stream).verify() == (True, 50)
