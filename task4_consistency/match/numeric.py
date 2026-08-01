"""Numeric tolerance matcher."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass
class NumericOutcome:
    match: bool
    left: Decimal | None
    right: Decimal | None
    abs_diff: Decimal | None = None
    rel_diff: Decimal | None = None


def _to_dec(v: str | float | int | Decimal | None) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def within_tolerance(
    a: Decimal,
    b: Decimal,
    abs_tol: float = 0.0,
    rel_tol: float = 0.0,
) -> bool:
    abs_diff = abs(a - b)
    if abs_diff <= Decimal(str(abs_tol)):
        return True
    denom = max(abs(a), abs(b), Decimal("1e-12"))
    rel_diff = abs_diff / denom
    return rel_diff <= Decimal(str(rel_tol))


def numeric_tolerance_match(
    a: str | float | int | Decimal | None,
    b: str | float | int | Decimal | None,
    abs_tol: float = 0.0,
    rel_tol: float = 0.0,
) -> NumericOutcome:
    da = _to_dec(a)
    db = _to_dec(b)
    if da is None or db is None:
        return NumericOutcome(match=False, left=da, right=db)
    abs_diff = abs(da - db)
    denom = max(abs(da), abs(db), Decimal("1e-12"))
    rel_diff = abs_diff / denom
    ok = within_tolerance(da, db, abs_tol=abs_tol, rel_tol=rel_tol)
    return NumericOutcome(match=ok, left=da, right=db, abs_diff=abs_diff, rel_diff=rel_diff)


def multi_numeric_all(
    values: list[str | None],
    abs_tol: float = 0.0,
    rel_tol: float = 0.0,
) -> NumericOutcome:
    present = [_to_dec(v) for v in values]
    present = [v for v in present if v is not None]
    if len(present) < 2:
        return NumericOutcome(match=True, left=None, right=None)
    base = present[0]
    worst = NumericOutcome(match=True, left=base, right=base, abs_diff=Decimal(0), rel_diff=Decimal(0))
    for other in present[1:]:
        out = numeric_tolerance_match(base, other, abs_tol=abs_tol, rel_tol=rel_tol)
        if not out.match:
            return out
        if out.abs_diff is not None and (worst.abs_diff is None or out.abs_diff > worst.abs_diff):
            worst = out
    return worst
