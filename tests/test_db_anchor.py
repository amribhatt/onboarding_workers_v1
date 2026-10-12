"""The verifier and the anchor: a consistent rewrite of a whole chain passes the chain check and is caught by the
anchor; a deleted or replaced anchor is caught by the anchors' own chain; the anchor sink never overwrites."""

from __future__ import annotations

import hashlib
import io
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from conftest import PgSession
from worker.cli import main
from worker.db import anchor as A
from worker.db import verify as V
from worker.db.core import Db, SqlAuditLog
from worker.hashing import canonical_strict


def _fill(db: Db) -> None:
    for stream in ("runs/a/audit.jsonl", "feedback/sponsor-a/feedback.jsonl"):
        log = SqlAuditLog(db, stream)
        for i in range(4):
            log.record("tick", f"entry {i}", {"i": i})


def _rewrite_consistently(conn: sa.engine.Connection, stream: str, from_seq: int) -> None:
    """What a superuser could do: change entry `from_seq`'s content and recompute every hash after it."""
    rows = conn.execute(sa.text("SELECT seq, body FROM audit_log WHERE stream = :s AND seq >= :q ORDER BY seq"),
                        {"s": stream, "q": from_seq}).all()
    prev = conn.execute(sa.text("SELECT hash FROM audit_log WHERE stream = :s AND seq = :q"),
                        {"s": stream, "q": from_seq - 1}).scalar_one()
    for seq, body in rows:
        doc = json.loads(body)
        doc["prev_hash"] = prev
        if seq == from_seq:
            doc["rationale"] = "rewritten"
        text = canonical_strict(doc)
        prev = hashlib.sha256(text.encode()).hexdigest()
        conn.execute(sa.text("UPDATE audit_log SET body = :b, hash = :h, prev_hash = :p WHERE stream = :s "
                             "AND seq = :q"), {"b": text, "h": prev, "p": doc["prev_hash"], "s": stream, "q": seq})


def test_a_clean_database_verifies_and_matches_its_anchor(tmp_path: Path) -> None:
    db = Db("sqlite://")
    db.create_all()
    _fill(db)
    sink = A.DirectorySink(tmp_path / "anchors")
    doc = A.write(db, sink)
    assert doc["number"] == 1 and {s["stream"] for s in doc["streams"]} == {
        "runs/a/audit.jsonl", "feedback/sponsor-a/feedback.jsonl"}
    report = V.verify_streams(db)
    assert report.ok and sum(r.checked for r in report.streams) == 9  # 8 entries + the anchor note
    assert A.check(db, sink, max_age_seconds=60) == []
    SqlAuditLog(db, "runs/a/audit.jsonl").record("tick", "later")
    second = A.write(db, sink)
    assert second["number"] == 2 and second["previous_sha256"] == A.sha_of(sink.read(sink.names()[0]))
    assert A.check(db, sink) == []


def test_a_consistent_rewrite_passes_the_chain_check_and_fails_the_anchor(tmp_path: Path) -> None:
    db = Db("sqlite://")
    db.create_all()
    _fill(db)
    sink = A.DirectorySink(tmp_path / "anchors")
    A.write(db, sink)
    with db.begin() as conn:
        conn.execute(sa.text("DROP TRIGGER audit_no_update"))
        _rewrite_consistently(conn, "runs/a/audit.jsonl", 2)
    assert V.verify_streams(db, only=["runs/a/audit.jsonl"]).ok  # the chain alone cannot see it
    problems = A.check(db, sink)
    assert {p["code"] for p in problems} == {"history_rewritten"}
    assert problems[0]["stream"] == "runs/a/audit.jsonl"


def test_rows_removed_from_the_end_are_behind_the_anchor_not_silently_gone(tmp_path: Path) -> None:
    db = Db("sqlite://")
    db.create_all()
    _fill(db)
    sink = A.DirectorySink(tmp_path / "anchors")
    A.write(db, sink)
    with db.begin() as conn:
        conn.execute(sa.text("DROP TRIGGER audit_no_delete"))
        conn.execute(sa.text("DELETE FROM audit_log WHERE stream = 'runs/a/audit.jsonl' AND seq > 2"))
    assert V.verify_streams(db).ok  # what is left is a valid chain
    (problem,) = A.check(db, sink)
    assert problem["code"] == "behind_anchor" and problem["entries_behind"] == 2


def test_the_restore_drill_reports_a_backup_older_than_the_anchor_and_fails_on_a_rewrite(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'ow.db').as_posix()}"
    db = Db(url)
    db.create_all()
    _fill(db)
    A.write(db, A.DirectorySink(tmp_path / "anchors"))
    with db.begin() as conn:
        conn.execute(sa.text("DROP TRIGGER audit_no_delete"))
        conn.execute(sa.text("DELETE FROM audit_log WHERE stream = 'runs/a/audit.jsonl' AND seq > 2"))
    env = {"DATABASE_URL": url, "WORKER_ENV_FILE": str(tmp_path / "none.env")}
    out = io.StringIO()
    argv = ["db", "verify", "--anchor-dir", str(tmp_path / "anchors")]
    assert main(argv, out=out, environ=env) == 1  # live: rows are gone
    out = io.StringIO()
    assert main([*argv, "--restore-drill"], out=out, environ=env) == 0
    assert "ends before the latest anchor" in out.getvalue()
    with db.begin() as conn:
        conn.execute(sa.text("DROP TRIGGER audit_no_update"))
        _rewrite_consistently(conn, "feedback/sponsor-a/feedback.jsonl", 2)
    assert main([*argv, "--restore-drill"], out=io.StringIO(), environ=env) == 1


