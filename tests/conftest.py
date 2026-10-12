"""Database fixtures shared by the persistence tests.

`db` runs a test on SQLite in memory and, when `WORKER_TEST_PG_URL` is set (a superuser URL on a server where this
suite may create and drop databases), again on PostgreSQL (marked `integration`). Each PostgreSQL test gets its own
freshly migrated database, so nothing leaks between tests.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from worker.db.core import Db

# the Parquet cache of `worker.columnar` lives in a temporary folder, never in the tree
os.environ.setdefault("WORKER_CACHE_DIR", tempfile.mkdtemp(prefix="worker-cache-"))

PG_URL = os.environ.get("WORKER_TEST_PG_URL", "")
APP_ROLE = "ow_app"
APP_PASSWORD = "ow-app-test-password"


class PgSession:
    """One migrated PostgreSQL database: `owner` (the migrating superuser) and `app` (the application role)."""

    def __init__(self, name: str) -> None:
        self.name = name
        base = make_url(PG_URL)
        self.owner_url = base.set(database=name).render_as_string(hide_password=False)
        self.app_url = base.set(database=name, username=APP_ROLE, password=APP_PASSWORD).render_as_string(
            hide_password=False)
        self.owner = Db(self.owner_url)
        self.app = Db(self.app_url)


def _admin() -> sa.engine.Engine:
    return sa.create_engine(PG_URL, isolation_level="AUTOCOMMIT")


@pytest.fixture
def pg() -> Iterator[PgSession]:
    if not PG_URL:
        pytest.skip("WORKER_TEST_PG_URL is not set")
    name = f"ow_t_{uuid.uuid4().hex[:12]}"
    admin = _admin()
    with admin.connect() as c:
        c.execute(sa.text(f'CREATE DATABASE "{name}"'))
    os.environ["WORKER_DB_APP_PASSWORD"] = APP_PASSWORD
    session = PgSession(name)
    try:
        session.owner.create_all()
        yield session
    finally:
        session.owner.dispose()
        session.app.dispose()
        with admin.connect() as c:
            c.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.integration)])
def db(request: pytest.FixtureRequest) -> Iterator[Db]:
    """The contract suite runs on both."""
    if request.param == "sqlite":
        d = Db("sqlite://")
        d.create_all()
        yield d
        d.dispose()
    else:
        session: PgSession = request.getfixturevalue("pg")
        yield session.app  # the application's own role: what production runs as


@pytest.fixture(autouse=True)
def _database_mode(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory,
                   monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """`WORKER_TEST_DB_MODE=sqlite|postgres` runs a test with `DATABASE_URL` set (a fresh migrated database per
    test): the parity check of the database-backed stores against the file-backed suite. Tests of the SQL layer
    itself and anything marked `integration` are left alone. Tests that seed or read the state folder's files
    directly are expected to differ in this mode: that is a statement about the test, not the stores."""
    mode = os.environ.get("WORKER_TEST_DB_MODE", "")
    if not mode or "test_db_" in request.node.nodeid or request.node.get_closest_marker("integration"):
        yield
        return
    if mode == "postgres":
        session: PgSession = request.getfixturevalue("pg")
        monkeypatch.setenv("DATABASE_URL", session.app_url)
        yield
        return
    url = f"sqlite:///{(tmp_path_factory.mktemp('db') / 'ow.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    db = Db(url)
    db.create_all()
    db.dispose()
    yield
