from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "configs" / "s02_source_conformance.json"


def _shape(payload: dict[str, Any]) -> str | None:
    if "per_image_results" in payload:
        return "ocr-detection/unversioned"
    if payload.get("schema") == "task4.external_ocr_slots.v1":
        return "step2-slots/v1"
    if "pages" in payload:
        return "step2-page-order/unversioned"
    if payload.get("schema_version") == 1 and "documents" in payload:
        return "external-ocr/v1"
    if "fields" in payload:
        return "ocr-aggregate/unversioned"
    return None


def _occurrences(shape: str, payload: dict[str, Any]) -> int:
    if shape == "step2-page-order/unversioned":
        return sum(len(page.get("detections") or []) for page in payload.get("pages") or [])
    if shape == "step2-slots/v1":
        return len(payload.get("slots") or [])
    if shape == "ocr-detection/unversioned":
        return sum(
            len(page.get("detections") or [])
            for page in payload.get("per_image_results") or []
        )
    if shape == "external-ocr/v1":
        return sum(len(document.get("fields") or {}) for document in payload.get("documents") or [])
    fields = payload.get("fields") or {}
    if isinstance(fields, dict) and isinstance(fields.get("fields"), dict):
        fields = fields["fields"]
    return sum(
        len(value.get("sources") or [])
        for value in fields.values()
        if isinstance(value, dict)
    )


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_conformance_manifest_registers_observed_shapes_and_legacy_callers() -> None:
    manifest = _manifest()
    shapes = {item["source_shape"] for item in manifest["registered_shapes"]}

    assert manifest["schema_version"] == "s02-source-conformance/1"
    assert manifest["adapter_build"] == "s02-registered-source-adapters/1"
    assert shapes == {
        "step2-page-order/unversioned",
        "step2-slots/v1",
        "ocr-aggregate/unversioned",
        "ocr-detection/unversioned",
        "external-ocr/v1",
    }
    assert {item["legacy_id"] for item in manifest["caller_reconciliation"]} == {
        "A07",
        "A08",
        "A09",
    }
    assert all(
        item["legacy_target_authority_writes"] is False
        for item in manifest["caller_reconciliation"]
    )
    assert all(item["unsupported_facts"] for item in manifest["registered_shapes"])


def test_environment_materials_have_registered_shapes_and_no_loss_records() -> None:
    configured = os.environ.get("TASK4_S02_CONFORMANCE_ROOTS")
    if not configured:
        pytest.skip("S02 conformance roots are not configured")
    roots = [Path(value) for value in configured.split(os.pathsep) if value]
    manifest = _manifest()
    registrations = {
        item["source_shape"]: item for item in manifest["registered_shapes"]
    }
    records: list[tuple[str, str, int]] = []
    unknown_shapes = 0
    unknown_keys = 0
    observed_shapes: set[str] = set()

    for root in roots:
        for source in sorted(root.rglob("*.json")):
            source_bytes = source.read_bytes()
            payload = json.loads(source_bytes.decode("utf-8"))
            shape = _shape(payload) if isinstance(payload, dict) else None
            if shape is None or shape not in registrations:
                unknown_shapes += 1
                continue
            registration = registrations[shape]
            known = set(registration["accepted_top_level"]) | set(
                registration["unsupported_top_level"]
            )
            unknown_keys += len(set(payload).difference(known))
            count = _occurrences(shape, payload)
            if count < 1:
                unknown_shapes += 1
                continue
            records.append((shape, hashlib.sha256(source_bytes).hexdigest(), count))
            observed_shapes.add(shape)
            assert source.read_bytes() == source_bytes

    expected = {
        value
        for value in os.environ.get("TASK4_S02_CONFORMANCE_EXPECTED_SHAPES", "").split(",")
        if value
    }
    aggregate = hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    assert records
    assert unknown_shapes == 0
    assert unknown_keys == 0
    assert not expected or observed_shapes == expected
    assert len(aggregate) == 64
