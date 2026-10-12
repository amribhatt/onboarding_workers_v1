"""A big GL fixture, generated on demand (never committed): 1,000,000 lines with the shapes of scenarios G1 and G2.

* 10,214 lines on account 6150, which has no Ledgerline account, in three blocks of rows:
  1204-7203, 256000-258999 and 909000-910213 (sheet rows; row 1 is the header);
* 4 accounts x 10,000 lines with a blank deal where one is required: 1500, 1600, 8050 and 9010.
Every other line sits on account 1000 with a deal. Columns are the source columns of the sample GL (`GLSRC`).
"""

from __future__ import annotations

import csv
from pathlib import Path

from worker.sample import GLSRC

ROWS = 1_000_000
UNMAPPED = [(1204, 7203), (256000, 258999), (909000, 910213)]
BLANK_DEAL = {"1500": (10_000, 19_999), "1600": (20_000, 29_999), "8050": (30_000, 39_999), "9010": (40_000, 49_999)}


def build(path: Path, rows: int = ROWS) -> Path:
    owner: dict[int, str] = {}
    for a, b in UNMAPPED:
        for n in range(a, b + 1):
            owner[n] = "6150"
    for acct, (a, b) in BLANK_DEAL.items():
        for n in range(a, b + 1):
            owner[n] = acct
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(GLSRC)
        for n in range(2, rows + 1):
            acct = owner.get(n, "1000")
            deal = "" if acct in BLANK_DEAL else "Deal X"
            w.writerow(["2025-12-31", f"J-{n // 2}", n, acct, f"Account {acct}", "Expense", 10, "", "line", "", "", "",
                        deal, "Fund A", "B1"])
    return path
