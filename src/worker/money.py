"""Money arithmetic, once: every balance, tolerance and total in the worker goes through here.

Amounts are `Decimal`. A float that comes from a spreadsheet cell is converted through its shortest repr
(`1234.565` stays `1234.565`, never `1234.56499999...`), sums are exact, and rounding to cents is half-up (the way
a ledger rounds), applied once at the end. `recon.py` can import this module instead of keeping its own float
arithmetic.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

CENT = Decimal("0.01")
ZERO = Decimal(0)


def to_decimal(value: Any) -> Decimal:
    """The amount in a cell: None or blank is zero; "1,234.50" and "(1,234.50)" (negative) are read as numbers.
    Raises ValueError for text that is not a number and for NaN or infinity."""
    if value is None:
        return ZERO
    if isinstance(value, bool):
        raise ValueError(f"not an amount: {value!r}")  # noqa: TRY004 (callers catch ValueError for every bad cell)
    if isinstance(value, Decimal):
        out = value
    elif isinstance(value, int):
        out = Decimal(value)
    else:
        text = repr(value) if isinstance(value, float) else str(value).strip().replace(",", "")
        if not text:
            return ZERO
        negative = text.startswith("(") and text.endswith(")")
        if negative:
            text = text[1:-1]
        try:
            out = Decimal(text)
        except InvalidOperation:
            raise ValueError(f"not an amount: {value!r}") from None
        if negative:
            out = -out
    if not out.is_finite():
        raise ValueError(f"not an amount: {value!r}")
    return out


def exact_amount(value: Any) -> float | Decimal:
    """A debit or credit cell as a number that loses nothing. It is the float the cell holds when that float is
    exactly the cell's decimal value (the usual case, and what every consumer already takes), and the `Decimal`
    when a float would round it (an amount of more than fifteen significant digits). A blank is 0.0."""
    if not value:
        return 0.0
    try:
        exact = to_decimal(value)
    except ValueError:
        return float(value)  # what the cell is, as it was read before; the caller sees the same error
    as_float = float(exact)
    return as_float if to_decimal(as_float) == exact else exact


def cents(value: Any) -> Decimal:
    """Rounded half-up to two places; zero is always +0.00 (never -0.00)."""
    d = to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)
    return ZERO.quantize(CENT) if d == 0 else d


def to_float(value: Any) -> float:
    """The cent-rounded amount as the number a spreadsheet cell holds."""
    return float(cents(value))


def net_to_float(debit: Any, credit: Any) -> float:
    """`to_float(debit - credit)` for the streaming path, which runs once per line of a million-line file.

    Plain floats are rounded directly unless the result sits near a half-cent tie (where binary error could pick
    the wrong side); then, and for any non-float input, the Decimal arithmetic decides. The answer is the same
    either way."""
    if isinstance(debit, int | float) and isinstance(credit, int | float) and not isinstance(debit, bool):
        x = (debit - credit) * 100
        if math.isfinite(x):
            frac = abs(x - math.floor(x) - 0.5)
            if frac > max(1e-6, abs(x) * 1e-12):
                return round(x) / 100 + 0.0
    return to_float(to_decimal(debit) - to_decimal(credit))


def total(values: Iterable[Any]) -> Decimal:
    """The exact sum, rounded to cents once."""
    return cents(sum((to_decimal(v) for v in values), ZERO))


def balance_status(debits: Any, credits: Any, tol_pass: Any = 0, tol_warn: Any = 0) -> tuple[str, Decimal]:
    """("ok" | "warn" | "err", difference in cents) for a journal. The difference is debits - credits rounded to
    cents; it passes when its size is within `tol_pass`, warns within `tol_warn`, and is an error beyond."""
    diff = cents(to_decimal(debits) - to_decimal(credits))
    size = abs(diff)
    if size <= to_decimal(tol_pass):
        return "ok", diff
    if size <= to_decimal(tol_warn):
        return "warn", diff
    return "err", diff
