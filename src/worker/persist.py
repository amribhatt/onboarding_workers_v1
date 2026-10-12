"""Files or the database, decided in one place.

Every store the product uses is made here: with `DATABASE_URL` unset each function returns the file class it
always did (same arguments, same behaviour), with it set the SQLAlchemy adapter of `worker.db`, which ignores the
path and keys by sponsor and name. A call site changes by one word (`AuditLog(p)` becomes `persist.audit(p)`);
nothing else in the file-backed code moves.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from worker.audit import AuditLog

_STATE: Path | None = None


def use_state_dir(path: str | Path) -> None:
    """The state folder audit streams are named relative to (set once by the service and the console)."""
    global _STATE
    _STATE = Path(path)


def _db() -> Any:
    from worker.db.backend import database_url, db_for

    url = database_url()
    return db_for(url) if url else None


def job_audit(c: Any, j: Mapping[str, Any]) -> Any:
    """The audit log in a job's own folder (made when missing)."""
    folder = c.job_dir(j)
    folder.mkdir(parents=True, exist_ok=True)
    return audit(folder / "audit.jsonl")


def audit(path: str | Path) -> Any:
    """The hash-chained log for `path`: the file, or the stream of the same name in the database."""
    db = _db()
    if db is None:
        return AuditLog(Path(path))
    from worker.db.backend import stream_of
    from worker.db.core import SqlAuditLog

    return SqlAuditLog(db, stream_of(path, _STATE or os.environ.get("WORKER_STATE_DIR")))


def feedback(root: str | Path, sponsor: str) -> Any:
    db = _db()
    if db is None:
        from worker.feedback import FeedbackStore

        return FeedbackStore(root, sponsor)
    from worker.db.documents import SqlFeedbackStore

    return SqlFeedbackStore(db, sponsor)


def feedback_sponsors(root: str | Path) -> list[str]:
    """Every sponsor that has a feedback log."""
    db = _db()
    if db is None:
        return sorted(p.parent.name for p in Path(root).glob("*/feedback.jsonl") if p.is_file())
    from worker.db.core import streams

    prefix, suffix = "feedback/", "/feedback.jsonl"
    return sorted(s[len(prefix):-len(suffix)] for s in streams(db) if s.startswith(prefix) and s.endswith(suffix))


def feedback_signature(root: str | Path) -> tuple[Any, ...]:
    """Changes whenever any sponsor's feedback log gains an entry (the cache key of what is derived from them)."""
    db = _db()
    if db is None:
        files = sorted(Path(root).glob("*/feedback.jsonl"))
        return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files if p.is_file())
    import sqlalchemy as sa

    from worker.db import schema as S

    t = S.audit_log
    with db.begin() as conn:
        rows = conn.execute(sa.select(t.c.stream, sa.func.max(t.c.seq)).where(t.c.stream.like("feedback/%"))
                            .group_by(t.c.stream).order_by(t.c.stream)).all()
    return tuple((r[0], int(r[1])) for r in rows)


def registry(root: str | Path, sponsor: str = "") -> Any:
    db = _db()
    if db is None:
        from worker.registry import Registry

        return Registry(root, sponsor)
    from worker.db.stores import SqlRegistry

    return SqlRegistry(db, sponsor)


def memory(root: str | Path, sponsor: str) -> Any:
    db = _db()
    if db is None:
        from worker.memory import SponsorMemory

        return SponsorMemory(root, sponsor)
    from worker.db.documents import SqlSponsorMemory

    return SqlSponsorMemory(db, sponsor)


def recipe_store(root: str | Path, sponsor: str) -> Any:
    db = _db()
    if db is None:
        from worker.recipes import RecipeStore

        return RecipeStore(root, sponsor)
    from worker.db.documents import SqlRecipeStore

    return SqlRecipeStore(db, sponsor)


def field_recipes(root: str | Path, sponsor: str, **kw: Any) -> Any:
    db = _db()
    if db is None:
        from worker.recipes import FieldRecipes

        return FieldRecipes(root, sponsor, **kw)
    from worker.db.documents import SqlFieldRecipes

    return SqlFieldRecipes(db, sponsor, **kw)


def improvements(root: str | Path, audit_log: Any = None) -> Any:
    db = _db()
    if db is None:
        from worker.improver import ImprovementStore

        return ImprovementStore(root, audit_log)
    from worker.db.documents import SqlImprovementStore

    return SqlImprovementStore(db, audit_log)


def accounts(root: str | Path, sponsor: str | None) -> Any:
    db = _db()
    if db is None:
        from worker.accounts import AccountStore

        return AccountStore(root, sponsor)
    from worker.db.stores import SqlAccountStore

    return SqlAccountStore(db, sponsor)


def account_stores(root: str | Path) -> dict[str, dict[str, dict[str, Any]]]:
    """Every sponsor store's entries by store name (`_golden` included), for the pages that list them all."""
    db = _db()
    if db is None:
        from worker.accounts import AccountStore

        return {p.stem: AccountStore(root, p.stem).entries() for p in sorted(Path(root).glob("*.jsonl"))}
    from worker.db.stores import SqlAccountStore, account_store_names

    return {n: SqlAccountStore(db, None if n == "_golden" else n).entries() for n in account_store_names(db)}


def versions(directory: str | Path, initial: Callable[[], dict[str, Any]] | None = None) -> Any:
    """A versioned document. In the database it is named by the folder's last three parts (e.g. the skill and
    the table of a decision-table overlay) under its kind."""
    db = _db()
    if db is None:
        from worker.accounts import Versions

        return Versions(directory, initial)
    from worker.db.stores import SqlVersions

    return SqlVersions(db, "/".join(Path(directory).parts[-3:]), initial)


def golden(root: str | Path) -> Any:
    """The Golden mappings, versioned (version 0 is the admin-promoted store)."""
    db = _db()
    if db is None:
        from worker.accounts import golden_versions

        return golden_versions(root)
    from worker.db.stores import SqlAccountStore, SqlVersions

    return SqlVersions(db, "golden", lambda: SqlAccountStore(db, None).entries())


def artifact_store(root: str | Path) -> Any:
    """Layout-program artifacts and their decisions: the folder under `root`, or the insert-only tables."""
    db = _db()
    from worker.flex.artifacts import FileArtifactStore, SqlArtifactStore

    return FileArtifactStore(Path(root)) if db is None else SqlArtifactStore(db)


def id_scheme(root: str | Path, sponsor: str, record_type: str, default: Mapping[str, Any] | None = None) -> Any:
    db = _db()
    if db is None:
        from worker.idgen import IdScheme

        return IdScheme(root, sponsor, record_type, default)
    from worker.db.stores import SqlIdScheme

    return SqlIdScheme(db, sponsor, record_type, default)


class _FileDocument:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> str | None:
        return self.path.read_text(encoding="utf-8") if self.path.is_file() else None

    def save(self, text: str) -> None:
        from worker.fsutil import atomic_write_text

        atomic_write_text(self.path, text)


def console_document(state_file: str | Path | None) -> Any:
    """Where the console keeps its one state document: the file, or a compare-and-set row in the database."""
    if state_file is None:
        return None
    db = _db()
    if db is None:
        return _FileDocument(Path(state_file))
    from worker.db.stores import SqlDocument

    return SqlDocument(db, 1)

