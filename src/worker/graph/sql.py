"""The graph questions over the SQL views (`id_lineage_v`, `account_current_v`)."""

from __future__ import annotations

import hashlib
from typing import Any

import sqlalchemy as sa

from worker.db.core import Db


def lineage_rows(db: Db, after_id: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
    """Every registered record with a source ID, in view order (by id), after `after_id`."""
    q = sa.text("SELECT id, sponsor, template, key, name, source_key, source, batch, job FROM id_lineage_v "
                "WHERE id > :a ORDER BY id" + (" LIMIT :n" if limit else ""))
    with db.begin() as conn:
        rows = conn.execute(q, {"a": after_id, **({"n": limit} if limit else {})}).mappings().all()
    return [dict(r) for r in rows]


def account_rows(db: Db, after_id: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
    q = sa.text("SELECT id, store, key, account, source, reason FROM account_current_v WHERE id > :a ORDER BY id"
                + (" LIMIT :n" if limit else ""))
    with db.begin() as conn:
        rows = conn.execute(q, {"a": after_id, **({"n": limit} if limit else {})}).mappings().all()
    return [dict(r) for r in rows]


def ident(row: dict[str, Any]) -> str:
    """A record's identity in the graph: its ID, or its name when it was registered without one."""
    return str(row["key"] or row["name"])


def fingerprint(lineage: list[tuple[str, str, str, str]], accounts: list[tuple[str, str, str]]) -> str:
    """One digest of the facts both stores must agree on: (sponsor, template, source ID, target ID) and
    (store, source key, account)."""
    lines = sorted(["B|" + "|".join(t) for t in lineage] + ["A|" + "|".join(a) for a in accounts])
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


class SqlAnswers:
    def __init__(self, db: Db) -> None:
        self.db = db

    def lineage(self, sponsor: str, *, template: str = "", source_key: str = "", key: str = "") -> list[dict[str, Any]]:
        q = sa.text("SELECT template, key, name, source_key, batch, job FROM id_lineage_v WHERE sponsor = :sp "
                    "AND source_key <> '' AND (CAST(:t AS TEXT) = '' OR template = :t) AND (CAST(:s AS TEXT) = '' OR source_key = :s)")
        with self.db.begin() as conn:
            rows = conn.execute(q, {"sp": sponsor, "t": template, "s": source_key}).mappings().all()
        out = [{"template": r["template"], "source_key": r["source_key"], "key": ident(r), "name": r["name"],
                "job": r["job"], "batch": r["batch"]} for r in rows]
        return sorted((r for r in out if not key or r["key"] == key),
                      key=lambda r: (r["template"], r["source_key"], r["key"]))

    def accounts(self, store: str) -> dict[str, str]:
        return {r["key"]: r["account"] for r in account_rows(self.db) if r["store"] == store}

    def fingerprint(self) -> str:
        return fingerprint([(r["sponsor"], r["template"], r["source_key"], ident(r))
                            for r in lineage_rows(self.db) if r["source_key"]],
                           [(r["store"], r["key"], r["account"]) for r in account_rows(self.db)])
