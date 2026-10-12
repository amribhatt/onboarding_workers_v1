"""LangGraph checkpoints in PostgreSQL (`PostgresSaver`), so a paused gate survives a restart of the container.

The saver needs its own connection pool in autocommit mode (it manages transactions itself), kept apart from the
SQLAlchemy pool. Its tables are created by `setup` with the owner's credentials (the migrate job); the application
role only reads and writes them.
"""

from __future__ import annotations

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy.engine import make_url

from worker.db.core import Db


def conninfo(url: str) -> str:
    """A SQLAlchemy URL (`postgresql+psycopg://...`) as the plain URL psycopg takes."""
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def _pool(url: str, size: int) -> ConnectionPool:
    return ConnectionPool(conninfo(url), min_size=1, max_size=size, open=True,
                          kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})


def setup(db: Db, app_role: str) -> None:
    """Create the saver's tables (idempotent) and let the application role use them. Owner credentials."""
    pool = _pool(db.url, 1)
    try:
        PostgresSaver(pool).setup()
        with pool.connection() as conn:
            conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON checkpoints, checkpoint_blobs, "
                         f'checkpoint_writes, checkpoint_migrations TO "{app_role}"')
    finally:
        pool.close()


def postgres_saver(url: str) -> PostgresSaver:
    return PostgresSaver(_pool(url, 4))
