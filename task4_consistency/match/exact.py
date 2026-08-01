"""Exact equality matcher on normalized values."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MatchOutcome:
    equal: bool
    left: str | None
    right: str | None
    score: float = 1.0


def exact_match(a: str | None, b: str | None) -> MatchOutcome:
    if a is None or b is None:
        return MatchOutcome(equal=False, left=a, right=b, score=0.0)
    return MatchOutcome(equal=(a == b), left=a, right=b, score=1.0 if a == b else 0.0)


def all_equal(values: list[str | None]) -> bool:
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return True
    first = present[0]
    return all(v == first for v in present[1:])
