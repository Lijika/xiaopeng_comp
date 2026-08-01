"""License plate normalizer."""

from __future__ import annotations

import re

from task4_consistency.normalize.base import basic_clean

# Province short names for plates
_PROVINCE = "京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼使领"


def normalize_plate(raw: str) -> str | None:
    text = basic_clean(raw)
    if not text:
        return None
    text = text.upper()
    # Remove separators and spaces
    text = re.sub(r"[\s\-·•_./]", "", text)
    # Drop 号牌 / 车牌 prefix noise
    text = re.sub(r"^(号牌号码|车牌号码|车牌号|号牌)", "", text)
    return text if text else None


def normalize_plate_list(raw: str) -> str | None:
    """Normalize a multi-plate list; each element via plate normalizer."""
    text = basic_clean(raw)
    if not text:
        return None
    # Split common separators first (keep order)
    parts: list[str]
    for sep in ["|", ";", "、", ",", "/"]:
        if sep in text:
            parts = [p.strip() for p in text.split(sep) if p.strip()]
            break
    else:
        # also split on whitespace if multiple tokens
        parts = [p for p in re.split(r"\s+", text) if p]

    norms: list[str] = []
    for p in parts:
        n = normalize_plate(p)
        if n:
            norms.append(n)
    if not norms:
        return None
    return "|".join(norms)


def looks_like_plate(normalized: str) -> bool:
    if not normalized:
        return False
    if normalized[0] not in _PROVINCE and not normalized[0].isalpha():
        return False
    return 7 <= len(normalized) <= 8
