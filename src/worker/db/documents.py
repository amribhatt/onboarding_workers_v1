"""Memory, recipes and improvement cards over SQLAlchemy Core.

Each class is the file class with only its storage swapped (it subclasses it), so the rules (gates, validation,
"audit first") are the same code on both backends. Every write is one transaction that starts with its audit row.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from worker.audit import AuditLog
from worker.db import schema as S
from worker.db.core import Db, SqlAuditLog, journal, remembered
from worker.feedback import FeedbackStore
from worker.improver import ImprovementStore
from worker.memory import _SAFE as _MEMORY_SAFE
from worker.memory import SponsorMemory
from worker.recipes import _RECORD, _SAFE, FieldRecipes, RecipeStore

_CARD_ID = re.compile(r"[A-Za-z0-9_-]{1,40}")


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SqlSponsorMemory(SponsorMemory):
    """Per-sponsor rules (pending and approved) and approved cases. `approve` moves a rule in one transaction."""

    def __init__(self, db: Db, sponsor: str) -> None:
        if not _MEMORY_SAFE.match(sponsor):
            raise ValueError(f"unsafe sponsor name {sponsor!r}")
        self.db = db
        self.sponsor = sponsor

    def _read(self, conn: Connection, bucket: str) -> dict[str, Any]:
        t = S.memory_rules
        rows = conn.execute(sa.select(t.c.rule_id, t.c.doc).where(t.c.sponsor == self.sponsor, t.c.bucket == bucket)
                            .order_by(t.c.ord)).all()
        return {r.rule_id: json.loads(r.doc) for r in rows}

    def _replace(self, conn: Connection, bucket: str, data: Mapping[str, Any], why: str) -> None:
        t = S.memory_rules
        journal(conn, f"state/{self.sponsor}/memory", f"memory.{bucket}_saved", why,
                {"sponsor": self.sponsor, "rules": sorted(data)})
        conn.execute(t.delete().where(t.c.sponsor == self.sponsor, t.c.bucket == bucket))
        if data:
            conn.execute(t.insert(), [{"sponsor": self.sponsor, "bucket": bucket, "rule_id": rid, "ord": n,
                                       "doc": _dump(doc)} for n, (rid, doc) in enumerate(data.items())])

    def _load_pending(self) -> dict[str, dict[str, Any]]:
        with self.db.begin() as conn:
            return self._read(conn, "pending")

    def _save_pending(self, data: dict[str, dict[str, Any]]) -> None:
        with self.db.begin() as conn:
            self._replace(conn, "pending", data, "pending rules saved")

    def _approved(self) -> dict[str, dict[str, str]]:
        with self.db.begin() as conn:
            return self._read(conn, "approved")

    def _write_approved(self, rules: dict[str, dict[str, str]]) -> None:
        with self.db.begin() as conn:
            self._replace(conn, "approved", rules, "approved rules saved")

    def approve(self, rule_id: str) -> None:
        with self.db.begin() as conn:
            pending = self._read(conn, "pending")
            item = pending.get(rule_id)
            if item is None or item.get("status") != "pending":
                raise KeyError(f"no pending rule {rule_id!r}")
            rules = self._read(conn, "approved")
            rules[rule_id] = {"status": "active", "text": item["text"], "evidence": item["evidence"],
                              "approved_at": _now()}
            del pending[rule_id]
            self._replace(conn, "approved", rules, f"rule {rule_id} approved")
            self._replace(conn, "pending", pending, f"rule {rule_id} left pending")

    def _put_case(self, case_id: str, record: dict[str, Any]) -> None:
        t = S.memory_cases
        with self.db.begin() as conn:
            journal(conn, f"state/{self.sponsor}/memory", "memory.case_saved", f"case {case_id} saved",
                    {"sponsor": self.sponsor, "case_id": case_id})
            conn.execute(t.delete().where(t.c.sponsor == self.sponsor, t.c.case_id == case_id))
            conn.execute(t.insert().values(sponsor=self.sponsor, case_id=case_id, doc=_dump(record)))

    def cases(self) -> list[dict[str, Any]]:
        t = S.memory_cases
        with self.db.begin() as conn:
            rows = conn.execute(sa.select(t.c.doc).where(t.c.sponsor == self.sponsor).order_by(t.c.case_id)).all()
        return [json.loads(r.doc) for r in rows]


class SqlFeedbackStore(FeedbackStore):
    """The sponsor's feedback log: a chained stream of the audit log (`feedback/<sponsor>/feedback.jsonl`)."""

    def __init__(self, db: Db, sponsor: str) -> None:
        self.log = SqlAuditLog(db, f"feedback/{sponsor}/feedback.jsonl")  # type: ignore[assignment]
        self.sponsor = sponsor


