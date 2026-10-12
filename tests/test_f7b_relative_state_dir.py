"""F7b: a relative --state-dir made the console job fail with 'no file named ... in the inbox'."""
from __future__ import annotations

import pytest

from worker.engine import SourceRef
from worker.guard import PathRefused
from worker.ports import FileReader


def test_relative_ref_under_relative_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state" / "console_inbox").mkdir(parents=True)
    f = tmp_path / "state" / "console_inbox" / "a.xlsx"
    f.write_bytes(b"x")
    reader = FileReader(["state/console_inbox"])
    got = reader.path(SourceRef("file", "state/console_inbox/a.xlsx"))
    assert got == f.resolve()
    assert reader.path(SourceRef("file", "a.xlsx")) == f.resolve()


def test_relative_ref_outside_root_still_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state" / "console_inbox").mkdir(parents=True)
    (tmp_path / "other.xlsx").write_bytes(b"x")
    with pytest.raises(PathRefused):
        FileReader(["state/console_inbox"]).path(SourceRef("file", "other.xlsx"))
