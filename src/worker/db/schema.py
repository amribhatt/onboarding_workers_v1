"""The schema, declared once as SQLAlchemy Core metadata (portable: SQLite for tests, PostgreSQL in production).

Rules:

* `audit_log` is insert and select only. No `update()` or `delete()` is ever built for it (see
  `tests/test_db_schema.py`), and the migration adds storage-level refusals on top (triggers on both dialects;
  revoked privileges and the "audit row in the same transaction" rule on PostgreSQL).
* Every tenant table leads its primary key with `sponsor`.
* Documents are JSON text (`Text`), so a hash taken in Python is the hash that is stored: the database never
  normalises what was hashed.
"""

from __future__ import annotations

import sqlalchemy as sa

metadata = sa.MetaData()

_ID = sa.BigInteger().with_variant(sa.Integer, "sqlite")

# ---- audit: append only --------------------------------------------------------------------------------
audit_log = sa.Table(
    "audit_log", metadata,
    sa.Column("stream", sa.String(300), primary_key=True),
    sa.Column("seq", sa.BigInteger, primary_key=True, autoincrement=False),
    sa.Column("ts", sa.String(40), nullable=False),
    sa.Column("action", sa.String(120), nullable=False),
    sa.Column("prev_hash", sa.String(64), nullable=False),
    sa.Column("hash", sa.String(64), nullable=False),
    sa.Column("body", sa.Text, nullable=False),  # the exact canonical text that `hash` covers (without `hash`)
    sa.Column("txid", sa.BigInteger),  # PostgreSQL: txid_current() by default; the "audit before effect" rule
    sa.Index("audit_log_hash", "hash"),
    sa.Index("audit_log_action", "action", "ts"),
    sa.Index("audit_log_txid", "txid"),
)

# ---- the sponsor's records and IDs ---------------------------------------------------------------------
registry_entries = sa.Table(
    "registry_entries", metadata,
    sa.Column("id", _ID, primary_key=True, autoincrement=True),
    sa.Column("sponsor", sa.String(60), nullable=False),
    sa.Column("template", sa.String(60), nullable=False),
    sa.Column("key_kind", sa.String(20), nullable=False),
    sa.Column("dedupe", sa.String(500), nullable=False),  # lower(key or name): one record, one row
    sa.Column("doc", sa.Text, nullable=False),
    sa.UniqueConstraint("sponsor", "template", "key_kind", "dedupe", name="registry_unique"),
)

id_counters = sa.Table(
    "id_counters", metadata,
    sa.Column("sponsor", sa.String(60), primary_key=True),
    sa.Column("record_type", sa.String(60), primary_key=True),
    sa.Column("scheme", sa.Text, nullable=False),
    sa.Column("counter", sa.BigInteger, nullable=False),
    sa.Column("high_water", sa.BigInteger, nullable=False),
    sa.CheckConstraint("counter >= 1", name="counter_positive"),
    sa.CheckConstraint("high_water >= counter", name="high_water_ahead"),
)

id_reservations = sa.Table(
    "id_reservations", metadata,
    sa.Column("sponsor", sa.String(60), primary_key=True),
    sa.Column("reservation_key", sa.String(64), primary_key=True),
    sa.Column("record_type", sa.String(60), nullable=False),
    sa.Column("first", sa.BigInteger, nullable=False),
    sa.Column("next", sa.BigInteger, nullable=False),
    sa.Column("ids", sa.Text, nullable=False),
    sa.Column("taken", sa.Text, nullable=False),
    sa.Column("scheme", sa.Text, nullable=False),
    sa.Column("status", sa.String(12), nullable=False),  # reserved | committed | released
    sa.Column("created_at", sa.String(40), nullable=False),
    sa.Column("closed_at", sa.String(40)),
    sa.CheckConstraint("status in ('reserved','committed','released')", name="reservation_status"),
)

# One ID, one reservation: the hard guarantee under concurrency, even if a code path is wrong.
issued_ids = sa.Table(
    "issued_ids", metadata,
    sa.Column("sponsor", sa.String(60), primary_key=True),
    sa.Column("record_type", sa.String(60), primary_key=True),
    sa.Column("id_norm", sa.String(300), primary_key=True),
    sa.Column("reservation_key", sa.String(64), nullable=False),
)

world_commits = sa.Table(
    "world_commits", metadata,
    sa.Column("sponsor", sa.String(60), primary_key=True),
    sa.Column("token", sa.String(200), primary_key=True),
    sa.Column("job_id", sa.String(200), nullable=False),
    sa.Column("committed_at", sa.String(40), nullable=False),
)

account_mappings = sa.Table(
    "account_mappings", metadata,
    sa.Column("id", _ID, primary_key=True, autoincrement=True),
    sa.Column("store", sa.String(60), nullable=False),  # `_golden` or a sponsor
    sa.Column("key", sa.String(500), nullable=False),
    sa.Column("doc", sa.Text, nullable=False),
    sa.Index("account_mappings_key", "store", "key", "id"),
)

