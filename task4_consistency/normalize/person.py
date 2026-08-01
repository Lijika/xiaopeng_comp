"""Person name normalizer."""

from __future__ import annotations

import re

from task4_consistency.normalize.base import basic_clean, fullwidth_to_halfwidth

# Lightweight same-char OCR / spacing variants only in MVP
_DOTS = re.compile(r"[·•．.・]")


def normalize_person_name(raw: str) -> str | None:
    text = basic_clean(raw)
    text = fullwidth_to_halfwidth(text)
    if not text:
        return None
    # Keep middle-dot for ethnic names but normalize form
    text = _DOTS.sub("·", text)
    text = re.sub(r"\s+", "", text)
    # Drop common titles/suffixes
    for suffix in ("先生", "女士", "小姐", "同志"):
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
    return text if text else None
