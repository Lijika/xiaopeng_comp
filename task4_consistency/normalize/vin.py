"""VIN normalizer with optional I/O/Q OCR fix + optional ISO check digit."""

from __future__ import annotations

import re

from task4_consistency.normalize.base import basic_clean
from task4_consistency.normalize.result import NormalizeResult

# Common OCR confusions for VIN (I/O/Q not used in standard VIN)
_OCR_FIX = str.maketrans(
    {
        "I": "1",
        "O": "0",
        "Q": "0",
        "i": "1",
        "o": "0",
        "q": "0",
    }
)

_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

# ISO 3779 transliteration for check digit
_VIN_TRANS = {
    **{str(i): i for i in range(10)},
    **{
        "A": 1,
        "B": 2,
        "C": 3,
        "D": 4,
        "E": 5,
        "F": 6,
        "G": 7,
        "H": 8,
        "J": 1,
        "K": 2,
        "L": 3,
        "M": 4,
        "N": 5,
        "P": 7,
        "R": 9,
        "S": 2,
        "T": 3,
        "U": 4,
        "V": 5,
        "W": 6,
        "X": 7,
        "Y": 8,
        "Z": 9,
    },
}
_VIN_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


def clean_vin(raw: str) -> str:
    text = basic_clean(raw)
    text = re.sub(r"[\s\-·•_./]", "", text)
    return text.upper()


def apply_ioq_fix(text: str) -> tuple[str, bool]:
    fixed = text.translate(_OCR_FIX)
    return fixed, fixed != text


def is_valid_vin(vin: str) -> bool:
    return bool(_VIN_RE.match(vin))


def vin_check_digit(vin17: str) -> str | None:
    """Compute ISO 3779 check character for positions 1-17 (pos 9 is check)."""
    if len(vin17) != 17:
        return None
    total = 0
    for i, ch in enumerate(vin17):
        if i == 8:
            continue  # check position
        if ch not in _VIN_TRANS:
            return None
        total += _VIN_TRANS[ch] * _VIN_WEIGHTS[i]
    rem = total % 11
    return "X" if rem == 10 else str(rem)


def is_valid_vin_check_digit(vin: str) -> bool:
    if not is_valid_vin(vin):
        return False
    expected = vin_check_digit(vin)
    return expected is not None and vin[8] == expected


def normalize_vin_ex(
    raw: str,
    *,
    fix_ioq: bool = True,
    validate: bool = True,
    strict_check_digit: bool = False,
) -> NormalizeResult:
    """Normalize VIN; ocr_fix when I/O/Q applied; optional check-digit strict."""
    text = clean_vin(raw)
    if not text:
        return NormalizeResult(value=None)
    pre = text
    notes: list[str] = []
    ocr_fixed = False
    if fix_ioq:
        text, ocr_fixed = apply_ioq_fix(text)
        if ocr_fixed:
            notes.append("vin_ioq_ocr_fix")
    if validate and not is_valid_vin(text):
        return NormalizeResult(
            value=None,
            ocr_fix=ocr_fixed,
            notes=notes + ["vin_invalid_charset_or_length"],
            pre_ocr=pre,
        )
    if strict_check_digit and not is_valid_vin_check_digit(text):
        return NormalizeResult(
            value=None,
            ocr_fix=ocr_fixed,
            notes=notes + ["vin_check_digit_fail"],
            pre_ocr=pre,
        )
    if not strict_check_digit and is_valid_vin(text) and not is_valid_vin_check_digit(text):
        notes.append("vin_check_digit_unchecked")
    return NormalizeResult(
        value=text,
        ocr_fix=ocr_fixed,
        notes=notes,
        pre_ocr=pre,
    )


def normalize_vin(
    raw: str,
    *,
    fix_ioq: bool = True,
    validate: bool = True,
    strict_check_digit: bool = False,
) -> str | None:
    return normalize_vin_ex(
        raw,
        fix_ioq=fix_ioq,
        validate=validate,
        strict_check_digit=strict_check_digit,
    ).value


def vin_edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (small strings only)."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[lb]
