"""Alembic environment: the engine comes from `worker.db.migrate` (online only; no autogenerate, no ORM)."""

from alembic import context

config = context.config
engine = config.attributes["engine"]

with engine.begin() as connection:
    context.configure(connection=connection, target_metadata=None, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()