versions = sa.Table(
    "versions", metadata,
    sa.Column("name", sa.String(120), primary_key=True),
    sa.Column("version", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("note", sa.Text, nullable=False),
    sa.Column("published", sa.String(40), nullable=False),
    sa.Column("content", sa.Text, nullable=False),
)

version_pointer = sa.Table(
    "version_pointer", metadata,
    sa.Column("name", sa.String(120), primary_key=True),
    sa.Column("version", sa.Integer, nullable=False),
)

# ---- memory, recipes, controls -------------------------------------------------------------------------
memory_rules = sa.Table(
    "memory_rules", metadata,
    sa.Column("sponsor", sa.String(60), primary_key=True),
    sa.Column("bucket", sa.String(10), primary_key=True),  # pending | approved
    sa.Column("rule_id", sa.String(40), primary_key=True),
    sa.Column("ord", sa.Integer, nullable=False),
    sa.Column("doc", sa.Text, nullable=False),
)

memory_cases = sa.Table(
    "memory_cases", metadata,
    sa.Column("sponsor", sa.String(60), primary_key=True),
    sa.Column("case_id", sa.String(120), primary_key=True),
    sa.Column("doc", sa.Text, nullable=False),
)

legacy_recipes = sa.Table(
    "legacy_recipes", metadata,
    sa.Column("sponsor", sa.String(60), primary_key=True),
    sa.Column("record_type", sa.String(60), primary_key=True),
    sa.Column("version", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("doc", sa.Text, nullable=False),
)

field_recipes = sa.Table(
    "field_recipes", metadata,
    sa.Column("sponsor", sa.String(60), primary_key=True),
    sa.Column("record_type", sa.String(60), primary_key=True),
    sa.Column("version", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("text", sa.Text, nullable=False),
    sa.Column("created_at", sa.String(40), nullable=False),
)

# Which version is in force (0 = none). A revert moves this; no version is ever deleted.
field_recipe_current = sa.Table(
    "field_recipe_current", metadata,
    sa.Column("sponsor", sa.String(60), primary_key=True),
    sa.Column("record_type", sa.String(60), primary_key=True),
    sa.Column("version", sa.Integer, nullable=False),
)

improvement_cards = sa.Table(
    "improvement_cards", metadata,
    sa.Column("card_id", sa.String(40), primary_key=True),
    sa.Column("status", sa.String(40), nullable=False),
    sa.Column("doc", sa.Text, nullable=False),
)

# ---- the engine's store (checkpoints and the spool) ----------------------------------------------------
store_chunks = sa.Table(
    "store_chunks", metadata,
    sa.Column("key", sa.String(500), primary_key=True),
    sa.Column("n", sa.Integer, primary_key=True, autoincrement=False),
    sa.Column("data", sa.LargeBinary, nullable=False),
)

store_seals = sa.Table(
    "store_seals", metadata,
    sa.Column("key", sa.String(500), primary_key=True),
    sa.Column("digest", sa.String(64), nullable=False),
)

# The console's one state document. Not a guarded table: every console action saves it, and the actions that
# decide something are audited on their own streams. A stale writer is refused (compare and set on `row_version`).
console_state = sa.Table(
    "console_state", metadata,
    sa.Column("id", sa.SmallInteger, primary_key=True, autoincrement=False),
    sa.Column("row_version", sa.BigInteger, nullable=False),
    sa.Column("doc", sa.Text, nullable=False),
)

# ---- transform artifacts (ticket 11): insert only, never edited --------------------------------------------
flex_artifacts = sa.Table(
    "flex_artifacts", metadata,
    sa.Column("sponsor", sa.String(60), primary_key=True),
    sa.Column("artifact_id", sa.String(80), primary_key=True),
    sa.Column("record_type", sa.String(60), nullable=False),
    sa.Column("input_sig", sa.String(64), nullable=False),
    sa.Column("parent", sa.String(80), nullable=False),
    sa.Column("doc", sa.Text, nullable=False),
    sa.Index("flex_artifacts_shape", "sponsor", "record_type", "input_sig"),
)

flex_decisions = sa.Table(  # approvals and retirements
    "flex_decisions", metadata,
    sa.Column("sponsor", sa.String(60), primary_key=True),
    sa.Column("decision_id", sa.String(80), primary_key=True),
    sa.Column("artifact_id", sa.String(80), nullable=False),
    sa.Column("doc", sa.Text, nullable=False),
    sa.Index("flex_decisions_artifact", "sponsor", "artifact_id"),
)
APPEND_ONLY_TABLES = ("flex_artifacts", "flex_decisions")

# Every table a decision changes. On PostgreSQL each refuses a write when no audit row exists in the transaction.
GUARDED_TABLES = (
    "registry_entries", "id_counters", "id_reservations", "issued_ids", "world_commits", "account_mappings",
    "versions", "version_pointer", "memory_rules", "memory_cases", "legacy_recipes", "field_recipes",
    "field_recipe_current", "improvement_cards",
)
