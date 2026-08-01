"""Date normalizer -> ISO YYYY-MM-DD.

Policy (Round3 / ADV-03/04):
- Ambiguous slash dates (both day & month ≤12) → None unless date_order set
- Year-month only (no day) → None (never invent day=01)
"""

from __future__ import annotations

import re
from datetime import datetime

from task4_consistency.normalize.base import basic_clean
from task4_consistency.normalize.result import NormalizeResult

_FORMATS_YEAR_FIRST = [
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y%m%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y年%m月%d日",
]


def _safe_date(y: int, m: int, d: int) -> str | None:
    try:
        return datetime(y, m, d).strftime("%Y-%m-%d")
    except ValueError:
        return None


def normalize_date_ex(
    raw: str,
    *,
    date_order: str | None = None,
) -> NormalizeResult:
    text = basic_clean(raw)
    if not text:
        return NormalizeResult(value=None)

    # Incomplete Chinese year-month: 2023年1月  (no day)
    if re.fullmatch(r"\d{4}\s*年\s*\d{1,2}\s*月", text):
        return NormalizeResult(value=None, notes=["date_incomplete_year_month"])

    # Chinese full: 2023年1月15日 or 2023年1月15
    m = re.fullmatch(
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?",
        text,
    )
    if m:
        out = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if out:
            return NormalizeResult(value=out)

    # Compact 8-digit yyyymmdd only when year-leading pure digits
    compact = re.sub(r"[\s\-.]", "", text)
    if re.fullmatch(r"\d{8}", compact) and (
        re.fullmatch(r"\d{8}", text) or re.match(r"^\d{4}[\-/.]?", text)
    ):
        # reject if looks like ddmmyyyy starting with day (01/02/2023 digits handled later)
        if re.fullmatch(r"\d{8}", text) or re.match(r"^\d{4}", text):
            out = _safe_date(int(compact[:4]), int(compact[4:6]), int(compact[6:8]))
            if out is not None and (
                re.fullmatch(r"\d{8}", text) or re.match(r"^\d{4}", text)
            ):
                # Only accept pure 8-digit or year-first dotted forms already handled
                if re.fullmatch(r"\d{8}", text) or re.match(
                    r"^\d{4}[\-/.]\d{1,2}[\-/.]\d{1,2}", text
                ):
                    return NormalizeResult(value=out)
                if re.fullmatch(r"\d{8}", text):
                    return NormalizeResult(value=out)

    if re.fullmatch(r"\d{8}", text):
        out = _safe_date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        if out:
            return NormalizeResult(value=out)

    # Year-first unambiguous strptime
    for fmt in _FORMATS_YEAR_FIRST:
        try:
            dt = datetime.strptime(text, fmt)
            return NormalizeResult(value=dt.strftime("%Y-%m-%d"))
        except ValueError:
            continue

    # Year-first loose yyyy-m-d
    m = re.fullmatch(r"(\d{4})[./\-](\d{1,2})[./\-](\d{1,2})", text)
    if m:
        out = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if out:
            return NormalizeResult(value=out)

    # d/m/Y or m/d/Y (slash/dot/dash)
    m = re.fullmatch(r"(\d{1,2})[./\-](\d{1,2})[./\-](\d{4})", text)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        order = (date_order or "").upper() or None
        if a > 12 and b <= 12:
            out = _safe_date(y, b, a)
            if out:
                return NormalizeResult(value=out, notes=["date_order_inferred_dmy"])
            return NormalizeResult(value=None, notes=["date_invalid"])
        if b > 12 and a <= 12:
            out = _safe_date(y, a, b)
            if out:
                return NormalizeResult(value=out, notes=["date_order_inferred_mdy"])
            return NormalizeResult(value=None, notes=["date_invalid"])
        if a <= 12 and b <= 12:
            if order == "DMY":
                out = _safe_date(y, b, a)
                if out:
                    return NormalizeResult(value=out, notes=["date_order_config_dmy"])
            elif order == "MDY":
                out = _safe_date(y, a, b)
                if out:
                    return NormalizeResult(value=out, notes=["date_order_config_mdy"])
            else:
                return NormalizeResult(
                    value=None,
                    notes=["date_ambiguous_dmy_mdy", f"parts={a}/{b}/{y}"],
                )
        return NormalizeResult(value=None, notes=["date_invalid_parts"])

    return NormalizeResult(value=None)


def normalize_date(raw: str, date_order: str | None = None) -> str | None:
    return normalize_date_ex(raw, date_order=date_order).value
