"""One line of a general-ledger source file.

A neutral home for the record the GL readers, the console views and the engine share, so that no module needs the
sample world just to name this type. `worker.sample.Line` is this class.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Line:
    j: str
    ln: int
    acct: str
    an: str
    tt: str
    dr: float | Decimal  # a float when it holds the cell exactly, else the exact Decimal
    cr: float | Decimal
    desc: str
    inv: str = ""
    ven: str = ""
    aff: str = ""
    invst: str = ""
    noDeal: bool = False
    date: str = ""  # the source GL Date, as written in the file
    pr: str = ""  # the source PR_DATE (preferred-return date), as written; blank when the file has none
    shares: float | None = None  # NO_OF_SHARES; None when the file has no such column or the cell is blank
    agn: str = ""  # AGNST_COMMIT as written ("Yes"/"No"); blank when the file has none
