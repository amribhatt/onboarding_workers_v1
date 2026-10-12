"""Transform artifacts and their approvals: insert-only tables.

Idempotent on purpose: 0001 creates every table of `worker.db.schema` it finds, so a database made fresh already has
these two; a database at 0002 gets them here. Either way they end up refusing UPDATE and DELETE (triggers on both
dialects) and, on PostgreSQL, with the app role's UPDATE, DELETE and TRUNCATE revoked.
"""

import os

import sqlalchemy as sa
from alembic import op

from worker.db import schema

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("WORKER_DB_APP_ROLE", "ow_app")


def upgrade() -> None:
    bind = op.get_bind()
    tables = [schema.metadata.tables[n] for n in schema.APPEND_ONLY_TABLES]
    schema.metadata.create_all(bind, tables=tables, checkfirst=True)
    for t in schema.APPEND_ONLY_TABLES:
        if bind.dialect.name == "sqlite":
            for event in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_{event.lower()} BEFORE {event} ON {t} "
                           f"BEGIN SELECT RAISE(ABORT, '{t} is insert-only: {event} refused'); END")
        elif bind.dialect.name == "postgresql":
            op.execute("CREATE OR REPLACE FUNCTION insert_only_refuse() RETURNS trigger LANGUAGE plpgsql AS "
                       "$$ BEGIN RAISE EXCEPTION '% is insert-only: % refused', TG_TABLE_NAME, TG_OP; END $$")
            op.execute(f"DROP TRIGGER IF EXISTS insert_only ON {t}")
            op.execute(f"CREATE TRIGGER insert_only BEFORE UPDATE OR DELETE ON {t} "
                       "FOR EACH ROW EXECUTE FUNCTION insert_only_refuse()")
            op.execute(f"DROP TRIGGER IF EXISTS insert_only_truncate ON {t}")
            op.execute(f"CREATE TRIGGER insert_only_truncate BEFORE TRUNCATE ON {t} "
                       "FOR EACH STATEMENT EXECUTE FUNCTION insert_only_refuse()")
            op.execute(f'REVOKE UPDATE, DELETE, TRUNCATE ON {t} FROM "{APP_ROLE}"')
            op.execute(f'GRANT SELECT, INSERT ON {t} TO "{APP_ROLE}"')


def downgrade() -> None:
    for t in reversed(schema.APPEND_ONLY_TABLES):
        op.execute(sa.text(f"DROP TABLE IF EXISTS {t}"))
