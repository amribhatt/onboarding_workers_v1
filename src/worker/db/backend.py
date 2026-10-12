"""Which backend the process runs on: files (the default) or the database (`DATABASE_URL` is set).

`current(state_dir)` is None for files, so every caller keeps its file class and nothing changes. The factories
here build the database's world and store; the file classes are never touched.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from worker.db import migrate
from worker.db import schema as S
from worker.db.core import Db
from worker.db.stores import SqlStore, SqlWorld
from worker.db.verify import VERIFICATION_STREAM

_DBS: dict[str, Db] = {}
_LOCK = threading.Lock()


def database_url(env: Mapping[str, str] | None = None) -> str | None:
    """`DATABASE_URL`, or the first line of the file named by `DATABASE_URL_FILE` (a mounted secret)."""
    e = env if env is not None else os.environ
    value = (e.get("DATABASE_URL") or "").strip()
    if not value and e.get("DATABASE_URL_FILE"):
        value = Path(e["DATABASE_URL_FILE"]).read_text(encoding="utf-8").strip()
    return value or None


def db_for(url: str) -> Db:
    """One engine (one pool) per URL per process."""
    with _LOCK:
        if url not in _DBS:
            _DBS[url] = Db(url)
        return _DBS[url]


def health(db: Db) -> dict[str, str]:
    """The database's part of readiness: reachable, migrated to what this build ships, and the last verification
    (`worker.db.verify`) did not find a problem. Each value is "ok" or the reason."""
    out: dict[str, str] = {}
    try:
        with db.begin() as conn:
            conn.execute(sa.text("SELECT 1"))
            last = conn.execute(sa.select(S.audit_log.c.action).where(S.audit_log.c.stream == VERIFICATION_STREAM)
                                .order_by(S.audit_log.c.seq.desc()).limit(1)).first()
        out["database"] = "ok"
        rev = migrate.current(db)
    except Exception as exc:  # noqa: BLE001  readiness reports any failure, it does not raise
        return {"database": f"unreachable: {type(exc).__name__}"}
    out["schema"] = "ok" if rev == migrate.head() else f"database is at {rev}, this build ships {migrate.head()}"
    out["audit_verification"] = ("audit chain verification failed: see system/verification"
                                 if last is not None and last[0] == "audit.verification_failed" else "ok")
    return out


def stream_of(path: str | Path, state_dir: str | Path | None = None) -> str:
    """The audit stream a file path maps to: the path under the state directory, in posix form. A path outside it
    keeps its own parts (no drive, no leading slash)."""
    p = Path(path)
    if state_dir is not None:
        try:
            return p.resolve().relative_to(Path(state_dir).resolve()).as_posix()
        except ValueError:
            pass
    return "/".join(part for part in p.as_posix().split("/") if part and part not in (".", "..") and ":" not in part)


class Backend:
    def __init__(self, db: Db, state_dir: str | Path) -> None:
        self.db = db
        self.state_dir = Path(state_dir)

    def world(self, sponsor: str, templates: Mapping[str, Any] | None = None) -> SqlWorld:
        return SqlWorld(self.db, sponsor, templates=templates)

    def store(self) -> SqlStore:
        return SqlStore(self.db)


def current(state_dir: str | Path, env: Mapping[str, str] | None = None) -> Backend | None:
    url = database_url(env)
    return Backend(db_for(url), state_dir) if url else None
