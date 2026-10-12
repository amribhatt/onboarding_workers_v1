"""An accepted approval is never dropped: it is on disk before the person is told, it runs once after a restart,
and a shutdown lets accepted work finish. Saves of whole documents are atomic."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import pytest

from id_decision import decide_agent_ids
from scripted_model import call, say
from test_restart import _inbox, _lift, _manager, _wait
from worker import fsutil
from worker.registry import Registry
from worker.service import RunManager, ServiceError


class _NeverRuns:
    """A pool whose work is accepted and then lost: the process died between 'accepted' and 'executed'."""

    def submit(self, *_a: Any, **_k: Any) -> Future[None]:
        return Future()

    def shutdown(self, **_k: Any) -> None:
        return None


def _waiting_run(state: Path) -> tuple[RunManager, str, dict[str, Any]]:
    _inbox(state)
    decide_agent_ids(state / "ids", "Investors")
    m = _manager(state, [say("lifted", _lift()),
                         say("write", call("write_upload", stage_id="s1", template="Investors", why="deliver"))])
    run_id = m.create_run("inv.xlsx", "sponsor-a", "GP-001")["id"]
    return m, run_id, _wait(m, run_id, "needs_approval")["gates"][0]


def test_an_accepted_approval_that_was_never_executed_runs_once_at_the_next_start(tmp_path: Path) -> None:
    state = tmp_path / "state"
    first, run_id, gate = _waiting_run(state)
    first._pool = _NeverRuns()  # type: ignore[assignment]
    first.decide(run_id, gate["hash"], "approve", "checked", reviewer="reviewer-x")  # accepted; never executed
    journal = sorted((state / "runs" / run_id / "approvals").glob("*.json"))
    assert len(journal) == 1 and json.loads(journal[0].read_text())["executed_at"] is None
    assert Registry(state, "sponsor-a").entries("Investors") == []  # nothing happened yet

    second = _manager(state, [say("done")])  # the next start
    assert _wait(second, run_id, "idle")["status"] == "idle"
    assert len(Registry(state, "sponsor-a").entries("Investors")) == 3
    assert (state / "runs" / run_id / "out" / "Investors_Upload.xlsx").is_file() or second.output_path(
        run_id, "Investors_Upload.xlsx").is_file()
    done = json.loads(journal[0].read_text())
    assert done["executed_at"] is not None and done["outcome"] == "done" and done["decision"] == {"type": "approve"}
    second.close()

    third = _manager(state, [say("done")])  # and not again
    assert third.get(run_id)["status"] == "idle"
    assert len(Registry(state, "sponsor-a").entries("Investors")) == 3
    third.close()


def test_a_journaled_decision_whose_gate_is_gone_is_reported_not_guessed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    _inbox(state)
    first = _manager(state, [say("lifted", _lift()), say("done")])
    run_id = first.create_run("inv.xlsx", "sponsor-a", "GP-001")["id"]
    _wait(first, run_id, "idle")
    first.close()
    journal = state / "runs" / run_id / "approvals" / "0001.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"gate_hash": "x", "verdict": "approve", "decision": {"type": "approve"},
                                   "why": "reviewer-x: ok", "meta": {}, "acknowledged": None, "accepted_at": 1.0,
                                   "executed_at": None}), encoding="utf-8")
    second = _manager(state, [say("done")])
    view = second.get(run_id)
    assert view["status"] == "idle" and "nothing was waiting" in view["messages"][-1]["text"]
    assert json.loads(journal.read_text())["outcome"] == "no gate was pending at restart"
    second.close()


def test_a_service_that_is_shutting_down_takes_no_new_work_and_lets_accepted_work_finish(tmp_path: Path) -> None:
    state = tmp_path / "state"
    m, run_id, gate = _waiting_run(state)
    run = m._get(run_id)
    started, release = threading.Event(), threading.Event()

    def slow() -> dict[str, Any]:
        started.set()
        release.wait(5)
        return {"messages": []}

    m._submit(run, slow)
    assert started.wait(5)
    closer = threading.Thread(target=m.close, kwargs={"timeout": 5})
    closer.start()
    deadline = time.time() + 5
    while not m.draining and time.time() < deadline:
        time.sleep(0.01)
    with pytest.raises(ServiceError) as err:
        m.decide(run_id, gate["hash"], "approve", "late")
    assert err.value.status == 503
    with pytest.raises(ServiceError):
        m.create_run("inv.xlsx", "sponsor-a", "GP-001")
    assert closer.is_alive()  # still waiting for the accepted work
    release.set()
    closer.join(5)
    assert not closer.is_alive() and not m._inflight


def test_atomic_write_keeps_the_old_file_when_the_swap_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "doc.json"
    fsutil.atomic_write_text(target, "old")
    monkeypatch.setattr(os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        fsutil.atomic_write_text(target, "new")
    monkeypatch.undo()
    assert target.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["doc.json"]  # no half-written temporary file left
    fsutil.atomic_write_text(target, "né")
    assert target.read_text(encoding="utf-8") == "né"
