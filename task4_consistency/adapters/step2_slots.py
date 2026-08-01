"""Load/validate step2 OCR slot lists (raw=null until external OCR fills).

Schema: ``task4.external_ocr_slots.v1`` under ``fixtures/ocr_inbox/step2_slots_*.json``.
No OCR engine — only structure checks for the crop→OCR pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SLOTS_SCHEMA = "task4.external_ocr_slots.v1"
REQUIRED_SLOT_KEYS = ("field", "bbox", "raw")


class Step2SlotsError(ValueError):
    def __init__(self, error: str, message: str):
        self.error = error
        super().__init__(message)


def validate_step2_slots(data: Any) -> dict[str, Any]:
    """Validate step2 slots payload; return data if ok."""
    if not isinstance(data, dict):
        raise Step2SlotsError("invalid_root", "root must be a JSON object")
    if data.get("schema") != SLOTS_SCHEMA:
        raise Step2SlotsError(
            "bad_schema",
            f"schema must be {SLOTS_SCHEMA!r}, got {data.get('schema')!r}",
        )
    sid = data.get("sample_id")
    if not isinstance(sid, str) or not sid.strip():
        raise Step2SlotsError("missing_sample_id", "sample_id required non-empty string")
    if not isinstance(data.get("doc_type"), str) or not str(data.get("doc_type")).strip():
        raise Step2SlotsError("missing_doc_type", "doc_type required")
    slots = data.get("slots")
    if not isinstance(slots, list) or not slots:
        raise Step2SlotsError("bad_slots", "slots must be non-empty list")
    n_slots = data.get("n_slots")
    if n_slots is not None and int(n_slots) != len(slots):
        raise Step2SlotsError(
            "n_slots_mismatch",
            f"n_slots={n_slots} != len(slots)={len(slots)}",
        )
    for i, slot in enumerate(slots):
        if not isinstance(slot, dict):
            raise Step2SlotsError("bad_slot", f"slots[{i}] must be object")
        for k in REQUIRED_SLOT_KEYS:
            if k not in slot:
                raise Step2SlotsError("missing_slot_key", f"slots[{i}].{k} required")
        if not str(slot.get("field") or "").strip():
            raise Step2SlotsError("empty_field", f"slots[{i}].field empty")
        bbox = slot.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise Step2SlotsError("bad_bbox", f"slots[{i}].bbox must be length-4 list")
        # raw may be null (pre-OCR) or str after OCR fill
        raw = slot.get("raw")
        if raw is not None and not isinstance(raw, str):
            raise Step2SlotsError("bad_raw", f"slots[{i}].raw must be str|null")
    return data


def load_step2_slots(path: str | Path) -> dict[str, Any]:
    """Load JSON file and validate step2 slots schema."""
    p = Path(path)
    if not p.is_file():
        raise Step2SlotsError("file_not_found", f"not a file: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise Step2SlotsError("invalid_json", str(e)) from e
    return validate_step2_slots(data)


def list_step2_slot_files(inbox: str | Path) -> list[Path]:
    """List ``step2_slots_*.json`` under ocr_inbox (sorted)."""
    d = Path(inbox)
    if not d.is_dir():
        return []
    return sorted(d.glob("step2_slots_*.json"))
