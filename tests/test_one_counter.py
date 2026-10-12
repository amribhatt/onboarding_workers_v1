"""One ID counter per sponsor and record type, owned by `worker.idgen`, used by the console and the agent path.

The same tests run on files, on SQLite and (integration) on the PostgreSQL of the compose stack. The counter
moves only at an approved write, a stale reservation is refused rather than repeated, and an old console state that
kept its own `ctr` opens with that number taken over, never lowered.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from langgraph.checkpoint.memory import InMemorySaver

import worker.console_engine as CE
from scripted_model import ScriptedModel, approve, call, say
from worker import persist
from worker.agent import build_agent, pending_gate, start
from worker.audit import AuditLog
from worker.console import Console, ConsoleError
from worker.context import RunContext
from worker.db.core import Db
from worker.idgen import CounterMoved
from worker.persist import id_scheme
from worker.staging import Stage
from worker.templates import TEMPLATES

SPONSOR = "Sponsor A"
SLUG = "sponsor-a"
TYPE = "Investors"


@pytest.fixture(params=["file", "sqlite", pytest.param("postgres", marks=pytest.mark.integration)])
def mode(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Where the counter lives: the state folder, a SQLite database, or the compose PostgreSQL."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL_FILE", raising=False)
    if request.param == "sqlite":
        url = f"sqlite:///{(tmp_path / 'ow.db').as_posix()}"
        db = Db(url)
        db.create_all()
        db.dispose()
        monkeypatch.setenv("DATABASE_URL", url)
    elif request.param == "postgres":
        monkeypatch.setenv("DATABASE_URL", request.getfixturevalue("pg").app_url)
    yield request.param


def _console(tmp_path: Path) -> Console:
    return Console(tmp_path / "inbox", tmp_path / "console.json")  # the state folder is tmp_path: ids in tmp_path/ids


def _approve_console_investors(c: Console) -> list[str]:
    """The sample investors job, approved by a named person; the IDs it wrote, in order."""
    c.act(f"start|{SPONSOR}|{TYPE}")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    c.act("ex|no")
    c.act("go")
    c.act("acceptall")
    c.act("approve", who="reviewer-a")
    assert j["approved"]
    return [v for _k, v in sorted(CE.out_ids(c, j)[0].items())]


def _agent_investors(tmp_path: Path, tag: str, names: list[str], approve_it: bool = True) -> list[str]:
    """One agent run for this sponsor: lift, assign IDs from the counter, write (gated). The IDs it assigned."""
    inbox = tmp_path / "inbox_agent"
    inbox.mkdir(exist_ok=True)
    book = openpyxl.Workbook()
    ws = book.active
    ws.append(["Investor ID", "Name"])
    for n in names:
        ws.append([f"SRC-{n}", f"Name {n}"])
    book.save(inbox / f"{tag}.xlsx")
    out = tmp_path / f"out{tag}"
    ctx = RunContext(sponsor=SLUG, gp_id="GP-001", inbox_roots=[inbox], out_dir=out,
                     audit=AuditLog(tmp_path / f"audit{tag}.jsonl"), stage=Stage(out), id_root=tmp_path / "ids")
    script = [
        say("lift", call("lift", path=f"{tag}.xlsx", sheet="Sheet", template=TYPE, header_row=1, first_row=2,
                         columns={"GP_ID": "A", "Investor_ID": "A", "Investor_Name": "B"}, why="copy")),
        say("ids", call("assign_ids", stage_id="s1", id_field="Investor_ID", source_key_field="Source_Investor_Key",
                        why="new IDs from the counter")),
        say("write", call("write_upload", stage_id="s2", template=TYPE, why="deliver")),
        say("done"),
    ]
    agent = build_agent(ctx, ScriptedModel(script=script), InMemorySaver())
    result = start(agent, ctx, "build investors", f"t{tag}")
    assert [g["name"] for g in pending_gate(result)] == ["write_upload"]
    if approve_it:
        approve(agent, ctx, f"t{tag}")
    return [r["Investor_ID"] for r in ctx.stage.rows("s2")]


def _store(tmp_path: Path) -> Any:
    return id_scheme(tmp_path / "ids", SLUG, TYPE, TEMPLATES[TYPE].id_generation)


def _number(ident: str) -> int:
    return int(ident.rsplit("-", 1)[1])


def _decided(tmp_path: Path) -> None:
    """A named person's decision: number them in sequence (one decision, read by both paths)."""
    _store(tmp_path).change({"mode": "sequential", "prefix": "GINV-", "width": 4, "start": 1}, by="reviewer-a",
                            reason="the sponsor's standard numbering", record=lambda *a: None)


# ---- console and agent are one counter ----------------------------------------------------------------------------
def test_console_and_agent_issue_disjoint_continuing_ids(mode: str, tmp_path: Path) -> None:
    _decided(tmp_path)
    first = _agent_investors(tmp_path, "a1", ["1", "2", "3"])
    c = _console(tmp_path)
    from_console = _approve_console_investors(c)
    second = _agent_investors(tmp_path, "a2", ["4", "5"])
    nums = [_number(i) for i in first + from_console + second]
    assert nums == list(range(1, len(nums) + 1))  # continuing, nothing repeated, nothing skipped
    assert from_console and len(set(first + from_console + second)) == len(nums)
    assert c.counter_of(SPONSOR, TYPE) == _store(tmp_path).counter == len(nums) + 1


