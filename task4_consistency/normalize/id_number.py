"""Chinese ID number normalizer with safe 15↔18 linking."""

from __future__ import annotations

import re

from task4_consistency.normalize.base import basic_clean
from task4_consistency.normalize.result import NormalizeResult

_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_CHECK_MAP = "10X98765432"


def make_valid_id18(body17: str) -> str:
    """Build 18-digit ID with correct checksum from 17-digit body."""
    if len(body17) != 17 or not body17.isdigit():
        raise ValueError("body17 must be 17 digits")
    total = sum(int(d) * w for d, w in zip(body17, _WEIGHTS))
    return body17 + _CHECK_MAP[total % 11]


def is_valid_cn_id18(id18: str) -> bool:
    if len(id18) != 18:
        return False
    body = id18[:17]
    if not body.isdigit():
        return False
    if id18[17] not in "0123456789X":
        return False
    total = sum(int(d) * w for d, w in zip(body, _WEIGHTS))
    return _CHECK_MAP[total % 11] == id18[17]


def id15_to_id18(id15: str) -> str | None:
    """Expand 15-digit ID to 18-digit using century 19 (historical GB practice).

    Safe scope: only pure 15-digit numeric; always insert '19' before YY.
    Does NOT guess 20xx (would create false links across centuries).
    """
    if len(id15) != 15 or not id15.isdigit():
        return None
    body17 = id15[:6] + "19" + id15[6:]
    return make_valid_id18(body17)


def ids_link_equivalent(a: str, b: str) -> bool:
    """True if same person under 15/18 expansion (19xx only)."""
    if a == b:
        return True
    if len(a) == 15 and len(b) == 18:
        exp = id15_to_id18(a)
        return exp is not None and exp == b
    if len(b) == 15 and len(a) == 18:
        exp = id15_to_id18(b)
        return exp is not None and exp == a
    return False


def normalize_id_number_ex(
    raw: str,
    *,
    validate: bool = True,
    strict_checksum: bool = True,
    expand_15_to_18: bool = True,
) -> NormalizeResult:
    text = basic_clean(raw)
    if not text:
        return NormalizeResult(value=None)
    text = re.sub(r"[\s\-·•_]", "", text)
    text = text.upper()
    text = re.sub(r"[^0-9X]", "", text)
    if not text:
        return NormalizeResult(value=None)

    if not validate:
        return NormalizeResult(value=text)

    notes: list[str] = []
    if len(text) == 15 and text.isdigit():
        if expand_15_to_18:
            exp = id15_to_id18(text)
            if exp is None:
                return NormalizeResult(value=None, notes=["id15_expand_fail"])
            return NormalizeResult(
                value=exp,
                notes=["id15_expanded_19xx"],
                pre_ocr=text,
            )
        return NormalizeResult(value=text, notes=["id15_kept"])

    if len(text) == 18:
        if not text[:17].isdigit():
            return NormalizeResult(value=None, notes=["id18_body_non_digit"])
        if text[17] not in "0123456789X":
            return NormalizeResult(value=None, notes=["id18_bad_check_char"])
        if strict_checksum and not is_valid_cn_id18(text):
            return NormalizeResult(value=None, notes=["id18_checksum_fail"])
        return NormalizeResult(value=text, notes=notes)

    return NormalizeResult(value=None, notes=["id_invalid_length"])


def normalize_id_number(
    raw: str,
    *,
    validate: bool = True,
    strict_checksum: bool = True,
    expand_15_to_18: bool = True,
) -> str | None:
    return normalize_id_number_ex(
        raw,
        validate=validate,
        strict_checksum=strict_checksum,
        expand_15_to_18=expand_15_to_18,
    ).value
