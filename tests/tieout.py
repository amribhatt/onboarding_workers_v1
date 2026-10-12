"""The tie-out of a written file, recomputed from the file's own rows (the oracle the lock and the parity tests use).

Until M22 `worker.output.tie_out` made this record in the console's write; the engine's `write_upload` record now
holds the same figures (`tie_out` in its outputs). The tests recompute them here, from the rows that were read back
from the upload file, so the figure is never taken from the code that wrote the file.
"""

from __future__ import annotations

from typing import Any

from worker.money import cents, to_decimal


def tie_out(rows: list[dict[str, Any]], record_type: str, expected: int) -> dict[str, Any]:
    """Facts that must hold for the written file: the row count against what the check counted, and for a GL file
    the debits, credits, net and journal count."""
    facts: dict[str, Any] = {"rows": len(rows), "engine_written": expected, "ties": len(rows) == expected}
    if record_type in ("GL Fund", "GL LP"):  # the console's two GL types; the agent's template name "GL" is not one
        amounts = [to_decimal(r["Total_Amount"]) for r in rows if r["Total_Amount"] != ""]
        facts.update({"debits": float(cents(sum(a for a in amounts if a > 0))),
                      "credits": float(cents(-sum(a for a in amounts if a < 0))),
                      "net": float(cents(sum(amounts))), "journals": len({r["Journal_ID"] for r in rows})})
    return facts
