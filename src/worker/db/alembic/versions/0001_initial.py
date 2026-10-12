"""Initial schema, the append-only guarantee on the audit table, and the "audit before effect" rule.

The tables come from `worker.db.schema` as they were when this migration was written; a later change is a new
migration made of explicit operations, never an edit of this one.

PostgreSQL adds, on top of the tables:

* triggers that refuse UPDATE, DELETE and TRUNCATE of `audit_log` for every role (the owner too);
* a BEFORE INSERT trigger that re-checks the hash against the stored text, the columns against the text, and the
  link to the previous row, so a broken chain cannot be inserted;
* `require_audit_in_txn` on every state table a decision changes: a write is refused unless an audit row exists in
  the same transaction (`audit_log.txid = txid_current()`);
* REVOKE of UPDATE, DELETE and TRUNCATE on `audit_log` from PUBLIC and from the app role;
* event triggers that refuse dropping `audit_log` and disabling its triggers (needs a superuser; skipped with a
  notice otherwise).

SQLite (tests) gets the append-only triggers only.
"""

import os

import sqlalchemy as sa
from alembic import op

from worker.db import schema

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("WORKER_DB_APP_ROLE", "ow_app")

_POSTGRES_AUDIT = """
CREATE FUNCTION audit_link() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE prev text; b jsonb := NEW.body::jsonb;
BEGIN
  IF encode(sha256(convert_to(NEW.body, 'UTF8')), 'hex') <> NEW.hash THEN
    RAISE EXCEPTION 'audit: hash does not match body (%/%)', NEW.stream, NEW.seq; END IF;
  IF (b->>'seq')::bigint <> NEW.seq OR b->>'prev_hash' <> NEW.prev_hash OR b->>'action' <> NEW.action THEN
    RAISE EXCEPTION 'audit: columns disagree with body (%/%)', NEW.stream, NEW.seq; END IF;
  IF NEW.seq = 1 THEN prev := repeat('0', 64);
  ELSE SELECT hash INTO prev FROM audit_log WHERE stream = NEW.stream AND seq = NEW.seq - 1; END IF;
  IF prev IS NULL OR prev <> NEW.prev_hash THEN
    RAISE EXCEPTION 'audit: chain broken at %/%', NEW.stream, NEW.seq; END IF;
  RETURN NEW;
END $$;
"""

_POSTGRES_REFUSE = """
CREATE FUNCTION audit_refuse() RETURNS trigger LANGUAGE plpgsql AS
$$ BEGIN RAISE EXCEPTION 'audit_log is append-only: % refused', TG_OP; END $$;
"""

_POSTGRES_REQUIRE = """
CREATE FUNCTION require_audit_in_txn() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM audit_log WHERE txid = txid_current()) THEN
    RAISE EXCEPTION 'no audit row in this transaction: % on % refused', TG_OP, TG_TABLE_NAME;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;
"""

_POSTGRES_DDL_GUARD = """
DO $do$
BEGIN
  IF current_setting('is_superuser') = 'on' THEN
    CREATE FUNCTION audit_ddl_drop_guard() RETURNS event_trigger LANGUAGE plpgsql AS $$
    DECLARE r record;
    BEGIN
      FOR r IN SELECT * FROM pg_event_trigger_dropped_objects() LOOP
        IF r.object_name = 'audit_log' THEN
          RAISE EXCEPTION 'audit_log cannot be dropped'; END IF;
      END LOOP;
    END $$;
    CREATE EVENT TRIGGER audit_no_drop ON sql_drop EXECUTE FUNCTION audit_ddl_drop_guard();
    CREATE FUNCTION audit_ddl_trigger_guard() RETURNS event_trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF EXISTS (SELECT 1 FROM pg_trigger t WHERE t.tgrelid = 'audit_log'::regclass
                 AND t.tgname LIKE 'audit_%' AND t.tgenabled <> 'O') THEN
        RAISE EXCEPTION 'the audit_log triggers cannot be disabled'; END IF;
    END $$;
    CREATE EVENT TRIGGER audit_triggers_stay_on ON ddl_command_end WHEN TAG IN ('ALTER TABLE')
      EXECUTE FUNCTION audit_ddl_trigger_guard();
  ELSE
    RAISE NOTICE 'not a superuser: the audit DDL event triggers were not created';
  END IF;
END $do$;
"""


def _postgres_grants(bind: sa.engine.Connection) -> None:
    role = APP_ROLE
    if not role.replace("_", "").isalnum():
        raise ValueError(f"bad role name {role!r}")
    exists = bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first()
    if not exists:
        bind.exec_driver_sql(f'CREATE ROLE "{role}" LOGIN')
    password = os.environ.get("WORKER_DB_APP_PASSWORD")
    if not password and os.environ.get("WORKER_DB_APP_PASSWORD_FILE"):
        with open(os.environ["WORKER_DB_APP_PASSWORD_FILE"], encoding="utf-8") as fh:
            password = fh.read().strip()
    if password:
        literal = sa.literal(password).compile(dialect=bind.dialect, compile_kwargs={"literal_binds": True})
        bind.exec_driver_sql(f'ALTER ROLE "{role}" PASSWORD {literal}')
    bind.exec_driver_sql("REVOKE ALL ON audit_log FROM PUBLIC")
    bind.exec_driver_sql(f'GRANT SELECT, INSERT ON audit_log TO "{role}"')
    bind.exec_driver_sql(f'REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON audit_log FROM "{role}"')
    bind.exec_driver_sql(f'GRANT SELECT ON alembic_version TO "{role}"')  # readiness compares it with the build
    others = [t.name for t in schema.metadata.sorted_tables if t.name != "audit_log"]
    for name in others:
        bind.exec_driver_sql(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{name}" TO "{role}"')
    bind.exec_driver_sql(f'GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO "{role}"')


def upgrade() -> None:
    bind = op.get_bind()
    schema.metadata.create_all(bind)
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE audit_log ALTER COLUMN txid SET DEFAULT txid_current()")
        op.execute(_POSTGRES_AUDIT)
        op.execute(_POSTGRES_REFUSE)
        op.execute(_POSTGRES_REQUIRE)
        op.execute("CREATE TRIGGER audit_link BEFORE INSERT ON audit_log FOR EACH ROW EXECUTE FUNCTION audit_link()")
        op.execute("CREATE TRIGGER audit_no_change BEFORE UPDATE OR DELETE ON audit_log "
                   "FOR EACH ROW EXECUTE FUNCTION audit_refuse()")
        op.execute("CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit_log "
                   "FOR EACH STATEMENT EXECUTE FUNCTION audit_refuse()")
        for name in schema.GUARDED_TABLES:
            op.execute(f"CREATE TRIGGER require_audit BEFORE INSERT OR UPDATE OR DELETE ON {name} "
                       "FOR EACH ROW EXECUTE FUNCTION require_audit_in_txn()")
        _postgres_grants(bind)
        op.execute(_POSTGRES_DDL_GUARD)
    elif bind.dialect.name == "sqlite":
        for event in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER audit_no_{event.lower()} BEFORE {event} ON audit_log "
                       f"BEGIN SELECT RAISE(ABORT, 'audit_log is append-only: {event} refused'); END")


def downgrade() -> None:
    raise RuntimeError("the first migration is not reversible: restore from a backup, or export and re-import")
