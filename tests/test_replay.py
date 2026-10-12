"""Migration M2: the replay core (ticket 04, D9). Nothing is wired in; `run` is injected, no model is called."""

from __future__ import annotations

import hashlib
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
import pytest

from scripted_model import ScriptedModel, say
from worker import replay as R
from worker.audit import AuditLog


def _result(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "output": {"name": "Investors_Upload.csv", "format": "csv", "bytes_sha256": "a" * 64,
                   "content_sha256": "a" * 64},
        "findings": [{"id": "f1", "rule_id": "r1", "severity": "ERR", "count": 2},
                     {"id": "f2", "rule_id": "r2", "severity": "WARN", "count": 1}],
        "tie_out": {"rows": 3, "ties": True},
        "missing": [{"field": "Country", "reason": "no source column"}, {"field": "Email", "reason": "blank"}],
        "tally": {"err": 2, "warn": 1},
        "ids_issued": ["I-001", "I-002", "I-003"],
        "decisions": [{"action": "lift", "rationale": "copy", "inputs": {"n": 3}, "outputs": {"rows": 3}}],
    }
    return {**base, **over}


def _record(tmp_path: Path, **over: Any) -> R.ReplayRecord:
    kw: dict[str, Any] = {"job_id": "job-1", "sponsor": "sponsor-a", "record_type": "Investors",
                          "signed_off": {"by": "alex", "approval_id": "ap-1"}, "expected": _result()}
    return R.ReplayRecord(**{**kw, **over})


def _xlsx(path: Path, rows: list[list[Any]], title: str = "Sheet1") -> Path:
    book = openpyxl.Workbook(write_only=True)
    ws = book.create_sheet(title)
    for r in rows:
        ws.append(r)
    book.save(path)
    return path


# 1
def test_only_the_volatile_keys_are_ignored() -> None:
    a = {"action": "lift", "ts": "2026-01-01", "seq": 1, "hash": "x", "prev_hash": "y", "outputs": {"rows": 3, "seconds": 9}}
    b = {"action": "lift", "ts": "2027-02-02", "seq": 77, "hash": "z", "prev_hash": "w", "outputs": {"rows": 3, "seconds": 1}}
    assert R.canonical(a) == R.canonical(b)
    assert R.VOLATILE == {"ts", "seq", "hash", "prev_hash", "trace_id", "run_id", "thread_id", "seconds",
                          "tokens_in", "tokens_out", "created_at", "filed",
                          # M19: the identities of a run (tests/test_replay_jobs.py says why each one is there)
                          "eval_key", "spec_key", "stage_key", "stage_keys", "fingerprint", "approval_id",
                          "approved_at", "record_sha256"}
    # a key that is not on the list is data, at any depth
    assert R.canonical({"outputs": {"rows": 3}}) != R.canonical({"outputs": {"rows": 4}})
    assert R.canonical({"a": {"note": "x"}}) != R.canonical({"a": {"note": "y"}})
    assert R.canonical({"a": [{"rationale": "x"}]}) != R.canonical({"a": [{"rationale": "y"}]})
    assert R.canonical("Abc") != R.canonical("abc")  # strings are compared exactly


# 2
def test_xlsx_saved_two_seconds_apart_is_content_equal_and_byte_unequal(tmp_path: Path) -> None:
    rows: list[list[Any]] = [["Investor_ID", "Amount", "When"], ["I-1", Decimal("10.50"), None], ["I-2", 3, "x"]]
    a = _xlsx(tmp_path / "a.xlsx", rows)
    time.sleep(2.1)
    b = _xlsx(tmp_path / "b.xlsx", rows)
    assert a.read_bytes() != b.read_bytes()
    assert R.xlsx_content_digest(a) == R.xlsx_content_digest(b)
    assert R.output_facts(a)["bytes_sha256"] != R.output_facts(b)["bytes_sha256"]
    assert R.output_facts(a)["content_sha256"] == R.output_facts(b)["content_sha256"]
    # a changed cell, or a text "10.5" where a number was, changes the content digest
    c = _xlsx(tmp_path / "c.xlsx", [rows[0], ["I-1", "10.5", None], rows[2]])
    assert R.xlsx_content_digest(c) != R.xlsx_content_digest(a)
    # a CSV is its bytes
    csv = tmp_path / "o.csv"
    csv.write_bytes("﻿A,B\n1,2\n".encode())
    assert R.output_facts(csv)["content_sha256"] == hashlib.sha256(csv.read_bytes()).hexdigest()


