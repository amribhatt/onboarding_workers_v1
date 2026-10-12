"""One switch picks the layout-program artifact store: the folder without a database, the insert-only tables with one."""

from __future__ import annotations

from pathlib import Path

import pytest

from worker import persist
from worker.db.core import Db
from worker.flex.artifacts import FileArtifactStore, SqlArtifactStore


def test_the_artifact_store_follows_the_database_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)
    assert isinstance(persist.artifact_store(tmp_path / "a"), FileArtifactStore)
    url = f"sqlite:///{(tmp_path / 'ow.db').as_posix()}"
    db = Db(url)
    db.create_all()
    db.dispose()
    monkeypatch.setenv("DATABASE_URL", url)
    assert isinstance(persist.artifact_store(tmp_path / "a"), SqlArtifactStore)
