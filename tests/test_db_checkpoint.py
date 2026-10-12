"""The paused gate survives a restart with the checkpoints in PostgreSQL (the saver the container uses)."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from conftest import PgSession
from scripted_model import call, say
from test_restart import _inbox, _lift, _manager, _wait
from worker import persist
from worker.db.checkpoint import conninfo


def test_conninfo_drops_the_driver_name_and_keeps_the_password() -> None:
    assert conninfo("postgresql+psycopg://u:p%40ss@h:5432/d") == "postgresql://u:p%40ss@h:5432/d"


@pytest.mark.integration
def test_a_paused_gate_survives_a_restart_on_the_postgres_saver(pg: PgSession, tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", pg.app_url)
    state = tmp_path / "state"
    _inbox(state)
    first = _manager(state, [say("lifted", _lift()),
                             say("write", call("write_upload", stage_id="s1", template="Investors", why="deliver"))])
    run_id = first.create_run("inv.xlsx", "sponsor-a", "GP-001")["id"]
    gate = _wait(first, run_id, "needs_approval")["gates"][0]
    first.close()
    assert not (state / "checkpoints.sqlite").exists()  # the checkpoints are in the database
    with pg.owner.begin() as conn:
        assert conn.execute(sa.text("SELECT count(*) FROM checkpoints")).scalar_one() > 0

    second = _manager(state, [say("done")])  # a new process: nothing in memory
    assert second.get(run_id)["gates"][0]["hash"] == gate["hash"]
    second.decide(run_id, gate["hash"], "approve", "checked")
    _wait(second, run_id, "idle")
    assert len(persist.registry(state, "sponsor-a").entries("Investors")) == 3  # in the database too
    second.close()
