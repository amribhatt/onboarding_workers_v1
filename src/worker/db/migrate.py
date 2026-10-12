"""Run the Alembic migrations (versioned, reviewed, applied before start-up; the app role cannot do DDL)."""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from worker.db.core import Db

HERE = Path(__file__).resolve().parent


def _config(db: Db) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(HERE / "alembic"))
    cfg.attributes["engine"] = db.engine
    return cfg


def upgrade(db: Db, revision: str = "head") -> None:
    """Apply every migration up to `revision`; a database that is already there is left alone. On PostgreSQL the
    LangGraph saver's tables are created too (the library owns their layout; they are not ours to migrate)."""
    command.upgrade(_config(db), revision)
    if db.dialect == "postgresql" and revision == "head":
        from worker.db.checkpoint import setup

        setup(db, os.environ.get("WORKER_DB_APP_ROLE", "ow_app"))


def head() -> str:
    """The revision this build ships."""
    heads = ScriptDirectory(str(HERE / "alembic")).get_heads()
    assert len(heads) == 1, "one migration head"
    return heads[0]


def current(db: Db) -> str | None:
    """The revision the database is at (None: not migrated)."""
    from alembic.runtime.migration import MigrationContext

    with db.engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()
