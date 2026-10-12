"""Performance budgets at one million rows (marker `perf`; not part of the default run).

    uv run pytest tests/perf -m perf -q -s --tb=short

The fixtures are generated into a temporary folder and never committed. Every test prints the number it measured
(`-s`), so a run is also a report. A budget is the figure a person feels: warm queries under 100 ms, a rule
preview under 100 ms, a lazy apply under 100 ms, a whole apply and check in seconds, ingest in seconds (CSV) or
once in tens of seconds (xlsx, bound by the format).

Measured on a shared, fully loaded 8-core Windows machine (min / median of 3, the old code in the same session):

    xlsx ingest, 1M x 12:       old 92 / 105 s     now 24 / 35 s   (target 25 s: met once, not on the median)
    exact debit total:          old 3.9 / 4.3 s    now 0.26 / 0.32 s
    duplicates (journal, line): old 4.2 / 5.0 s    now 2.7 / 2.9 s (target 0.5 s: missed)
    3-field recipe, 500k distinct: old 25.3 / 25.5 s  now 21.1 / 21.4 s (target 3 s: missed)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from worker import columnar
from worker import setrecipe as SR
from worker import transforms as T
from worker.accuracy import conservation, control_totals
from worker.setops import RowTable, database

pytestmark = pytest.mark.perf

ROWS = 1_000_000
HEADERS = ["Journal", "Line", "Account", "Deal", "Debit", "Credit", "Date", "Description", "Investor", "Memo",
           "Type", "Fund"]
BUDGET_MS = 100  # the targets a person feels; each test prints MET or MISSED against them
BUDGET_APPLY_S = 3.0
BUDGET_CSV_S = 15.0
BUDGET_XLSX_S = 25.0
BUDGET_START_S = 30.0  # Start plus the first state of a 100,020-row Investors upload
GUARD = 4  # the suite fails only when a figure is this many times worse than its target: a regression guard


def _ms(fn: Callable[[], Any], repeat: int = 3) -> tuple[float, Any]:
    """The best of `repeat` runs in milliseconds, and the last result."""
    best = float("inf")
    out = None
    for _ in range(repeat):
        t = time.perf_counter()
        out = fn()
        best = min(best, (time.perf_counter() - t) * 1000)
    return best, out


def _report(name: str, value: float, unit: str, budget: float | None = None) -> None:
    verdict = "" if budget is None else f" (target {budget:g}: {'MET' if value <= budget else 'MISSED'})"
    print(f"PERF {name}: {value:,.1f} {unit}{verdict}")


@pytest.fixture(scope="session")
def csv_1m(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("perf") / "gl_1m.csv"
    con = database().cursor()
    con.execute(
        "COPY (SELECT 'J-' || (i // 2) AS Journal, i AS Line, "
        "CAST(1000 + (i % 40) * 10 AS VARCHAR) AS Account, "
        "CASE WHEN i % 7 = 0 THEN '' ELSE 'Deal ' || (i % 500) END AS Deal, "
        "CASE WHEN i % 2 = 0 THEN printf('%.2f', (i % 100000) / 100.0) ELSE '' END AS Debit, "
        "CASE WHEN i % 2 = 1 THEN printf('%.2f', (i % 100000) / 100.0) ELSE '' END AS Credit, "
        "strftime(DATE '2024-01-01' + CAST(i % 366 AS INTEGER), '%Y-%m-%d') AS Date, "
        "'line ' || i AS Description, 'INV-' || (i % 5000) AS Investor, '' AS Memo, "
        "CASE WHEN i % 3 = 0 THEN 'Capital call' ELSE 'Expense' END AS Type, 'Fund A' AS Fund "
        f"FROM range(1, {ROWS + 1}) t(i)) TO '{path.as_posix()}' (FORMAT CSV, HEADER)")
    return path


@pytest.fixture(scope="session")
def xlsx_1m(tmp_path_factory: pytest.TempPathFactory, csv_1m: Path) -> Path:
    """The same million lines as a workbook (written once, row by row, by the write-only writer)."""
    import openpyxl

    path = tmp_path_factory.mktemp("perf_x") / "gl_1m.xlsx"
    book = openpyxl.Workbook(write_only=True)
    ws = book.create_sheet("Data")
    ws.append(HEADERS)
    tbl = pq.read_table(columnar.parquet_parts(csv_1m)[0])
    for part in columnar.parquet_parts(csv_1m)[1:]:
        tbl = pa.concat_tables([tbl, pq.read_table(part)])
    cols = [c.to_pylist() for c in tbl.columns]
    for row in list(zip(*cols))[1:]:
        r = list(row)
        r[1] = int(r[1])
        for k in (4, 5):
            r[k] = float(r[k]) if r[k] != "" else None
        ws.append(r)
    book.save(path)
    return path


def _from_arrow(table: pa.Table, fields: list[str] | None = None) -> RowTable:
    """Rows that are already columns: the named columns as text, `_src` made blank and `_i` the position."""
    import tempfile
    names = list(fields) if fields else [c for c in table.column_names if c not in ("_src", "_i")]
    n = table.num_rows
    cols: dict[str, Any] = {f"c{k}": table[name].cast(pa.string()) for k, name in enumerate(names)}
    cols["_src"] = table["_src"] if "_src" in table.column_names else pa.array([""] * n, type=pa.string())
    cols["_i"] = pa.array(range(n), type=pa.int64())
    folder = Path(tempfile.mkdtemp(suffix=".rows"))
    pq.write_table(pa.table(cols), folder / "part-0.parquet", compression="zstd", compression_level=1)
    return RowTable([folder / "part-0.parquet"], names, folder)


@pytest.fixture(scope="session")
def job(csv_1m: Path) -> RowTable:
    """The loaded job table: the Parquet cache read once into Arrow (what a job holds after ingest)."""
    parts = columnar.parquet_parts(csv_1m)
    tbl = pa.concat_tables([pq.read_table(p) for p in parts])
    n = tbl.num_rows - 1  # the header row
    fields = [str(v) for v in tbl.slice(0, 1).to_pylist()[0].values()]
    body = tbl.slice(1)
    cols: dict[str, Any] = {f: body.column(k) for k, f in enumerate(fields)}
    cols["_src"] = pa.array([f"Data!{i + 2}" for i in range(n)], type=pa.string())
    return _from_arrow(pa.table(cols), fields)


# ---- ingest ------------------------------------------------------------------------------------------------------
def test_csv_ingest_seconds(csv_1m: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_CACHE_DIR", str(tmp_path / "cache"))
    t = time.perf_counter()
    parts = columnar.parquet_parts(csv_1m)
    cold = time.perf_counter() - t
    _report("csv ingest 1M rows (cold, to Parquet)", cold, "s", BUDGET_CSV_S)
    warm, n = _ms(lambda: sum(len(pq.read_table(p, columns=["c0"])) for p in parts))
    _report("csv cache read of one column, 1M rows", warm, "ms")
    assert n == ROWS + 1
    assert cold < BUDGET_CSV_S * GUARD


def test_xlsx_ingest_once(xlsx_1m: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_CACHE_DIR", str(tmp_path / "cache"))
    t = time.perf_counter()
    parts = columnar.parquet_parts(xlsx_1m, "Data")
    cold = time.perf_counter() - t
    _report("xlsx ingest 1M rows x 12 columns (cold, to Parquet)", cold, "s", BUDGET_XLSX_S)
    t = time.perf_counter()
    again = columnar.parquet_parts(xlsx_1m, "Data")
    _report("xlsx second open (cache hit)", (time.perf_counter() - t) * 1000, "ms")
    assert again == parts
    assert sum(pq.ParquetFile(p).metadata.num_rows for p in parts) == ROWS + 1
    assert cold < BUDGET_XLSX_S * GUARD


# ---- queries over the loaded job (warm) ------------------------------------------------------------------------
def test_filter_and_aggregate_warm(job: RowTable) -> None:
    assert len(job) == ROWS
    blank_deal = job.col("Deal")
    ms, got = _ms(lambda: job.rows_where(f"{blank_deal} = ''", (), 50))
    _report("filter: blank deal rows (count + first 50), 1M rows", ms, "ms", BUDGET_MS)
    assert got[0] > 0 and ms < BUDGET_MS * GUARD * 10
    ms, tot = _ms(lambda: job.exact_sum("Debit"))
    _report("aggregate: exact debit total, 1M rows", ms, "ms", BUDGET_MS)
    assert tot[0] > 0 and ms < BUDGET_MS * GUARD * 10
    ms, every = _ms(lambda: job.exact_sum("Line"))  # the worst case: a million distinct texts, parsed once each
    _report("aggregate: exact total of 1M distinct values", ms, "ms", BUDGET_MS * 10)
    assert every[0] == sum(range(1, ROWS + 1)) and ms < BUDGET_MS * GUARD * 40
    ms, ids = _ms(lambda: job.duplicate_ids(["Journal", "Line"]))
    _report("duplicate detection on (journal, line), 1M rows", ms, "ms", BUDGET_MS * 5)
    assert ids[0] == 0


# ---- rules -------------------------------------------------------------------------------------------------------
def _columns(job: RowTable, *names: str) -> tuple[list[str], list[Any]]:
    return list(names), [job.table[job.col(n)] for n in names]


def test_rule_preview_and_lazy_apply(job: RowTable) -> None:
    """A date rule (365 distinct values) and an amount rule (a million distinct values), over Arrow columns."""
    date_field = T.compile_field("Event_Date", {"from": "Date", "ops": [
        {"op": "date_parse", "patterns": ["YYYY-MM-DD"]}]}, T.FieldRule(kind="date"))
    headers, cols = _columns(job, "Date")
    t = time.perf_counter()
    full = SR.apply_field(date_field, headers, cols)
    cold = (time.perf_counter() - t) * 1000
    _report("date rule, first apply (compile + 1M rows)", cold, "ms")
    assert len(full) == ROWS
    ms, head = _ms(lambda: SR.apply_field(date_field, headers, [c.slice(0, 200) for c in cols]))
    _report("date rule preview, first 200 rows", ms, "ms", BUDGET_MS)
    assert len(head) == 200 and ms < BUDGET_MS * GUARD
    amount = T.compile_field("Total_Amount", {"from": "Debit", "ops": [
        {"op": "number_parse", "decimal": ".", "thousands": [","], "negative": ["minus"], "currency": [],
         "percent": "reject"}]}, T.FieldRule(kind="amount"))
    headers, cols = _columns(job, "Debit")
    t = time.perf_counter()
    big = SR.apply_field(amount, headers, cols)
    secs = time.perf_counter() - t
    _report("amount rule (number_parse), full apply of 1M rows", secs, "s", BUDGET_APPLY_S)
    ms, head = _ms(lambda: SR.apply_field(amount, headers, [c.slice(0, 200) for c in cols]))
    _report("amount rule preview, first 200 rows", ms, "ms", BUDGET_MS)
    assert len(big) == ROWS and ms < BUDGET_MS * GUARD * 4
    con = database().cursor()
    con.register("applied", big.table)
    ms, counts = _ms(lambda: con.execute(
        "SELECT status, count(*) FROM applied GROUP BY status ORDER BY status").fetchall())
    _report("counts over the applied column (the lazy view's summary), 1M rows", ms, "ms", BUDGET_MS)
    assert dict(counts)["ok"] > 0 and ms < BUDGET_MS * GUARD


def test_apply_and_accuracy_checks(job: RowTable) -> None:
    """Whole apply of a three-field recipe, then the set-based accuracy checks over the result."""
    recipe = T.compile_recipe({"fields": {
        "Event_ID": {"from": "Journal", "ops": [{"op": "trim"}, {"op": "case", "to": "upper"}]},
        "Account_Name": {"from": "Account", "ops": [{"op": "trim"}]},
        "Total_Amount": {"from": "Debit", "ops": [{"op": "number_parse", "decimal": ".", "thousands": [","],
                                                    "negative": ["minus"], "currency": [], "percent": "reject"}]},
    }}, {"Total_Amount": T.FieldRule(kind="amount")})
    headers, cols = _columns(job, "Journal", "Account", "Debit")
    t = time.perf_counter()
    done = SR.apply_recipe(compiled=recipe, headers=headers, columns=cols)
    apply_s = time.perf_counter() - t
    _report("full apply of a 3-field recipe over 1M rows", apply_s, "s", BUDGET_APPLY_S)
    t = time.perf_counter()
    ok = sum(1 for _n, r in done.items() if len(r) == ROWS)
    staged = _from_arrow(pa.table({"Total_Amount": done["Total_Amount"].table["payload"]}))
    net, _n = staged.exact_sum("Total_Amount")
    totals = {"rows": len(staged), "net": net}
    verdict = control_totals({"rows": ROWS, "net": net}, totals)
    cons = conservation(ROWS, ROWS, {}, 0)
    check_s = time.perf_counter() - t
    _report("accuracy checks (conservation + exact control totals) over 1M rows", check_s, "s", BUDGET_APPLY_S)
    assert ok == 3 and verdict.ok and cons.ok
    assert totals["net"] > Decimal(0)
    assert apply_s + check_s < BUDGET_APPLY_S * GUARD * 5


# ---- confirmed mappings ------------------------------------------------------------------------------------------
def test_confirmed_mappings_apply_as_a_join(csv_1m: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """1M rows x a 5,000-entry mapping: apply, the unmapped distinct report, and a re-apply after one more mapping
    (which must not read the source again)."""
    from worker.setops import ColumnMapper as Mapper

    monkeypatch.setenv("WORKER_CACHE_DIR", str(tmp_path / "cache"))
    parts = columnar.parquet_parts(csv_1m)
    stamp = [p.stat().st_mtime_ns for p in parts]
    con = database().cursor()  # a column with 6,000 distinct source values, of which 5,000 are mapped
    column = con.execute("SELECT 'SRC-' || (i % 6000) AS v FROM range(1, 1000001) t(i)").to_arrow_table().column(0)
    mapping = {f"SRC-{i}": f"ACC-{i}" for i in range(5000)}
    t = time.perf_counter()
    mapper = Mapper(column)
    _report("mapper build (dictionary encode 1M rows once)", (time.perf_counter() - t) * 1000, "ms")
    mapper.distinct(mapping)  # warm
    ms, got = _ms(lambda: mapper.distinct(mapping))
    _report("join 5,000 confirmed mappings to 1M rows' distinct values (warm)", ms, "ms", 1000)
    un = [v for v, _n, r in got if r is None]
    assert sum(n for _v, n, _r in got) == ROWS and ms < 1000 and len(un) == 1000
    mapping2 = {**mapping, un[0]: "ACC-NEW"}
    ms, got2 = _ms(lambda: mapper.distinct(mapping2))
    _report("re-join after the person adds one mapping (no re-parse)", ms, "ms", 1000)
    assert ms < 1000 and [p.stat().st_mtime_ns for p in parts] == stamp
    assert sum(r is None for _v, _n, r in got2) == 999


# ---- Start on a person's own 100,020-row upload (docs/perf-100k-start.md) -----------------------------------------
def test_start_on_100k_rows(tmp_path: Path) -> None:
    """The defect: this did not finish in 20 minutes. The figure that is asserted is the process's CPU time, so a
    loaded machine does not decide it; the wall time is printed beside it."""
    import openpyxl

    from worker.console import Console
    from worker.console_screens import state_view

    n = 100_020
    book = openpyxl.Workbook(write_only=True)
    sheet = book.create_sheet("Investors")
    sheet.append(["Row Labels", "Investor", "Type", "Entity", "Country", "Contact Email", "Notes"])
    for i in range(n):
        key = f"INV-{i - 100_000:07d}" if i >= 100_000 else f"INV-{i:07d}"  # the last 20 repeat a key
        sheet.append([key, f"Holder {i} LP", ("Individual", "Trust", "Corporate")[i % 3], "Fund A",
                      "" if i % 9 == 0 else "US", f"{i}@example.test", f"note {i}"])
    path = tmp_path / "investors_100k.xlsx"
    book.save(path)
    c = Console(tmp_path / "inbox")
    c.act("prov|paid")
    c.act("start|Sponsor B|Investors")  # the sponsor with a saved workflow: Start runs the job at once
    c.ask_attach_bytes(path.name, path.read_bytes())
    c.act("ask_set|cov|all")
    wall, cpu = time.perf_counter(), time.process_time()
    c.act("ask_go")
    view = state_view(c)
    wall, cpu = time.perf_counter() - wall, time.process_time() - cpu
    _report("Start + first state, 100,020-row Investors upload (CPU)", cpu, "s", BUDGET_START_S)
    _report("Start + first state, 100,020-row Investors upload (wall)", wall, "s", BUDGET_START_S)
    job = view["job"]
    assert job is not None and job["plan"]["rows"] == n
    assert cpu < BUDGET_START_S * 2
