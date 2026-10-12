"""GL files in and out: read a GL source one line at a time, shape a line into an upload row, write the upload.

The engine's read and write stages and the agent's staging both read GL lines through `read_gl_lines`, so a million
lines never sit in memory, and both shape the written row through `GlRows`. Nothing here decides a row: it copies
the source's cells, and a field the source cannot provide stays blank.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from worker.engine_console import cell_text
from worker.glline import Line
from worker.money import exact_amount, net_to_float
from worker.templates import TemplateSpec

GL_COLUMNS = {"j": "Journal", "ln": "Line", "acct": "GL Account", "an": "Account Name", "tt": "Transaction Type",
              "dr": "Debit", "cr": "Credit", "desc": "Description", "inv": "Investor", "date": "GL Date"}


def money_cell(value: Any) -> float | Decimal:
    """A debit or credit cell as the number it holds. Text such as "1,250.00" or "(40.00)" goes through the money
    parser; nothing is rounded here (the Decimal sums and the cent rounding happen where the figures are used),
    and an amount a float cannot hold exactly stays a `Decimal`."""
    return exact_amount(value)


@dataclass
class GlRows:
    """Shapes one GL line (or its LP offset) into an upload row."""

    spec: TemplateSpec
    gp_id: str
    fund_id: str
    fund_name: str
    account: Callable[[str, str], str]  # (source account, transaction type) -> Golden account number or ""
    investor: Callable[[str], tuple[str, str]]  # a reference in the file -> (Ledgerline ID, name) or ("", "")

    def row(self, ln: Line, offset: bool = False) -> dict[str, Any]:
        """Account_Name is the account as the source file names it; Account_Number is the Golden account."""
        out: dict[str, Any] = dict.fromkeys(self.spec.fields, "")
        inv_id, inv_name = self.investor(ln.inv) if ln.inv else ("", "")
        out.update({
            "Event_ID": f"{ln.j}-{ln.ln}" + ("-OFF" if offset else ""), "GP_ID": self.gp_id,
            "Fund_ID": self.fund_id, "Fund_Name": self.fund_name, "Investor_ID": inv_id, "Investor_Name": inv_name,
            "Event_Date": ln.date, "Transaction_Type": ln.tt, "Account_Number": self.account(ln.acct, ln.tt),
            "Account_Name": ln.acct, "Total_Amount": net_to_float(ln.dr, ln.cr),
            "Journal_ID": ln.j, "Description": ln.desc,
        })
        return {f: out.get(f, "") for f in self.spec.fields}


def offset_line(ln: Line) -> Line:
    """The offsetting line of an LP line: the same line with its two sides swapped."""
    return Line(ln.j, ln.ln, ln.acct, ln.an, ln.tt, ln.cr, ln.dr, ln.desc, inv=ln.inv, date=ln.date)


def golden_account(accounts: Mapping[str, Any], known: Callable[[str, str], str], source_account: str,
                   transaction_type: str) -> str:
    """The Golden account number for a source account: the person's mapping for this job (`accounts`), else what the
    sponsor or the organisation already knows. "" when there is none (reported, never guessed)."""
    picked = accounts.get(source_account)
    return str(picked).partition(" ")[0] if picked else known(source_account, transaction_type)


def read_gl_lines(path: str | Path, sheet: str | None = None, columns: Mapping[str, str] | None = None,
                  header_row: int = 1) -> Iterator[tuple[int, Line]]:
    """(sheet row, line) for each line of a GL source file (CSV or XLSX), one at a time: a million lines never sit
    in memory. `columns` maps line parts (j, ln, acct, an, tt, dr, cr, desc, inv, date) to the file's headers."""
    from worker.quality import Source

    src = Source(path, sheet, header_row)
    at = {k: src.index_of(h) for k, h in {**GL_COLUMNS, **(columns or {})}.items()}

    def cell(row: list[Any], key: str) -> Any:
        k = at[key]
        return row[k] if k is not None and k < len(row) else None

    for n, row in src.rows():
        yield n, Line(cell_text(cell(row, "j")), int(float(cell(row, "ln") or 0)), cell_text(cell(row, "acct")),
                      cell_text(cell(row, "an")), cell_text(cell(row, "tt")), money_cell(cell(row, "dr")),
                      money_cell(cell(row, "cr")), cell_text(cell(row, "desc")), inv=cell_text(cell(row, "inv")),
                      date=cell_text(cell(row, "date")))
