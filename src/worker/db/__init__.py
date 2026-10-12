"""Relational persistence: one SQLAlchemy Core schema, adapters behind the ports, migrations, verifier, anchors.

File-backed state stays the default; this package is used only when `DATABASE_URL` is set (`worker.db.backend`).
"""
