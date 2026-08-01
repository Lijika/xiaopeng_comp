"""Normalize result with OCR audit metadata."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NormalizeResult:
    value: str | None
    ocr_fix: bool = False
    notes: list[str] = field(default_factory=list)
    pre_ocr: str | None = None  # cleaned value before OCR substitutions

    @classmethod
    def of(cls, value: str | None) -> "NormalizeResult":
        return cls(value=value)