# 3
def test_decimal_and_float_normalise_and_minus_zero_equals_zero() -> None:
    assert R.canonical({"v": Decimal("1.50")}) == R.canonical({"v": 1.5}) == R.canonical({"v": Decimal("1.5")})
    assert R.canonical({"v": Decimal(100)}) == R.canonical({"v": 100.0}) == R.canonical({"v": 100})
    assert R.canonical({"v": Decimal("-0")}) == R.canonical({"v": 0.0}) == R.canonical({"v": -0.0})
    assert R.canonical({"v": Decimal("0.10")}) != R.canonical({"v": Decimal("0.1001")})
    assert R.number_text(Decimal("-0.00")) == "0" and R.number_text(0.1 + 0.2) == "0.30000000000000004"


# 4
def test_findings_compare_as_a_set_and_the_order_of_ids_matters() -> None:
    exp = _result()
    shuffled = _result(findings=list(reversed(exp["findings"])),
                       missing=list(reversed(exp["missing"])))
    assert R.diff(exp, shuffled).identical
    assert R.result_digest(exp) == R.result_digest(shuffled)
    swapped = _result(ids_issued=["I-002", "I-001", "I-003"])
    d = R.diff(exp, swapped, "job-1")
    assert d.status == "different" and d.component == "ids_issued"
    assert "IDs issued" in d.first_difference
    # the first component that differs is the one named, in table order
    both = _result(ids_issued=["I-009"], tally={"err": 9, "warn": 1})
    assert R.diff(exp, both).component == "tally"
    changed = _result(findings=[{**exp["findings"][0], "count": 5}, exp["findings"][1]])
    d = R.diff(exp, changed)
    assert d.component == "findings" and "f1" in d.first_difference and "count" in d.first_difference


