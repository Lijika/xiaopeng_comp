"""Money/amount normalizer -> decimal string without currency unit."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from task4_consistency.normalize.base import basic_clean
from task4_consistency.normalize.result import NormalizeResult

_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_CN_UNITS = {
    "十": 10,
    "百": 100,
    "千": 1000,
    "万": 10000,
    "亿": 100000000,
}

_UNIT_WORDS = ("亿", "万", "千", "百", "十")


def _fmt_dec(val: Decimal) -> str:
    s = format(val, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _parse_simple_cn_number(text: str) -> Decimal | None:
    """Parse pure Chinese numbers like 十二万八千 / 一百二十八万."""
    if not text or not any(ch in _CN_DIGITS or ch in _CN_UNITS for ch in text):
        return None
    if any(ch.isdigit() for ch in text):
        return None
    total = 0
    section = 0
    number = 0
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if unit >= 10000:
                section = (section + number) * unit
                total += section
                section = 0
                number = 0
            else:
                if number == 0:
                    number = 1
                section += number * unit
                number = 0
        else:
            return None
    return Decimal(total + section + number)


def _parse_mixed_arabic_cn_units(text: str) -> Decimal | None:
    """Parse mixed forms: 12万8千 / 1.5万 / 12.8万 / 1百万 / 3千2百."""
    if not any(u in text for u in _UNIT_WORDS):
        return None

    unit_table = [
        ("百万", Decimal(1000000)),
        ("千万", Decimal(10000000)),
        ("亿", Decimal(100000000)),
        ("万", Decimal(10000)),
        ("千", Decimal(1000)),
        ("百", Decimal(100)),
        ("十", Decimal(10)),
    ]
    unit_alt = "|".join(re.escape(u) for u, _ in unit_table)
    token_re = re.compile(rf"(\d+(?:\.\d+)?)?({unit_alt})")
    unit_val = {u: v for u, v in unit_table}

    s = text
    total = Decimal(0)
    pos = 0
    matched_any = False
    for m in token_re.finditer(s):
        if m.start() < pos:
            continue
        gap = s[pos : m.start()]
        if gap and not re.fullmatch(r"[\s]*", gap):
            if matched_any:
                return None
            if not re.fullmatch(r"\d+(?:\.\d+)?", gap.strip()):
                return None
        num_s = m.group(1)
        unit = m.group(2)
        num = Decimal(num_s) if num_s else Decimal(1)
        total += num * unit_val[unit]
        pos = m.end()
        matched_any = True

    if not matched_any:
        return None

    tail = s[pos:].strip()
    if tail:
        if re.fullmatch(r"\d+(?:\.\d+)?", tail):
            total += Decimal(tail)
        else:
            return None
    return total


def _core_parse(text: str, original: str) -> Decimal | None:
    compact = text.replace(" ", "")

    # Scientific notation first (avoid 1.28e6 → partial 1.28)
    m = re.fullmatch(r"-?\d+(?:\.\d+)?[eE][+-]?\d+", compact)
    if m:
        try:
            return Decimal(m.group(0))
        except InvalidOperation:
            return None

    if any(u in text for u in _UNIT_WORDS) and re.search(r"\d", text):
        mixed = _parse_mixed_arabic_cn_units(text)
        if mixed is not None:
            return mixed

    cn_only = re.sub(r"[\s]", "", text)
    if any(ch in _CN_DIGITS or ch in _CN_UNITS for ch in cn_only) and not re.search(
        r"\d", cn_only
    ):
        cn = _parse_simple_cn_number(cn_only)
        if cn is not None:
            return cn

    m = re.fullmatch(r"-?\d+(?:\.\d+)?", compact)
    if m:
        try:
            return Decimal(m.group(0))
        except InvalidOperation:
            return None

    m = re.search(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", compact)
    if m and not any(u in original for u in _UNIT_WORDS):
        try:
            return Decimal(m.group(0))
        except InvalidOperation:
            return None
    return None


def normalize_money_ex(raw: str) -> NormalizeResult:
    """Normalize money; 约/大约/approx → value + money_approx note (ADV-06).

    Prefer 价税合计 when multiple labeled amounts appear in one OCR blob.
    """
    text = basic_clean(raw)
    if not text:
        return NormalizeResult(value=None)

    original = text
    approx = False
    notes: list[str] = []

    # Prefer tax-inclusive total when present (invoice OCR blobs)
    m_tax = re.search(
        r"价税合计\s*[:：]?\s*([0-9]+(?:[,，][0-9]{3})*(?:\.[0-9]+)?)",
        original,
    )
    if m_tax:
        num = m_tax.group(1).replace(",", "").replace("，", "")
        try:
            return NormalizeResult(
                value=_fmt_dec(Decimal(num)),
                notes=["money_prefer_price_tax_total"],
            )
        except InvalidOperation:
            pass

    # Explicit approx markers
    if re.search(r"(约|大约|近|大概|左右|approx\.?|about|~|～)", text, flags=re.I):
        approx = True
        notes.append("money_approx")
        text = re.sub(
            r"(约|大约|近|大概|左右|approx\.?|about|~|～)",
            "",
            text,
            flags=re.I,
        )

    text = text.replace(",", "").replace("，", "")
    # underscore thousands: 1_280_000
    text = text.replace("_", "")
    text = re.sub(r"[￥¥$€]|人民币|RMB|CNY|元整|元|圆", "", text, flags=re.I)
    text = text.strip()
    if not text:
        return NormalizeResult(
            value=None,
            notes=notes + (["money_empty_after_approx_strip"] if approx else []),
        )

    val = _core_parse(text, original)
    if val is None:
        if approx:
            return NormalizeResult(
                value=None,
                notes=notes + ["money_approx_unparseable"],
            )
        return NormalizeResult(value=None)

    return NormalizeResult(value=_fmt_dec(val), notes=notes)


def normalize_money(raw: str) -> str | None:
    return normalize_money_ex(raw).value


def money_to_decimal(normalized: str | None) -> Decimal | None:
    if normalized is None:
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None
