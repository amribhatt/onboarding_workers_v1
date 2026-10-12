"""Columnar ingest: a source file becomes Arrow batches, cached as Parquet by the content hash of the file.

CSV is read by pyarrow (every column as text, nothing inferred, so `007` stays `007`); xlsx by python-calamine.
The result is written once, as a folder of Parquet parts of at most `BATCH` rows
(`<cache>/<hh>/<hash of source and sheet>/part-N.parquet`, so a million rows never sit in memory at once), and
every later read, in this process or another, streams from those files. The cache lives under the worker state
(`WORKER_CACHE_DIR`, default `.worker_state/cache`) and is never committed.

A cell keeps its type: a column holding one kind of value in a part is stored natively; a column that mixes kinds
(a text header over numbers, a number among dates) is stored as tagged text and decoded on read. A sheet reads
back cell for cell as the readers it replaced gave it: `None` for an empty cell, `int` for a whole number,
`float`, `datetime`, `str`, `bool`.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from itertools import islice
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from python_calamine import CalamineWorkbook

FORMAT = "1"  # bumped when the stored layout changes, so an old cache is never read
BATCH = 65_536
_MAX_WHOLE = 1e15  # beyond this a float is not an integer cell (Excel keeps 15 digits)
_TAGGED = b"tagged"
_NONE = type(None)
_BARE_CR = re.compile(rb"\r(?!\n)")
_NULLSTR = chr(1) + "null" + chr(2)  # never in a file: an empty CSV cell is the empty text
INGEST_MEMORY = "256MB"  # what the bulk CSV reader may hold; the file streams through
INGEST_THREADS = 4  # also the most processes that parse one workbook sheet side by side
PARALLEL_BYTES = 24 << 20  # a sheet whose XML is larger is cut by rows into pieces of about this size
_NATIVE = {str: pa.string(), int: pa.int64(), float: pa.float64(), bool: pa.bool_(),
           dt.datetime: pa.timestamp("us"), dt.date: pa.date32(), dt.time: pa.time64("us")}


# ---- where the cache lives ------------------------------------------------------------------------------------
def cache_dir() -> Path:
    base = os.environ.get("WORKER_CACHE_DIR")
    path = Path(base) if base else Path(os.environ.get("WORKER_STATE_DIR", ".worker_state")) / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


_HASHES: dict[tuple[str, int, int], str] = {}


def source_hash(path: str | Path) -> str:
    """sha256 of the file content; remembered per (path, size, mtime) so a repeated read does not rehash."""
    p = Path(path)
    st = p.stat()
    key = (str(p.resolve()), st.st_size, st.st_mtime_ns)
    if key not in _HASHES:
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        _HASHES[key] = h.hexdigest()
    return _HASHES[key]


# ---- sheets ---------------------------------------------------------------------------------------------------
def is_csv(path: str | Path) -> bool:
    return Path(path).suffix.lower() in (".csv", ".txt")


def sheet_names(path: str | Path) -> list[str]:
    """The sheets of a workbook in order; a CSV has one, named by its file stem."""
    if is_csv(path):
        return [Path(path).stem]
    return list(CalamineWorkbook.from_path(str(path)).sheet_names)


def first_sheet(path: str | Path) -> str:
    return sheet_names(path)[0]


# ---- cell <-> column ------------------------------------------------------------------------------------------
def _whole(v: Any) -> Any:
    """A cell as openpyxl gave it: a whole-number float is an int, a calendar date is a midnight datetime."""
    if isinstance(v, float):
        return int(v) if v.is_integer() and abs(v) < _MAX_WHOLE else v
    if type(v) is dt.date:
        return dt.datetime(v.year, v.month, v.day)
    return v


def _tag(v: Any) -> str | None:
    if v is None:
        return None
    t = type(v)
    if t is str:
        return "s" + v
    if t is bool:
        return "b1" if v else "b0"
    if t is int:
        return "i" + str(v)
    if t is float:
        return "f" + repr(v)
    if t is dt.datetime:
        return "t" + v.isoformat()
    if t is dt.date:
        return "D" + v.isoformat()
    if t is dt.time:
        return "T" + v.isoformat()
    if t is dt.timedelta:
        return "x" + repr(v.total_seconds())
    return "s" + str(v)


def _untag(s: str | None) -> Any:
    if s is None:
        return None
    k, rest = s[0], s[1:]
    if k == "s":
        return rest
    if k == "i":
        return int(rest)
    if k == "f":
        return float(rest)
    if k == "b":
        return rest == "1"
    if k == "t":
        return dt.datetime.fromisoformat(rest)
    if k == "D":
        return dt.date.fromisoformat(rest)
    if k == "T":
        return dt.time.fromisoformat(rest)
    return dt.timedelta(seconds=float(rest))


def _column(values: tuple[Any, ...], blank_text: bool = False) -> tuple[pa.Array, bool]:
    """One column of cells as Arrow. `blank_text` reads an empty text as a blank cell (what an xlsx reader gives).
    A float column is cut over in one pass: whole numbers are int cells, as `_whole` makes them one by one."""
    kinds = set(map(type, values)) - {_NONE}
    if blank_text and str in kinds:
        if kinds == {str}:
            arr = pa.array(values, type=pa.string())
            return pc.if_else(pc.equal(arr, ""), None, arr), False
        values = tuple(None if v == "" else v for v in values)
        kinds = set(map(type, values)) - {_NONE}
    if kinds == {float}:
        arr = pa.array(values, type=pa.float64())
        whole = pc.and_(pc.equal(pc.floor(arr), arr), pc.less(pc.abs(arr), _MAX_WHOLE))
        if pc.all(whole).as_py():
            return arr.cast(pa.int64()), False
        if not pc.any(whole).as_py():
            return arr, False
    if kinds == {dt.date}:
        return pa.array(values, type=pa.date32()).cast(pa.timestamp("us")), False
    if kinds & {float, dt.date}:
        values = tuple(map(_whole, values))
        kinds = set(map(type, values)) - {_NONE}
    if not kinds:
        return pa.array(values, type=pa.string()), False
    if len(kinds) == 1:
        kind = next(iter(kinds))
        arrow = _NATIVE.get(kind)
        if arrow is not None:
            return pa.array(values, type=arrow), False
    return pa.array([_tag(v) for v in values], type=pa.string()), True


def _table_from_rows(rows: list[list[Any]], left: int = 0, blank_text: bool = False) -> pa.Table:
    """Rows as an Arrow table of columns `c0..`, `left` empty columns in front, short rows padded with blanks."""
    width = max((len(r) for r in rows), default=0)
    padded = [r if len(r) == width else r + [None] * (width - len(r)) for r in rows]
    cols = [(None,) * len(rows)] * left + (list(zip(*padded)) if padded else [])
    arrays: list[pa.Array] = []
    fields: list[pa.Field] = []
    for k, col in enumerate(cols):
        arr, tagged = _column(col, blank_text)
        arrays.append(arr)
        fields.append(pa.field(f"c{k}", arr.type, metadata={_TAGGED: b"1"} if tagged else None))
    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))


# ---- readers ----------------------------------------------------------------------------------------------------
def _write_part(directory: Path, name: str, table: pa.Table) -> None:
    pq.write_table(table, directory / f"{name}.parquet", compression="zstd", compression_level=1)


def _csv_width(path: Path) -> int:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return len(next(csv.reader(fh), []))


def _plain_lines(path: Path) -> bool:
    """True when every record ends in `\\n` or `\\r\\n` and there is no blank line: the file shapes on which the
    bulk reader and `csv.reader` number rows the same way."""
    tail = b""
    with open(path, "rb") as fh:
        first = True
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            data = tail + chunk
            if first and data[:1] in (b"\n", b"\r"):
                return False
            first = False
            if b"\n\n" in data or b"\r\n\r\n" in data or b"\r\r" in data or _BARE_CR.search(data):
                return False
            tail = data[-3:]
    return True


def _csv_parts(path: Path, directory: Path) -> None:
    """DuckDB streams the file into Parquet with every cell as text and bounded memory. A file it cannot take as a
    plain rectangle (ragged rows, blank lines, bare carriage returns, an odd encoding) goes through the standard
    csv reader, which defines the row numbers."""
    width = _csv_width(path)
    if width and _plain_lines(path):
        names = ", ".join(f"'c{k}': 'VARCHAR'" for k in range(width))
        target = (directory / "part-0.parquet").as_posix()
        con = duckdb.connect(config={"memory_limit": INGEST_MEMORY, "threads": str(INGEST_THREADS)})
        try:
            con.execute(
                f"COPY (SELECT * FROM read_csv(?, header=false, delim=',', quote='\"', escape='\"', "
                f"columns={{{names}}}, auto_detect=false, strict_mode=true, nullstr='{_NULLSTR}', "
                f"encoding='utf-8')) TO '{target}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {BATCH})",
                [path.as_posix()])
            return
        except duckdb.Error:
            for old in directory.glob("part-*.parquet"):
                old.unlink()
        finally:
            con.close()
    _csv_parts_via_reader(path, directory)


def _csv_parts_via_reader(path: Path, directory: Path) -> None:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        width = max((len(r) for r in csv.reader(fh)), default=0)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for n, batch in enumerate(_chunks(csv.reader(fh), BATCH)):
            rows = [r + [None] * (width - len(r)) for r in batch]
            _write_part(directory, f"part-{n}", _table_from_rows(rows))


def _chunks(it: Any, size: int) -> Iterator[list[Any]]:
    batch: list[Any] = []
    for item in it:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _sheet_part(zf: zipfile.ZipFile, name: str) -> str:
    """The archive path of the XML that holds sheet `name`."""
    rid = next(s.get(_REL_ID) for s in ET.fromstring(zf.read("xl/workbook.xml")).findall(".//{*}sheet")
               if s.get("name") == name)
    target = next(r.get("Target") for r in ET.fromstring(zf.read("xl/_rels/workbook.xml.rels")) if r.get("Id") == rid)
    return target.lstrip("/") if target.startswith("/") else "xl/" + target


def _row_cuts(path: Path, name: str) -> tuple[str, list[int]] | None:
    """(the sheet's XML path, byte offsets in it that cut its rows into `INGEST_THREADS` or fewer pieces) for a
    big .xlsx sheet; None to read it whole. Nothing is read here: the XML is cut where its rows are found."""
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        return None
    try:
        with zipfile.ZipFile(path) as zf:
            part = _sheet_part(zf, name)
            size = zf.getinfo(part).file_size
    except (KeyError, StopIteration, zipfile.BadZipFile, ET.ParseError):
        return None
    pieces = min(INGEST_THREADS, size // PARALLEL_BYTES)
    return (part, [size * k // pieces for k in range(pieces)]) if pieces >= 2 else None


def _read_rows(fh: Any, lo: int, hi: int | None) -> tuple[bytes, bytes]:
    """(the XML before the first row, the rows that begin at or after byte `lo` and before byte `hi` of the sheet's
    XML) read off a stream, holding only what it keeps. Neighbouring pieces cut at the same `<row `, so they join.
    A sheet that is not safe to cut (a row without its number `r`, a CDATA section) raises `ValueError`."""
    buf, base, head, begin = bytearray(), 0, None, None
    while True:
        chunk = fh.read(1 << 22)
        buf += chunk
        if head is None:
            at = buf.find(b"<row ")
            if at < 0:
                if chunk:
                    continue
                return b"", b""
            head = bytes(buf[:at])
        if begin is None:
            at = buf.find(b"<row ", max(max(lo, len(head)) - base, 0))
            if at >= 0:
                begin = base + at
                del buf[:at]
                base = begin
            else:  # not yet: keep only what a `<row ` straddling the next chunk needs
                drop = max(len(buf) - 4, 0)
                del buf[:drop]
                base += drop
        if begin is not None:
            if hi is not None and begin >= hi:  # no row begins in this piece: the next piece has it
                return head, b""
            at = buf.find(b"<row ", hi - base) if hi is not None else -1
            if at < 0 and not chunk:
                at = buf.rfind(b"</sheetData>")
                at = len(buf) if at < 0 else at
            if at >= 0:
                body = bytes(buf[:at])
                if b"<![CDATA[" in body or body.count(b"<row ") != body.count(b'<row r="'):
                    raise ValueError("a sheet whose rows cannot be cut apart")
                return head, body
        if not chunk:
            return head, b""


def _xlsx_piece(path: str, name: str, cut: tuple[str, int, int | None] | None, directory: str, tag: int,
                batch: int) -> tuple[int, int, int]:
    """Parse the whole sheet, or only the rows between two cuts of its XML, into Parquet parts `x<tag>-<n>`.
    Returns (index of the first row, rows, columns). A piece is a workbook of its own with the same shared
    strings and styles and only its rows, so pieces parse side by side in separate processes."""
    if cut is None:
        book = CalamineWorkbook.from_path(path)
    else:
        part, lo, hi = cut
        buf = io.BytesIO()
        with zipfile.ZipFile(path) as zf, zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as out:
            with zf.open(part) as fh:
                head, body = _read_rows(fh, lo, hi)
            for info in zf.infolist():
                out.writestr(info.filename,
                             head + body + b"</sheetData></worksheet>" if info.filename == part else zf.read(info))
        del body
        buf.seek(0)
        book = CalamineWorkbook.from_object(buf)
    ws = book.get_sheet_by_name(name)
    left = ws.start[1] if ws.start is not None else 0
    first = ws.start[0] if ws.start is not None and tag else 0  # rows above a piece's first row are empty
    rows = columns = 0
    for n, rows_batch in enumerate(_chunks(islice(ws.iter_rows(), first, None), batch)):
        table = _table_from_rows(rows_batch, left, blank_text=True)
        _write_part(Path(directory), f"x{tag}-{n}", table)
        rows += len(rows_batch)
        columns = table.num_columns
    return first, rows, columns


def _xlsx_parts(path: Path, sheet: str | None, directory: Path) -> None:
    book = CalamineWorkbook.from_path(str(path))
    name = sheet if sheet else book.sheet_names[0]
    if name not in book.sheet_names:
        raise KeyError(f"Worksheet {name} does not exist.")
    cuts = _row_cuts(path, name)
    pieces: list[tuple[int, int, int]] | None = None
    if cuts is not None:
        part, at = cuts
        jobs = [(str(path), name, (part, lo, hi), str(directory), k, BATCH)
                for k, (lo, hi) in enumerate(zip(at, [*at[1:], None], strict=True))]
        try:
            with ProcessPoolExecutor(max_workers=len(jobs), max_tasks_per_child=1) as pool:
                pieces = list(pool.map(_xlsx_piece, *zip(*jobs, strict=True)))
        except (BrokenProcessPool, OSError, ValueError):  # no process to spare, or a sheet to read whole
            pieces = None
    if pieces is None:
        for old in directory.glob("x*.parquet"):
            old.unlink()
        pieces = [_xlsx_piece(str(path), name, None, str(directory), 0, BATCH)]
    width = max(c for _f, _r, c in pieces)
    n = 0
    for tag, (first, rows, columns) in enumerate(pieces):
        if not rows:
            continue
        files = sorted(directory.glob(f"x{tag}-*.parquet"), key=lambda p: int(p.stem.split("-")[1]))
        gap = first - sum(r for _f, r, _c in pieces[:tag])  # empty rows that no piece holds
        for start in range(0, gap, BATCH):
            _write_part(directory, f"part-{n}", _table_from_rows([[None] * width] * min(BATCH, gap - start)))
            n += 1
        for f in files:
            if columns < width:  # the pieces are the same sheet: pad the narrower ones to its width
                t = pq.read_table(f)
                for k in range(columns, width):
                    t = t.append_column(f"c{k}", pa.nulls(t.num_rows, pa.string()))
                pq.write_table(t, f, compression="zstd", compression_level=1)
            os.replace(f, directory / f"part-{n}.parquet")
            n += 1
    if n == 0:
        _write_part(directory, "part-0", _table_from_rows([]))


def _parts_for(path: Path, sheet: str | None) -> list[Path]:
    csv_like = is_csv(path)
    key = hashlib.sha256(f"{FORMAT}|{source_hash(path)}|{'' if csv_like else sheet or ''}".encode()).hexdigest()
    target = cache_dir() / key[:2] / key
    if not target.is_dir():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=target.parent, suffix=".tmp"))
        try:
            if csv_like:
                _csv_parts(path, tmp)
            else:
                _xlsx_parts(path, sheet, tmp)
            try:
                os.replace(tmp, target)
            except OSError:  # another process published the same source first; its copy is identical
                if not target.is_dir():
                    raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return sorted(target.glob("part-*.parquet"), key=lambda p: int(p.stem.split("-")[1]))


def parquet_parts(path: str | Path, sheet: str | None = None) -> list[Path]:
    """The cached Parquet files of a sheet, in row order (built on first use). Columns are `c0..cN`."""
    return _parts_for(Path(path), sheet)


def rows(path: str | Path, sheet: str | None = None) -> Iterator[tuple[int, list[Any]]]:
    """(row number from 1, cell values) for every row of the sheet, blank rows included, in batches off the cache."""
    csv_like = is_csv(path)
    n = 0
    for part in parquet_parts(path, sheet):
        pf = pq.ParquetFile(part)
        tagged = [f.metadata is not None and f.metadata.get(_TAGGED) == b"1" for f in pf.schema_arrow]
        for batch in pf.iter_batches(batch_size=BATCH):
            cols = [c.to_pylist() for c in batch.columns]
            for k, is_tagged in enumerate(tagged):
                if is_tagged:
                    cols[k] = [_untag(v) for v in cols[k]]
            for row in zip(*cols) if cols else ():
                n += 1
                cells = list(row)
                if csv_like:  # a CSV cell is never None: a null is the padding of a short row
                    while cells and cells[-1] is None:
                        cells.pop()
                yield n, cells


def column_fill(path: str | Path, sheet: str | None = None, header_row: int = 1) -> tuple[list[str], list[int]]:
    """(the header texts, the number of non-blank cells under each) of a sheet, counted over the cached Parquet:
    a column is a count in a query, not a pass over the rows. Rows above and at the header row do not count."""
    header: list[str] = []
    filled: list[int] = []
    seen = 0  # rows passed so far, to find the header row in the first parts
    for part in parquet_parts(path, sheet):
        pf = pq.ParquetFile(part)
        tagged = [f.metadata is not None and f.metadata.get(_TAGGED) == b"1" for f in pf.schema_arrow]
        table = pf.read()
        width = table.num_columns
        if len(filled) < width:
            filled += [0] * (width - len(filled))
        if seen < header_row <= seen + table.num_rows:
            first = table.slice(header_row - seen - 1, 1).to_pylist()[0]
            header = [cell_text(_untag(v) if tagged[k] else v) for k, v in enumerate(first.values())]
        body = table.slice(min(max(0, header_row - seen), table.num_rows))
        for k in range(width):
            col = body.column(k)
            if tagged[k]:  # a tagged text is `s` + text for a string cell; every other tag is a value
                text = pc.utf8_trim_whitespace(pc.utf8_slice_codeunits(col, 1))
                blank = pc.and_(pc.starts_with(col, "s"), pc.equal(text, ""))
                filled[k] += int(pc.sum(pc.cast(pc.invert(pc.or_(pc.is_null(col), pc.fill_null(blank, False))),
                                                pa.int64())).as_py() or 0)
            else:
                as_text = pc.utf8_trim_whitespace(pc.cast(col, pa.string()))
                filled[k] += int(pc.sum(pc.cast(pc.and_(pc.is_valid(as_text), pc.not_equal(as_text, "")),
                                                pa.int64())).as_py() or 0)
        seen += table.num_rows
    return header, filled


def cell_text(v: Any) -> str:
    return "" if v is None else str(v).strip()