class SqlRecipeStore(RecipeStore):
    """The old recipe versions (`v<N>`), as rows."""

    def __init__(self, db: Db, sponsor: str) -> None:
        if not _SAFE.match(sponsor):
            raise ValueError(f"bad sponsor name {sponsor!r}")
        self.db = db
        self.sponsor = sponsor

    def _type(self, record_type: str) -> str:
        if not _SAFE.match(record_type):
            raise ValueError(f"bad record type {record_type!r}")
        return record_type

    def versions(self, record_type: str) -> list[int]:
        t = S.legacy_recipes
        with self.db.begin() as conn:
            rows = conn.execute(sa.select(t.c.version).where(t.c.sponsor == self.sponsor,
                                                           t.c.record_type == self._type(record_type))
                                .order_by(t.c.version)).all()
        return [int(r[0]) for r in rows]

    def load(self, record_type: str, version: int | None = None) -> dict[str, Any] | None:
        have = self.versions(record_type)
        if not have:
            return None
        t = S.legacy_recipes
        with self.db.begin() as conn:
            row = conn.execute(sa.select(t.c.doc).where(t.c.sponsor == self.sponsor,
                                                      t.c.record_type == record_type,
                                                      t.c.version == (version or have[-1]))).first()
        if row is None:
            raise FileNotFoundError(f"no version {version} of the {record_type} recipe")
        return json.loads(row[0])

    def _put(self, record_type: str, version: int, recipe: dict[str, Any]) -> None:
        t = S.legacy_recipes
        with self.db.begin() as conn:
            journal(conn, f"state/{self.sponsor}/recipes", "recipe.version_saved",
                    f"{record_type} recipe version {version} saved",
                    {"sponsor": self.sponsor, "record_type": record_type, "version": version})
            conn.execute(t.insert().values(sponsor=self.sponsor, record_type=self._type(record_type),
                                           version=version, doc=_dump(recipe)))


class SqlFieldRecipes(FieldRecipes):
    """One sponsor's field recipes. A change adds a version and moves the pointer; a revert moves the pointer
    back. Nothing is deleted. The history is a chained stream of the audit log."""

    def __init__(self, db: Db, sponsor: str, *, record_types: Iterable[str] | None = None,
                 target_fields: Mapping[str, Iterable[str]] | None = None) -> None:
        super().__init__(".", sponsor, record_types=record_types, target_fields=target_fields)
        self.db = db

    def history(self, record_type: str) -> SqlAuditLog:  # type: ignore[override]
        if not _RECORD.match(record_type):
            raise ValueError(f"bad record type {record_type!r}")
        return SqlAuditLog(self.db, f"recipes/{self.sponsor}/{record_type}.history")

    def text(self, record_type: str) -> str | None:
        if not _RECORD.match(record_type):
            raise ValueError(f"bad record type {record_type!r}")
        cur, rec = S.field_recipe_current, S.field_recipes

        def load() -> str | None:
            with self.db.begin() as conn:
                row = conn.execute(
                    sa.select(rec.c.text).select_from(cur.join(rec, sa.and_(
                        rec.c.sponsor == cur.c.sponsor, rec.c.record_type == cur.c.record_type,
                        rec.c.version == cur.c.version)))
                    .where(cur.c.sponsor == self.sponsor, cur.c.record_type == record_type)).first()
            return str(row[0]) if row else None

        return remembered((id(self.db), "field_recipe", self.sponsor, record_type), load)  # type: ignore[no-any-return]

    def _write(self, record_type: str, text: str | None) -> None:
        cur, rec = S.field_recipe_current, S.field_recipes
        with self.db.begin() as conn:
            journal(conn, f"state/{self.sponsor}/recipes", "recipe.stored",
                    f"{record_type} recipe " + ("removed from force" if text is None else "stored"),
                    {"sponsor": self.sponsor, "record_type": record_type})
            version = 0
            if text is not None:
                version = int(conn.execute(sa.select(sa.func.coalesce(sa.func.max(rec.c.version), 0)).where(
                    rec.c.sponsor == self.sponsor, rec.c.record_type == record_type)).scalar_one()) + 1
                conn.execute(rec.insert().values(sponsor=self.sponsor, record_type=record_type, version=version,
                                                 text=text, created_at=_now()))
            where = sa.and_(cur.c.sponsor == self.sponsor, cur.c.record_type == record_type)
            if conn.execute(cur.update().where(where).values(version=version)).rowcount == 0:
                conn.execute(cur.insert().values(sponsor=self.sponsor, record_type=record_type, version=version))


class SqlImprovementStore(ImprovementStore):
    """Improvement cards as rows (the controls' change cards); the filing and promotion rules are the base class's."""

    def __init__(self, db: Db, audit: AuditLog | SqlAuditLog | None = None) -> None:
        self.db = db
        self.audit = audit  # type: ignore[assignment]

    def _id(self, card_id: str) -> str:
        if not _CARD_ID.fullmatch(card_id):
            raise ValueError(f"bad card id {card_id!r}")
        return card_id

    def save(self, card: dict[str, Any]) -> None:
        t = S.improvement_cards
        cid = self._id(card["id"])
        with self.db.begin() as conn:
            journal(conn, "state/improvements", "card.saved", f"card {cid} is {card.get('status')}",
                    {"card": cid, "status": card.get("status")})
            values = {"status": str(card.get("status", "")), "doc": _dump(card)}
            if conn.execute(t.update().where(t.c.card_id == cid).values(**values)).rowcount == 0:
                conn.execute(t.insert().values(card_id=cid, **values))

    def load(self, card_id: str) -> dict[str, Any]:
        t = S.improvement_cards
        with self.db.begin() as conn:
            row = conn.execute(sa.select(t.c.doc).where(t.c.card_id == self._id(card_id))).first()
        if row is None:
            raise FileNotFoundError(f"no card {card_id!r}")
        return json.loads(row[0])

    def cards(self, status: str | None = None) -> list[dict[str, Any]]:
        t = S.improvement_cards
        query = sa.select(t.c.doc).order_by(t.c.card_id)
        if status is not None:
            query = query.where(t.c.status == status)
        with self.db.begin() as conn:
            return [json.loads(r[0]) for r in conn.execute(query)]

