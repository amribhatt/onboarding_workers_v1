"""Lineage and account-mapping questions, answered from SQL views (the default) or from Neo4j (`WORKER_GRAPH=on`).

Neo4j is a projection of rows that live in PostgreSQL. Nothing here is a source of truth: the engine, the gates and
the console never read it, readiness does not include it, and it can be deleted and rebuilt from the database.
The `neo4j` driver is imported only inside this package (a fitness test holds that line).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from worker.db.core import Db


class Answers(Protocol):
    def lineage(self, sponsor: str, *, template: str = "", source_key: str = "", key: str = "") -> list[dict[str, Any]]:
        """Which source IDs became which target IDs: [{template, source_key, key, name, job, batch}], sorted."""

    def accounts(self, store: str) -> dict[str, str]:
        """The account each source key maps to now, for one store (`_golden` or a sponsor)."""


def enabled(env: Mapping[str, str]) -> bool:
    return (env.get("WORKER_GRAPH") or "off").strip().lower() in ("on", "1", "true", "yes")


def answers(db: Db, env: Mapping[str, str]) -> Answers:
    """The graph when it is switched on, else the SQL views: the same questions, the same shape of answer."""
    if enabled(env):
        from worker.graph.neo import GraphAnswers, connect

        return GraphAnswers(connect(env))
    from worker.graph.sql import SqlAnswers

    return SqlAnswers(db)