# 5
def test_source_gone_is_counted_not_skipped(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    src = inbox / "roster.csv"
    src.write_text("a,b\n1,2\n", encoding="utf-8")
    item = {"ref": "roster.csv", "sha256": hashlib.sha256(src.read_bytes()).hexdigest(), "bytes": 8}
    rec = _record(tmp_path, inputs=[item])
    calls: list[int] = []

    def run(r: R.ReplayRecord, work: Path) -> dict[str, Any]:
        calls.append(1)
        return r.expected

    assert R.replay_job(rec, run, inbox_root=inbox).identical and calls == [1]
    src.write_text("a,b\n1,3\n", encoding="utf-8")  # changed
    d = R.replay_job(rec, run, inbox_root=inbox)
    assert d.status == "cannot_replay" and "source_gone" in d.first_difference and "changed" in d.first_difference
    src.unlink()
    d = R.replay_job(rec, run, inbox_root=inbox)
    assert d.status == "cannot_replay" and "no longer there" in d.first_difference
    assert calls == [1]  # the run was never reached
    assert R.replay_job(rec, run, inbox_root=None).status == "cannot_replay"  # never silently skipped


# 6
def test_a_tape_miss_is_a_difference_and_the_model_is_not_used(tmp_path: Path) -> None:
    model = ScriptedModel(script=[say("must not be asked")])

    def run(r: R.ReplayRecord, work: Path) -> dict[str, Any]:
        raise R.TapeMiss("no recorded answer for unit u-7, role mapper")

    d = R.replay_job(_record(tmp_path), run)
    assert d.status == "different" and d.component == "tape" and d.first_difference.startswith("tape_miss")
    assert "u-7" in d.first_difference
    assert model.seen == [] and model._i == 0


# 7
def test_an_answer_key_path_is_refused_and_audited(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    ran: list[int] = []
    for ref in ("answer_key/GL.csv", "x/investors.answer.json", "../outside.csv"):
        rec = _record(tmp_path, inputs=[{"ref": ref, "sha256": "0" * 64, "bytes": 1}])
        d = R.replay_job(rec, lambda r, w: ran.append(1) or r.expected, inbox_root=tmp_path, audit=audit)
        assert d.status == "cannot_replay" and d.detail["refused"] is True
    assert ran == []
    entries = audit.read()
    assert [e["action"] for e in entries] == ["replay.refused"] * 3
    assert all(e["rationale"] for e in entries) and audit.verify()[0]


# 8
def test_the_holdout_is_stable_and_reaches_the_proposing_side_as_counts_only(tmp_path: Path) -> None:
    ids = [f"job-{i}" for i in range(400)]
    first = [R.assign_holdout("sponsor-a", j, 20) for j in ids]
    assert first == [R.assign_holdout("sponsor-a", j, 20) for j in ids]
    want = [int(hashlib.sha256(f"sponsor-a|{j}".encode()).hexdigest()[:8], 16) % 100 < 20 for j in ids]
    assert first == want and 40 < sum(first) < 120
    assert not any(R.assign_holdout("sponsor-a", j, 0) for j in ids)
    assert all(R.assign_holdout("sponsor-a", j, 100) for j in ids)
    # a different sponsor does not share the assignment
    assert first != [R.assign_holdout("sponsor-b", j, 20) for j in ids]


# 10
def test_the_result_digest_follows_any_component(tmp_path: Path) -> None:
    recs = [_record(tmp_path, job_id="job-1")]
    # the record's digest is over the component digests and follows any component
    other = _record(tmp_path, expected=_result(tally={"err": 0, "warn": 1}))
    assert other.result_sha256 != recs[0].result_sha256


# 11
def test_capability_mode_raises_not_built(tmp_path: Path) -> None:
    with pytest.raises(R.NotBuilt):
        R.replay_job(_record(tmp_path), lambda r, w: r.expected, mode="capability")
    with pytest.raises(ValueError):
        R.replay_job(_record(tmp_path), lambda r, w: r.expected, mode="other")


# 12
def test_replay_writes_nothing_outside_its_temp_dir_and_its_audit(tmp_path: Path) -> None:
    inbox, state = tmp_path / "inbox", tmp_path / "state"
    inbox.mkdir()
    src = inbox / "roster.csv"
    src.write_text("a\n1\n", encoding="utf-8")
    rec = _record(tmp_path, inputs=[{"ref": "roster.csv", "sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
                                     "bytes": 4}])
    saved = rec.save(state / "replay")
    store = R.BlobStore(state / "replay" / "blobs")
    blob = store.put(b"tape")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    seen: list[Path] = []

    def run(r: R.ReplayRecord, work: Path) -> dict[str, Any]:
        (work / "would-be-upload.csv").write_text("x", encoding="utf-8")
        seen.append(work)
        return r.expected

    audit = AuditLog(tmp_path / "audit.jsonl")
    d = R.replay_job(rec, run, inbox_root=inbox, audit=audit)
    assert d.identical
    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert set(after) - set(before) == {tmp_path / "audit.jsonl"}
    assert {p: b for p, b in after.items() if p in before} == before
    assert not seen[0].exists()  # the temp dir is gone
    assert [e["action"] for e in audit.read()] == ["replay.run"]
    assert R.ReplayRecord.load(saved) == rec and store.get(blob) == b"tape"


# the pieces the 12 lean on
def test_blob_store_is_content_addressed_and_deduplicated(tmp_path: Path) -> None:
    store = R.BlobStore(tmp_path / "blobs")
    a = store.put_plain({"b": 1, "a": [1]})
    assert a == store.put_plain({"a": [1], "b": 1}) and store.count() == 1
    assert store.get_json(a) == {"a": [1], "b": 1}
    with pytest.raises(R.ReplayError):
        store.get("0" * 64)
    with pytest.raises(R.ReplayError):
        store.get("../etc")
    p = tmp_path / "blobs" / "sha256" / a[:2] / a[2:]
    p.write_bytes(b"tampered")
    with pytest.raises(R.ReplayError):
        store.get(a)


def test_record_names_are_safe_and_the_checksum_follows_content(tmp_path: Path) -> None:
    with pytest.raises(R.ReplayError):
        _record(tmp_path, job_id="../x")
    rec = _record(tmp_path)
    c = rec.checksum()
    assert c == _record(tmp_path).checksum()
    rec.history.append({"engine": "2", "result_sha256": "b" * 64, "accepted_by": "alex", "reason": "bug fix"})
    assert rec.checksum() != c
    assert rec.path(tmp_path) == tmp_path / "sponsor-a" / "job-1.json"
    assert rec.family == "static" and _record(tmp_path, record_type="GL Fund").family == "gl"