def test_a_plain_edit_is_found_by_the_verifier(tmp_path: Path) -> None:
    db = Db("sqlite://")
    db.create_all()
    _fill(db)
    with db.begin() as conn:
        conn.execute(sa.text("DROP TRIGGER audit_no_update"))
        conn.execute(sa.text("UPDATE audit_log SET body = replace(body, 'entry 1', 'edited') "
                             "WHERE stream = 'runs/a/audit.jsonl' AND seq = 2"))
    report = V.verify_streams(db)
    assert not report.ok
    broken = report.summary()["broken"]
    assert [(b["stream"], b["seq"]) for b in broken] == [("runs/a/audit.jsonl", 2)]
    assert "altered" in broken[0]["reason"]


def test_incremental_verification_starts_at_the_last_head() -> None:
    db = Db("sqlite://")
    db.create_all()
    log = SqlAuditLog(db, "s")
    for i in range(5):
        log.record("tick", f"e{i}")
    first = V.verify_stream(db, "s")
    log.record("tick", "more")
    again = V.verify_stream(db, "s", after_seq=first.head_seq, after_hash=first.head_hash)
    assert (first.checked, again.checked, again.head_seq) == (5, 1, 6)


def test_the_anchors_own_chain_catches_a_deleted_or_replaced_anchor(tmp_path: Path) -> None:
    db = Db("sqlite://")
    db.create_all()
    _fill(db)
    sink = A.DirectorySink(tmp_path / "anchors")
    for _ in range(3):
        A.write(db, sink)
    assert A.check(db, sink) == []
    (tmp_path / "anchors" / sink.names()[1]).unlink()
    assert "anchor_chain_broken" in {p["code"] for p in A.check(db, sink)}


def test_the_sink_never_overwrites_and_a_stale_anchor_is_reported(tmp_path: Path) -> None:
    db = Db("sqlite://")
    db.create_all()
    _fill(db)
    sink = A.DirectorySink(tmp_path / "anchors")
    long_ago = datetime.now(UTC) - timedelta(hours=2)
    doc = A.write(db, sink, now=long_ago)
    with pytest.raises(FileExistsError):
        sink.put(f"anchor-{doc['number']:010d}.json", b"{}")
    assert [p["code"] for p in A.check(db, sink, max_age_seconds=1800)] == ["anchor_stale"]
    # no anchor yet: stale only once there is something old enough to have been anchored
    fresh = Db("sqlite://")
    fresh.create_all()
    none = A.DirectorySink(tmp_path / "none")
    assert A.check(fresh, none, max_age_seconds=1800) == []
    SqlAuditLog(fresh, "s").record("a", "r")
    assert A.check(fresh, none, max_age_seconds=1800) == []
    later = datetime.now(UTC) + timedelta(hours=2)
    assert [p["code"] for p in A.check(fresh, none, max_age_seconds=1800, now=later)] == ["anchor_stale"]


def test_the_follow_loops_run_a_round_and_stop(tmp_path: Path) -> None:
    db = Db("sqlite://")
    db.create_all()
    _fill(db)
    sink = A.DirectorySink(tmp_path / "anchors")
    stop = threading.Event()
    reports: list[V.Report] = []

    def got(report: V.Report) -> None:
        reports.append(report)
        stop.set()

    A.write(db, sink)
    V.follow(db, interval=0.01, full_every=2, stop=stop, check_anchors=lambda: A.check(db, sink), on_report=got)
    assert len(reports) == 1 and reports[0].ok
    kinds = [e["inputs"]["kind"] for e in SqlAuditLog(db, V.VERIFICATION_STREAM).read()]
    assert kinds == ["full"]
    stop2 = threading.Event()
    stop2.set()
    A.follow(db, sink, interval=0.01, stop=stop2)  # a set event: no round, returns


def test_the_cli_migrates_verifies_and_anchors(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'ow.db').as_posix()}"
    env = {"DATABASE_URL": url, "WORKER_ENV_FILE": str(tmp_path / "none.env")}

    def run(*argv: str) -> tuple[int, str]:
        out = io.StringIO()
        return main(list(argv), out=out, environ=env), out.getvalue()

    code, text = run("db", "migrate")
    assert code == 0 and "revision 0003" in text
    SqlAuditLog(Db(url), "s").record("a", "r")
    code, text = run("db", "verify")
    assert code == 0 and "OK (1 entries in 1 streams)" in text
    code, text = run("db", "anchor", "--anchor-dir", str(tmp_path / "anchors"))
    assert code == 0 and "Anchor 1 written" in text
    code, text = run("db", "verify", "--anchor-dir", str(tmp_path / "anchors"))
    assert code == 0
    code, text = run("db", "anchor")
    assert code == 2 and "needs a sink" in text


@pytest.mark.integration
def test_on_postgres_a_superuser_rewrite_is_caught_by_the_anchor(pg: PgSession, tmp_path: Path) -> None:
    _fill(pg.app)
    sink = A.DirectorySink(tmp_path / "anchors")
    A.write(pg.app, sink)
    with pg.owner.begin() as conn:
        conn.execute(sa.text("SET LOCAL session_replication_role = replica"))  # what only a superuser can do
        _rewrite_consistently(conn, "runs/a/audit.jsonl", 2)
    assert V.verify_streams(pg.app, only=["runs/a/audit.jsonl"]).ok
    assert {p["code"] for p in A.check(pg.app, sink)} == {"history_rewritten"}
