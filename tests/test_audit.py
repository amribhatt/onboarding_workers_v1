import builtins
import json

import pytest

from worker.audit import AuditError, AuditLog
from worker.hashing import sha256_file


def test_chain_verifies_after_many_records(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(25):
        log.record("act", f"reason {i}", {"i": i}, {"ok": True})
    assert log.verify() == (True, 25)
    assert [e["seq"] for e in log.read()] == list(range(1, 26))


def test_editing_any_byte_breaks_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(5):
        log.record("act", f"reason {i}", {"i": i})
    original = path.read_bytes()
    lines = original.split(b"\n")
    offset = 0
    for idx, line in enumerate(lines[:5]):
        pos = offset + line.index(b"reason")
        tampered = bytearray(original)
        tampered[pos] = ord("R")
        path.write_bytes(bytes(tampered))
        assert log.verify() == (False, idx)
        offset += len(line) + 1
    path.write_bytes(original)
    assert log.verify() == (True, 5)


def test_write_failure_raises_and_appends_nothing(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.record("first", "ok")
    before = path.read_bytes()
    real_open = builtins.open

    def boom(file, mode="r", *a, **kw):
        if "a" in mode:
            raise OSError("disk full")
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", boom)
    with pytest.raises(AuditError):
        log.record("second", "fails")
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert log.verify() == (True, 1)


def test_oversize_payload_stored_as_hash_and_size(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    entry = log.record("big", "why", {"blob": "x" * 30_000})
    assert set(entry["inputs"]) >= {"sha256", "bytes"}
    assert entry["inputs"]["bytes"] > 20_000
    assert "blob" not in json.dumps(log.read()[0])
    assert log.verify() == (True, 1)


def test_non_serialisable_payload_refused(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(AuditError):
        log.record("bad", "why", {"x": object()})
    assert log.read() == []


def test_sha256_file(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc")
    assert sha256_file(p) == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_smoke_import():
    import deepagents  # noqa: F401
    import langchain_openai  # noqa: F401
    import langgraph  # noqa: F401
