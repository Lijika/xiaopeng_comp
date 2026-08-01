"""Fuzzy string matcher (stdlib SequenceMatcher) + CJK confusable short-name band."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

# Lightweight OCR / look-alike pairs for Chinese given names (safe subset)
# Only used to promote hard SequenceMatcher misses into uncertain — never auto-match.
_CJK_CONFUSABLE_PAIRS: set[tuple[str, str]] = {
    ("伟", "玮"),
    ("玮", "伟"),
    ("明", "铭"),
    ("铭", "明"),
    ("强", "彊"),
    ("丽", "莉"),
    ("莉", "丽"),
    ("华", "譁"),
    ("军", "钧"),
    ("钧", "军"),
    ("峰", "锋"),
    ("锋", "峰"),
    ("婷", "亭"),
    ("霞", "瑕"),
    ("杰", "傑"),
    ("国", "國"),
    ("國", "国"),
}


@dataclass
class FuzzyOutcome:
    match: bool
    score: float
    left: str | None
    right: str | None
    uncertain: bool = False


def fuzzy_ratio(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def single_confusable_diff(a: str, b: str) -> bool:
    """True if same length and exactly one char differs by confusable pair."""
    if len(a) != len(b) or not a:
        return False
    diffs = [(ca, cb) for ca, cb in zip(a, b) if ca != cb]
    if len(diffs) != 1:
        return False
    return diffs[0] in _CJK_CONFUSABLE_PAIRS


def adaptive_uncertain_band(a: str, b: str, base_band: float) -> float:
    """Widen band for short strings so 1-char OCR near-miss can be uncertain.

    Chinese names are often 2–4 chars; SequenceMatcher drops sharply on 1-char edit.
    """
    n = min(len(a), len(b))
    if n <= 2:
        # 2-char: default still needs score>=0.63 for band; confusable handled separately
        return max(base_band, 0.25)
    if n <= 4:
        return max(base_band, 0.20)
    return base_band


def fuzzy_match(
    a: str | None,
    b: str | None,
    threshold: float = 0.88,
    uncertain_band: float = 0.05,
    *,
    adaptive_band: bool = True,
) -> FuzzyOutcome:
    if a is None or b is None:
        return FuzzyOutcome(match=False, score=0.0, left=a, right=b, uncertain=True)
    if a == b:
        return FuzzyOutcome(match=True, score=1.0, left=a, right=b, uncertain=False)
    score = fuzzy_ratio(a, b)
    if score >= threshold:
        return FuzzyOutcome(match=True, score=score, left=a, right=b, uncertain=False)
    band = adaptive_uncertain_band(a, b, uncertain_band) if adaptive_band else uncertain_band
    if score >= max(0.0, threshold - band):
        return FuzzyOutcome(match=False, score=score, left=a, right=b, uncertain=True)
    # Short-name confusable single-char (伟/玮): score often 0.5 → uncertain not hard fail
    if single_confusable_diff(a, b):
        return FuzzyOutcome(match=False, score=max(score, 0.75), left=a, right=b, uncertain=True)
    return FuzzyOutcome(match=False, score=score, left=a, right=b, uncertain=False)


def multi_fuzzy_all(
    values: list[str | None],
    threshold: float = 0.88,
    uncertain_band: float = 0.05,
    *,
    adaptive_band: bool = True,
) -> FuzzyOutcome:
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return FuzzyOutcome(match=True, score=1.0, left=None, right=None, uncertain=True)
    worst = FuzzyOutcome(match=True, score=1.0, left=present[0], right=present[0])
    saw_uncertain = False
    for i in range(len(present)):
        for j in range(i + 1, len(present)):
            out = fuzzy_match(
                present[i],
                present[j],
                threshold,
                uncertain_band,
                adaptive_band=adaptive_band,
            )
            if not out.match and not out.uncertain:
                return out
            if out.uncertain:
                saw_uncertain = True
                if out.score < worst.score or worst.match:
                    worst = out
            elif out.score < worst.score:
                worst = out
    if saw_uncertain and not worst.match:
        worst.uncertain = True
    return worst
