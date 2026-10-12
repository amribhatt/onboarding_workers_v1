"""Accuracy checks: cheap, set-based proofs that nothing was lost, invented or altered on the way to the file.

Four checks, each a pure function over counts or over `RowTable`s (one query per question, any size):

* `conservation`: rows read = rows written + rows held, each held row with a reason (and the rows the code
  generated, such as an LP offset, are named, never hidden);
* `control_totals`: the totals of what is written equal the totals of what was staged, to the cent and exactly
  (Decimal, never float): debits, credits, net, row count;
* `paired_values`: for every target field that is a plain copy of a source column, the written value equals the
  source value on every kept row, and the multiset of values has the same checksum;
* `cell_accounting`: every source column with data is mapped to a target field, or dropped with a stated reason,
  or flagged; none is unaccounted for.

A failed check is a finding that blocks the approval and, if one slips through, refuses the write
(`require` raises `AccuracyError`). Nothing here changes a value.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from worker.setops import RowTable


class AccuracyError(ValueError):
    """One or more accuracy checks failed; the write does not happen."""


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    facts: Mapping[str, Any] = field(default_factory=dict)

    def as_record(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "facts": dict(self.facts)}


def conservation(read: int, out: int, held: Mapping[str, int], generated: int = 0) -> Check:
    """`read == out + held - generated`: every row read is written or held with a reason; rows the code added
    (`generated`) are the only rows written that were not read."""
    held_total = sum(held.values())
    expected = read - held_total + generated
    ok = out == expected and all(v >= 0 for v in held.values()) and read >= 0 and out >= 0
    reasons = ", ".join(f"{n:,} {why}" for why, n in held.items() if n) or "none held"
    extra = f" and {generated:,} generated" if generated else ""
    detail = (f"{read:,} rows read = {out:,} written{extra} + {held_total:,} held ({reasons})" if ok else
              f"{read:,} rows read, {out:,} written, {held_total:,} held ({reasons}){extra}: "
              f"{abs(out - expected):,} row(s) unexplained")
    return Check("conservation", ok, detail, {"read": read, "out": out, "held": dict(held), "generated": generated,
                                              "unexplained": out - expected})


def control_totals(expected: Mapping[str, Decimal | int], actual: Mapping[str, Decimal | int],
                   what: str = "the written file against the staged table") -> Check:
    """Exact equality of every named total (counts and Decimal sums)."""
    names = list(expected)
    off = [n for n in names if expected[n] != actual.get(n)]
    ok = not off
    detail = (f"{what}: " + ", ".join(f"{n} {expected[n]}" for n in names) + " tie out exactly" if ok else
              f"{what}: " + "; ".join(f"{n} should be {expected[n]} and is {actual.get(n)}" for n in off))
    return Check("control_totals", ok, detail, {"expected": {k: str(v) for k, v in expected.items()},
                                                "actual": {k: str(v) for k, v in actual.items()}})


def paired_values(source: RowTable, target: RowTable, pairs: Sequence[tuple[str, str]],
                  where: str = "") -> Check:
    """Source and target rows are joined on `_src`. For each (source field, target field) the written value must
    equal the source value (outer spaces ignored) on every row that was written, and the two sides must hold the
    same multiset of values (a checksum). `where` narrows the target side, for example `Event_ID NOT LIKE '%-OFF'`
    to leave out generated rows."""
    rows = source.paired_mismatches(target, pairs, where)
    bad = {f"{s} -> {t}": m for (s, t), m in rows["mismatches"].items() if m}
    off = {f"{s} -> {t}": c for (s, t), c in rows["checksums"].items() if c[0] != c[1]}
    ok = not bad and not off and rows["unmatched"] == 0
    if ok:
        detail = (f"{len(pairs)} copied field(s) hold the source values on all {rows['joined']:,} written rows "
                  "(checksums equal)")
    else:
        parts = [f"{k}: {m:,} row(s) differ" for k, m in bad.items()]
        parts += [f"{k}: checksum differs" for k in off]
        if rows["unmatched"]:
            parts.append(f"{rows['unmatched']:,} written row(s) have no source row")
        detail = "; ".join(parts)
    return Check("value_checksum", ok, detail, {"joined": rows["joined"], "pairs": [list(p) for p in pairs],
                                                "mismatches": bad, "unmatched": rows["unmatched"]})


def cell_accounting(columns: Mapping[str, int], mapped: Mapping[str, str], dropped: Mapping[str, str],
                    flagged: Mapping[str, str] | None = None) -> Check:
    """`columns` is every source column with its count of non-blank cells. Each one with data must be `mapped` (to
    a target field), `dropped` (with the reason it is not carried) or `flagged` (with what the person must
    look at). The check fails on a column with data that is none of these."""
    flagged = flagged or {}
    with_data = {c: n for c, n in columns.items() if n}
    loose = [c for c in with_data if c not in mapped and c not in dropped and c not in flagged]
    ok = not loose
    cells = sum(with_data.values())
    got = {c: ("mapped" if c in mapped else "dropped" if c in dropped else "flagged") for c in with_data}
    detail = (f"{len(with_data)} source column(s), {cells:,} cells: "
              f"{sum(1 for v in got.values() if v == 'mapped')} mapped, "
              f"{sum(1 for v in got.values() if v == 'dropped')} dropped with a reason, "
              f"{sum(1 for v in got.values() if v == 'flagged')} flagged" if ok else
              f"{len(loose)} source column(s) with data are neither mapped, dropped nor flagged: "
              + ", ".join(loose[:5]))
    return Check("cell_accounting", ok, detail, {
        "columns": {c: {"cells": n, "what": got.get(c, "empty"),
                        "to": mapped.get(c) or dropped.get(c) or flagged.get(c, "")} for c, n in columns.items()},
        "unaccounted": loose})


def failed(checks: Iterable[Check]) -> list[Check]:
    return [c for c in checks if not c.ok]


def require(checks: Iterable[Check]) -> None:
    """Raise `AccuracyError` naming every failed check; return quietly when all passed."""
    bad = failed(checks)
    if bad:
        raise AccuracyError("; ".join(f"{c.name}: {c.detail}" for c in bad))