def test_the_counter_survives_a_restart_of_either_side(mode: str, tmp_path: Path) -> None:
    _decided(tmp_path)
    ids = _approve_console_investors(_console(tmp_path))
    reopened = _console(tmp_path)  # a new process: only the stores remain
    assert reopened.counter_of(SPONSOR, TYPE) == len(ids) + 1 == _store(tmp_path).counter
    assert [_number(i) for i in _agent_investors(tmp_path, "a1", ["1", "2"])] == [len(ids) + 1, len(ids) + 2]
    assert _console(tmp_path).counter_of(SPONSOR, TYPE) == len(ids) + 3


def test_a_rejected_agent_run_and_an_unapproved_console_job_move_nothing(mode: str, tmp_path: Path) -> None:
    _decided(tmp_path)
    c = _console(tmp_path)
    c.act(f"start|{SPONSOR}|{TYPE}")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    c.act("ex|no")
    c.act("go")
    assert _agent_investors(tmp_path, "a1", ["1", "2"], approve_it=False)
    assert c.counter_of(SPONSOR, TYPE) == 1


# ---- an old console state that kept `ctr` -------------------------------------------------------------------------
def _old_state(tmp_path: Path, ctr: dict[str, int]) -> None:
    document = persist.console_document(tmp_path / "console.json")  # the file, or the database row
    doc = json.loads(document.load())
    doc["ctr"] = ctr
    document.save(json.dumps(doc))


def test_an_old_state_with_ctr_opens_and_its_counter_is_taken_over_once(mode: str, tmp_path: Path) -> None:
    _decided(tmp_path)
    c = _console(tmp_path)
    c.save()
    _old_state(tmp_path, {f"{SPONSOR}|{TYPE}": 40, "Sponsor B|Investors": 6})
    opened = _console(tmp_path)
    assert "ctr" not in opened.S
    assert opened.counter_of(SPONSOR, TYPE) == 40 and opened.counter_of("Sponsor B", TYPE) == 6
    assert [_number(i) for i in _agent_investors(tmp_path, "a1", ["1", "2"])] == [40, 41]  # no reuse of 1..39
    opened.save()
    again = _console(tmp_path)  # opened again: taken over once, nothing moves
    assert again.counter_of(SPONSOR, TYPE) == 42


def test_an_old_counter_never_lowers_the_agents(mode: str, tmp_path: Path) -> None:
    _decided(tmp_path)
    c = _console(tmp_path)
    c.save()
    _agent_investors(tmp_path, "a1", ["1", "2", "3"])  # the agent's counter is at 4
    _old_state(tmp_path, {f"{SPONSOR}|{TYPE}": 2})
    assert _console(tmp_path).counter_of(SPONSOR, TYPE) == 4


# ---- concurrent jobs ----------------------------------------------------------------------------------------------
def test_two_reservations_from_one_place_cannot_both_commit(mode: str, tmp_path: Path) -> None:
    _decided(tmp_path)
    a, b = _store(tmp_path).reserve(), _store(tmp_path).reserve()
    mine = [a.take() for _ in range(3)]
    theirs = [b.take() for _ in range(3)]
    assert mine == theirs  # both read the same counter: the numbers would repeat
    _store(tmp_path).commit(a)
    with pytest.raises(CounterMoved):
        _store(tmp_path).commit(b)
    assert _store(tmp_path).counter == 4  # the refused job moved nothing
    fresh = _store(tmp_path).reserve()
    assert fresh.take() not in mine


def test_racing_commits_let_exactly_one_win(mode: str, tmp_path: Path) -> None:
    _decided(tmp_path)
    rsvs = [_store(tmp_path).reserve() for _ in range(6)]
    for r in rsvs:
        r.take()
    results: list[str] = []
    gate = threading.Barrier(len(rsvs))

    def run(r: Any) -> None:
        gate.wait()
        try:
            _store(tmp_path).commit(r)
            results.append("ok")
        except CounterMoved:
            results.append("moved")

    threads = [threading.Thread(target=run, args=(r,)) for r in rsvs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["moved"] * 5 + ["ok"] and _store(tmp_path).counter == 2


def test_a_console_job_whose_numbers_were_taken_meanwhile_is_not_approved_and_then_gets_new_ones(
        mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _decided(tmp_path)
    c = _console(tmp_path)
    c.act(f"start|{SPONSOR}|{TYPE}")
    c.act("ask_set|cov|all")
    c.act("ask_sample")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    c.act("ex|no")
    c.act("go")
    c.act("acceptall")
    real = CE.write_upload
    other: dict[str, list[str]] = {}

    def racing(con: Any, job: dict[str, Any]) -> Any:
        out = real(con, job)  # the file is there; now a second job (the agent) commits from the same counter
        st = _store(tmp_path)
        r = st.reserve()
        other["ids"] = [r.take() for _ in range(2)]
        st.commit(r)
        return out

    monkeypatch.setattr(CE, "write_upload", racing)
    with pytest.raises(ConsoleError, match="counter moved"):
        c.act("approve", who="reviewer-a")
    assert not j.get("approved") and c.counter_of(SPONSOR, TYPE) == 3  # only the other job's two numbers
    monkeypatch.setattr(CE, "write_upload", real)
    c.act("approve", who="reviewer-a")
    ids = [v for _k, v in sorted(CE.out_ids(c, j)[0].items())]
    assert j["approved"] and not set(ids) & set(other["ids"])
    assert [_number(i) for i in ids] == list(range(3, 3 + len(ids)))
    assert c.counter_of(SPONSOR, TYPE) == 3 + len(ids)
