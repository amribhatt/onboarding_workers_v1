"""Start does not repeat per-row work (docs/perf-100k-start.md).

A 100,020-row Investors file did not finish Start in 20 minutes. The cost was not one bad call: the check asked the
world "is this field required?" for every field of every row (each question was turned into a JSON key and logged
on the replay tape), looked a cause's rule file up for every row of an error group, resolved the skills folder
on the file system for each, and read the sheet twice. These tests count the questions, not seconds: a count that
grows with the rows is the defect. The timed budget is in `tests/perf/test_perf.py` (`-m perf`).
"""

from __future__ import annotations

import datetime as dt
import io
import threading
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
import pytest

from worker import console_engine as CE
from worker import engine_console as EC
from worker import skills as K
from worker import spool
from worker.console import Console

HEAD = ["Row Labels", "Investor", "Type", "Entity", "Country", "Contact Email", "Notes"]


def book(n: int, *, long_ids: bool = False) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEAD)
    for i in range(n):
        ws.append([f"INV-{i:05d}" + ("X" * 40 if long_ids else ""), f"Holder {i} LP", "Trust", "Fund A",
                   "" if i % 9 == 0 else "US", f"{i}@example.test", f"note {i}"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def start(tmp_path: Path, n: int, **kw: Any) -> Console:
    c = Console(tmp_path / f"inbox{n}")
    c.act("prov|paid")
    c.act("start|Sponsor A|Investors")
    c.ask_attach_bytes("investors.xlsx", book(n, **kw))
    c.act("ask_set|cov|all")
    c.act("ask_go")
    j = c.job()
    assert j is not None
    if c.ex_pending(j):
        c.act("ex|no")
    c.act("go")
    return c


def test_the_world_is_asked_once_per_field_not_once_per_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asked: Counter[str] = Counter()
    real = CE.ConsoleWorld.required

    def counting(self: Any, record_type: str, field_name: str, default: bool) -> bool:
        asked[field_name] += 1
        return real(self, record_type, field_name, default)

    monkeypatch.setattr(CE.ConsoleWorld, "required", counting)
    start(tmp_path, 20)
    few = sum(asked.values())
    asked.clear()
    start(tmp_path, 400)
    many = sum(asked.values())
    assert few > 0
    assert many == few, f"{few} questions for 20 rows, {many} for 400: the check asks per row"


def test_a_causes_rule_file_is_read_once_per_cause_not_once_per_row(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    looked: Counter[str] = Counter()
    real = EC.cause

    def counting(code: str) -> dict[str, str]:
        looked[code] += 1
        return real(code)

    monkeypatch.setattr(EC, "cause", counting)
    c = start(tmp_path, 15, long_ids=True)
    few = sum(looked.values())
    j = c.job()
    assert j is not None and CE.tally(c, j)["err"] == 15  # every row is in one cause: the group is the case at issue
    looked.clear()
    c = start(tmp_path, 300, long_ids=True)
    j = c.job()
    assert j is not None and CE.tally(c, j)["err"] == 300
    assert sum(looked.values()) == few, f"{few} lookups for 15 error rows, {sum(looked.values())} for 300"


def test_the_sheet_is_read_once_for_the_attach_the_plan_and_the_engine(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[str] = []
    real = EC.read_sheet

    def counting(path: Any, sheet: str | None = None) -> Any:
        reads.append(str(path))
        return real(path, sheet)

    monkeypatch.setattr(EC, "read_sheet", counting)
    start(tmp_path, 50)
    assert len(reads) == 1, reads


def test_the_skills_folder_is_resolved_once() -> None:
    first = K.skills_dir()
    assert K.skills_dir() == first and K.bundled_skills_dir.cache_info().hits >= 1


def test_a_spool_line_decodes_the_same_with_or_without_the_typed_value_hook() -> None:
    plain = {"i": 1, "v": {"a": "x", "b": 2.5, "c": None}, "rv": {}, "rf": {}}
    typed = {"i": 2, "v": {"d": dt.date(2025, 1, 31), "m": Decimal("12.50"), "t": dt.datetime(2025, 1, 31, 8, 30)}}
    for rec in (plain, typed):
        assert spool.decode(spool.encode(rec)) == rec
    assert b'"__t"' not in spool.encode(plain) and b'"__t"' in spool.encode(typed)
    # a text that merely looks like the marker is not a typed value
    odd = {"v": {"note": 'has "__t" inside'}}
    assert spool.decode(spool.encode(odd)) == odd


def test_a_view_that_asks_while_the_evaluation_runs_waits_for_it(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """A page that fetches the state during Start must not begin a second evaluation of the same file."""
    c = start(tmp_path, 30)
    j = c.job()
    assert j is not None
    CE._held(c).clear()
    runs: list[int] = []
    real = CE.E.evaluate

    def slow(*a: Any, **k: Any) -> Any:
        runs.append(1)
        time.sleep(0.4)
        return real(*a, **k)

    monkeypatch.setattr(CE.E, "evaluate", slow)
    out: list[Any] = []
    threads = [threading.Thread(target=lambda: out.append(CE.evaluation(c, j))) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(runs) == 1 and len(out) == 3 and out[0] is out[1] is out[2]
