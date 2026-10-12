"""SQL views that answer the graph questions without Neo4j: where an ID came from, and which account a source
account maps to now. Neo4j (when `WORKER_GRAPH=on`) is a projection of the same rows and is checked against these.
"""

import os

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

APP_ROLE = os.environ.get("WORKER_DB_APP_ROLE", "ow_app")
FIELDS = ("key", "name", "source_key", "source", "fund", "batch", "job", "label")


def _json(dialect: str, column: str, field: str) -> str:
    if dialect == "postgresql":
        return f"(({column})::jsonb ->> '{field}')"
    return f"json_extract({column}, '$.{field}')"


def upgrade() -> None:
    d = op.get_bind().dialect.name
    cols = ", ".join(f"coalesce({_json(d, 'doc', f)}, '') AS {f}" for f in FIELDS)
    op.execute(f"CREATE VIEW id_lineage_v AS SELECT id, sponsor, template, {cols} FROM registry_entries")
    acct = ", ".join(f"coalesce({_json(d, 'm.doc', f)}, '') AS {f}" for f in ("account", "source", "reason"))
    op.execute(
        f"CREATE VIEW account_current_v AS SELECT m.id, m.store, m.key, {acct} FROM account_mappings m "
        "WHERE m.id = (SELECT max(n.id) FROM account_mappings n WHERE n.store = m.store AND n.key = m.key)")
    if d == "postgresql":
        for view in ("id_lineage_v", "account_current_v"):
            op.execute(f'GRANT SELECT ON {view} TO "{APP_ROLE}"')


def downgrade() -> None:
    op.execute("DROP VIEW account_current_v")
    op.execute("DROP VIEW id_lineage_v")
