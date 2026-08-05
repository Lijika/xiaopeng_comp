from __future__ import annotations

import copy
import hashlib
import json
import struct
import zlib
from pathlib import Path
from typing import Any, Callable

import pytest

from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules
from task4_consistency.controlled.s01 import (
    AdmissionResult,
    AdmissionDisposition,
    ControlledScenarioService,
    QueryNotFound,
    S01CommandPrincipal,
)
from task4_consistency.controlled.s02 import ControlledObject, RegisteredSource


ROOT = Path(__file__).resolve().parents[1]
TENANT_SCOPE = "R-OBSERVED/tenant-test"
INTEGRATOR = S01CommandPrincipal(
    subject="registered-real-integrator",
    role="integrator",
    scope=TENANT_SCOPE,
    source_id="registered-source",
)


def _png(width: int = 1, height: int = 1) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    scanlines = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


def _damaged_png(kind: str) -> bytes:
    content = bytearray(_png())
    type_offset = content.index(b"IDAT")
    size = struct.unpack(">I", content[type_offset - 4 : type_offset])[0]
    payload_start = type_offset + 4
    payload_end = payload_start + size
    if kind == "crc":
        content[payload_end] ^= 0x01
    else:
        content[payload_start] ^= 0xFF
        crc = zlib.crc32(bytes(content[type_offset:payload_end])) & 0xFFFFFFFF
        content[payload_end : payload_end + 4] = struct.pack(">I", crc)
    return bytes(content)


def _descriptor(ref: str, media_type: str, content: bytes) -> dict[str, object]:
    return {
        "controlled_object_ref": ref,
        "media_type": media_type,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _detection_result() -> dict[str, object]:
    return {
        "per_image_results": [
            {
                "image_path": "page.png",
                "image_size": {"width": 1, "height": 1},
                "detections": [
                    {
                        "bbox": [0, 0, 1, 1],
                        "class_id": 1,
                        "class_name": "vehicle_identifier",
                        "confidence": 0.97,
                        "field_key": "vin",
                        "ocr_text": "TEST-VIN-A",
                        "value": "TEST-VIN-A",
                    }
                ],
            }
        ]
    }


def _registered_service(
    tmp_path: Path,
    *,
    enabled: bool = True,
    result: dict[str, object] | None = None,
    page_bytes: bytes | None = None,
    page_media_type: str = "image/png",
    object_tenant: str = "tenant-test",
    max_result_bytes: int = 2 * 1024 * 1024,
    max_attachment_bytes: int = 32 * 1024 * 1024,
    max_pages: int = 64,
    duplicate_registration: bool = False,
    source_shape: str = "ocr-detection/unversioned",
    checker_runner: Callable[[Any], Any] | None = None,
) -> tuple[ControlledScenarioService, dict[str, object]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    page_bytes = _png() if page_bytes is None else page_bytes
    result = _detection_result() if result is None else result
    result_bytes = json.dumps(
        result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    registration = RegisteredSource(
        tenant_id="tenant-test",
        source_system_id="registered-source",
        workload_identity_id="registration-workload",
        adapter_id="ocr-detection-unversioned",
        adapter_version="1",
        source_shape=source_shape,
        producer_family="registration-ocr",
        enabled=enabled,
        max_result_bytes=max_result_bytes,
        max_attachment_bytes=max_attachment_bytes,
        max_pages=max_pages,
    )
    registrations = (registration,)
    if duplicate_registration:
        registrations += (
            RegisteredSource(
                tenant_id="tenant-test",
                source_system_id="registered-source",
                workload_identity_id="registration-workload",
                adapter_id="ambiguous-registration",
                adapter_version="1",
                source_shape=source_shape,
                producer_family="other-producer-family",
            ),
        )
    objects = (
        ControlledObject(
            tenant_id=object_tenant,
            source_system_id="registered-source",
            object_ref="result-object",
            media_type="application/json",
            content=result_bytes,
        ),
        ControlledObject(
            tenant_id=object_tenant,
            source_system_id="registered-source",
            object_ref="page-object",
            media_type=page_media_type,
            content=page_bytes,
        ),
    )
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        registered_sources=registrations,
        controlled_objects=objects,
        checker_runner=checker_runner,
    )
    source_name_digest = hashlib.sha256(b"page.png").hexdigest()
    submission: dict[str, object] = {
        "envelope_id": "envelope-real-1",
        "schema_version": "1.0.0",
        "semantic_version": "1.0.0",
        "command_type": "submit_observation_result",
        "upstream_application_ref": "upstream-application-1",
        "stream_id": "source-stream-1",
        "source_revision": 1,
        "predecessor_revision": None,
        "must_understand": [],
        "workload_identity_id": "registration-workload",
        "document_binding": {
            "source_document_ref": "source-document-1",
            "document_type": "motor_vehicle_registration_certificate",
            "document_role": "registration_certificate",
        },
        "result_object": _descriptor("result-object", "application/json", result_bytes),
        "attachments": [
            {
                "source_attachment_ref": "source-attachment-1",
                "page_ref": "source-page-1",
                "page_ordinal": 1,
                "source_name_sha256": source_name_digest,
                "object": _descriptor("page-object", page_media_type, page_bytes),
            }
        ],
        "producer": {
            "producer_id": "registered-producer",
            "producer_family": "registration-ocr",
            "task_id": "registration-field-extraction",
            "task_version": "1",
            "run_id": "producer-run-1",
            "model_id": "registered-model",
            "model_version": "1",
            "coordinate_system": {
                "name": "pixel",
                "unit": "pixel",
                "origin": "top_left",
            },
            "confidence_semantics": {
                "minimum": 0.0,
                "maximum": 1.0,
                "higher_is": "stronger_detection",
                "meaning": "producer_detection_score",
                "granularity": "observation",
                "calibration": "unknown",
            },
        },
    }
    return service, submission


def _shape_service(
    tmp_path: Path,
    *,
    shape: str,
    payload: dict[str, object],
    suffix: str,
) -> tuple[ControlledScenarioService, dict[str, object]]:
    page_bytes = _png()
    result_bytes = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    registration = RegisteredSource(
        tenant_id="tenant-test",
        source_system_id="registered-source",
        workload_identity_id=f"workload-{suffix}",
        adapter_id=f"adapter-{suffix}",
        adapter_version="1",
        source_shape=shape,
        producer_family="registration-ocr",
    )
    result_ref = f"result-{suffix}"
    page_ref = f"page-{suffix}"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / f"target-{suffix}.sqlite3",
        registered_sources=(registration,),
        controlled_objects=(
            ControlledObject(
                tenant_id="tenant-test",
                source_system_id="registered-source",
                object_ref=result_ref,
                media_type="application/json",
                content=result_bytes,
            ),
            ControlledObject(
                tenant_id="tenant-test",
                source_system_id="registered-source",
                object_ref=page_ref,
                media_type="image/png",
                content=page_bytes,
            ),
        ),
    )
    submission: dict[str, object] = {
        "envelope_id": f"envelope-{suffix}",
        "schema_version": "1.0.0",
        "semantic_version": "1.0.0",
        "command_type": "submit_observation_result",
        "upstream_application_ref": f"upstream-{suffix}",
        "stream_id": f"stream-{suffix}",
        "source_revision": 1,
        "predecessor_revision": None,
        "must_understand": [],
        "workload_identity_id": f"workload-{suffix}",
        "document_binding": {
            "source_document_ref": f"document-{suffix}",
            "document_type": "motor_vehicle_registration_certificate",
            "document_role": "registration_certificate",
        },
        "result_object": _descriptor(result_ref, "application/json", result_bytes),
        "attachments": [
            {
                "source_attachment_ref": f"attachment-{suffix}",
                "page_ref": f"source-page-{suffix}",
                "page_ordinal": 1,
                "source_name_sha256": hashlib.sha256(b"page.png").hexdigest(),
                "object": _descriptor(page_ref, "image/png", page_bytes),
            }
        ],
        "producer": {
            "producer_id": "registered-producer",
            "producer_family": "registration-ocr",
            "task_id": "registration-field-extraction",
            "task_version": "1",
            "run_id": "producer-run-1",
            "model_id": "registered-model",
            "model_version": "1",
            "coordinate_system": {
                "name": "pixel",
                "unit": "pixel",
                "origin": "top_left",
            },
            "confidence_semantics": {
                "minimum": 0.0,
                "maximum": 1.0,
                "higher_is": "stronger_detection",
                "meaning": "producer_detection_score",
                "granularity": "observation",
                "calibration": "unknown",
            },
        },
    }
    return service, submission


def test_registered_detection_returns_atomic_accepted_receipt(tmp_path: Path) -> None:
    service, submission = _registered_service(tmp_path)

    receipt = service.submit_registered(
        submission=submission,
        idempotency_key="registered-receipt-1",
        principal=INTEGRATOR,
    )

    assert receipt.disposition is AdmissionDisposition.ACCEPTED
    assert receipt.lifecycle_revision == 1
    assert receipt.evidence_revision == 1
    assert receipt.reason_code == "intake.accepted"
    assert receipt.responsible_party == "none"
    assert receipt.recovery_action == "none"
    assert receipt.fact_counts == {
        "applications": 1,
        "receipts": 1,
        "idempotency_bindings": 1,
        "lifecycle_events": 1,
        "evidence_events": 1,
        "audit_events": 1,
        "jobs": 1,
        "outbox_events": 1,
        "attachments": 1,
        "pages": 1,
        "producer_results": 1,
        "observations": 1,
    }
    assert set(receipt.gate_results) == {
        "identity:verified",
        "contract:verified",
        "object:verified",
        "causality:verified",
        "tenant_source_binding:verified",
        "idempotency:bound",
        "provenance:eligible",
    }
    assert receipt.claim_label == "R-OBSERVED"
    assert receipt.real_cross_document_opportunities == 0
    assert receipt.performance_status == "not_estimable"


def test_programmatic_non_dict_registered_submission_returns_schema_rejection(
    tmp_path: Path,
) -> None:
    service, _ = _registered_service(tmp_path)

    receipt = service.submit_registered(
        submission=[],
        idempotency_key="registered-non-dict",
        principal=INTEGRATOR,
    )

    assert isinstance(receipt, AdmissionResult)
    assert receipt.disposition is AdmissionDisposition.REJECTED
    assert receipt.reason_code == "intake.schema_unsupported"
    assert receipt.receipt_id is not None
    assert "identity:verified" in receipt.gate_results
    assert "contract:failed" in receipt.gate_results
    assert receipt.application_id is None
    assert receipt.lifecycle_revision is None
    assert receipt.evidence_revision is None
    assert receipt.fact_counts["applications"] == 0
    assert receipt.fact_counts["receipts"] == 1
    assert receipt.fact_counts["evidence_events"] == 0
    assert receipt.fact_counts["lifecycle_events"] == 0


def test_observed_finding_traces_immutable_snapshot_to_source_receipt(
    tmp_path: Path,
) -> None:
    service, submission = _registered_service(tmp_path)
    receipt = service.submit_registered(
        submission=submission,
        idempotency_key="registered-trace-1",
        principal=INTEGRATOR,
    )

    completed = service.process_next_job()
    projected = service.refresh_projection()
    workspace = service.workspace_view(
        receipt.application_id or "",
        role="reviewer",
        scope=TENANT_SCOPE,
        subject=INTEGRATOR.subject,
    )

    assert completed.status == "complete"
    assert completed.evidence_snapshot_id
    assert completed.evidence_snapshot_digest
    assert projected["updated"] == 1
    assert workspace["track"] == "R-OBSERVED"
    assert workspace["claim_label"] == "R-OBSERVED"
    assert workspace["real_cross_document_opportunities"] == 0
    assert workspace["performance_status"] == "not_estimable"
    finding = workspace["selected_finding"]
    assert finding["rule_id"] == "R-OBSERVED"
    assert finding["reason_code"] == "R_OBSERVED_PROVENANCE_REVIEW"
    assert len(finding["evidence_links"]) == 1
    link = finding["evidence_links"][0]
    assert set(link) == {
        "document_id",
        "document_role",
        "field",
        "value_state",
        "raw_masked",
        "observation_id",
        "source_sha256",
        "provenance_manifest_digest",
        "source_page",
        "source_region",
        "producer_id",
        "producer_family",
        "producer_run_id",
        "model_id",
        "model_version",
        "source_receipt_id",
        "evidence_eligible",
        "eligibility_reason",
    }
    assert link["raw_masked"] == "[REDACTED]"
    assert link["source_sha256"] == submission["attachments"][0]["object"]["sha256"]
    assert link["source_page"] == 1
    assert link["source_region"] == "region:1"
    assert link["producer_id"] == "registered-producer"
    assert link["producer_family"] == "registration-ocr"
    assert link["producer_run_id"] == "producer-run-1"
    assert link["model_id"] == "registered-model"
    assert link["model_version"] == "1"
    assert link["source_receipt_id"] == receipt.receipt_id
    assert link["evidence_eligible"] is True
    assert "raw" not in link
    assert "source_pointer" not in link
    assert "bbox" not in json.dumps(workspace, sort_keys=True)
    assert "[0,0,1,1]" not in json.dumps(workspace, sort_keys=True)


def test_public_region_identity_distinguishes_same_page_regions_without_coordinates(
    tmp_path: Path,
) -> None:
    raw_values = ("PRIVATE-VIN", "PRIVATE-BRAND", "PRIVATE-OWNER")
    detections = [
        {
            "bbox": bbox,
            "class_id": index,
            "class_name": field,
            "confidence": 0.97,
            "field_key": field,
            "ocr_text": raw,
            "value": raw,
        }
        for index, (field, bbox, raw) in enumerate(
            (
                ("vin", [0, 0, 1, 1], raw_values[0]),
                ("brand", [2, 2, 3, 3], raw_values[1]),
                ("owner_name", [0, 0, 1, 1], raw_values[2]),
            ),
            start=1,
        )
    ]
    service, submission = _registered_service(
        tmp_path,
        page_bytes=_png(10, 10),
        result={
            "per_image_results": [
                {
                    "image_path": "page.png",
                    "image_size": {"width": 10, "height": 10},
                    "detections": detections,
                }
            ]
        },
    )

    receipt = service.submit_registered(
        submission=submission,
        idempotency_key="registered-distinct-regions",
        principal=INTEGRATOR,
    )
    completed = service.process_next_job()
    service.refresh_projection()
    workspace = service.workspace_view(
        receipt.application_id or "",
        role="reviewer",
        scope=TENANT_SCOPE,
        subject=INTEGRATOR.subject,
    )
    finding = next(
        item for item in workspace["mandatory_blockers"] if item["rule_id"] == "R-OBSERVED"
    )

    assert completed.status == "complete"
    assert [
        (link["field"], link["source_region"])
        for link in finding["evidence_links"]
    ] == [
        ("vin", "region:1"),
        ("brand", "region:2"),
        ("owner_name", "region:1"),
    ]
    public_surface = json.dumps(workspace, sort_keys=True)
    assert "bbox" not in public_surface
    assert "[0,0,1,1]" not in public_surface
    assert "[2,2,3,3]" not in public_surface
    assert all(raw not in public_surface for raw in raw_values)


def test_every_observed_shape_is_registered_without_sample_allowlisting(
    tmp_path: Path,
) -> None:
    shapes = (
        (
            "step2-page-order/unversioned",
            {
                "sample_id": "never-seen-before",
                "pages": [
                    {
                        "filename": "page.png",
                        "order": 1,
                        "detections": [
                            {
                                "bbox": [0, 0, 1, 1],
                                "class_id": 1,
                                "class_name_cn": "车辆识别代号/车架号",
                                "confidence": 0.91,
                            },
                            {
                                "bbox": [0, 0, 1, 1],
                                "class_id": 1,
                                "class_name_cn": "车辆识别代号/车架号",
                                "confidence": 0.42,
                            },
                        ],
                    }
                ],
            },
            "page-order",
            2,
            False,
        ),
        (
            "step2-slots/v1",
            {
                "schema": "task4.external_ocr_slots.v1",
                "sample_id": "never-seen-before",
                "doc_type": "registration_certificate",
                "n_slots": 2,
                "slots": [
                    {
                        "field": "vin",
                        "bbox": [0, 0, 1, 1],
                        "raw": None,
                        "confidence_det": 0.91,
                        "image_filename": "page.png",
                        "page_order": 1,
                    },
                    {
                        "field": "vin",
                        "bbox": [0, 0, 1, 1],
                        "raw": None,
                        "confidence_det": 0.42,
                        "image_filename": "page.png",
                        "page_order": 1,
                    },
                ],
            },
            "slots",
            2,
            False,
        ),
        (
            "ocr-aggregate/unversioned",
            {
                "sample_id": "never-seen-before",
                "fields": {
                    "车辆识别代号/车架号": {
                        "value": "TEST-VIN-A",
                        "consistent": True,
                        "sources": [
                            {"filename": "page.png"},
                            {"filename": "page.png"},
                        ],
                    }
                },
            },
            "aggregate",
            2,
            False,
        ),
        (
            "ocr-detection/unversioned",
            {
                "sample_id": "never-seen-before",
                "per_image_results": [
                    {
                        "image_path": "page.png",
                        "image_size": {"width": 1, "height": 1},
                        "detections": [
                            {
                                "bbox": [0, 0, 1, 1],
                                "class_id": 1,
                                "class_name": "vehicle_identifier",
                                "confidence": 0.91,
                                "field_key": "vin",
                                "ocr_text": "TEST-VIN-A",
                                "value": "TEST-VIN-A",
                            },
                            {
                                "bbox": [0, 0, 1, 1],
                                "class_id": 1,
                                "class_name": "vehicle_identifier",
                                "confidence": 0.42,
                                "field_key": "vin",
                                "ocr_text": "TEST-VIN-B",
                                "value": "TEST-VIN-B",
                            },
                        ],
                    }
                ],
            },
            "detection",
            2,
            True,
        ),
        (
            "external-ocr/v1",
            {
                "schema_version": 1,
                "application_id": "ignored-upstream-identity",
                "ocr_model": "registered-model",
                "ocr_version": "1",
                "documents": [
                    {
                        "doc_id": "ignored-document-identity",
                        "doc_type": "registration_certificate",
                        "fields": {
                            "vin": {
                                "raw": "TEST-VIN-A",
                                "confidence": 0.91,
                                "source_page": 1,
                            }
                        },
                    }
                ],
            },
            "external",
            1,
            False,
        ),
    )

    for shape, payload, suffix, expected_count, expected_eligible in shapes:
        service, submission = _shape_service(
            tmp_path,
            shape=shape,
            payload=payload,
            suffix=suffix,
        )
        receipt = service.submit_registered(
            submission=submission,
            idempotency_key=f"shape-{suffix}",
            principal=INTEGRATOR,
        )
        assert receipt.disposition is AdmissionDisposition.ACCEPTED
        assert receipt.fact_counts["observations"] == expected_count
        assert (
            "provenance:eligible" in receipt.gate_results
        ) is expected_eligible


@pytest.mark.parametrize(
    ("descriptor", "attribute", "reason_code"),
    (
        ("result", "sha256", "evidence.integrity_failed"),
        ("result", "size_bytes", "evidence.integrity_failed"),
        ("page", "media_type", "evidence.content_type_mismatch"),
    ),
)
def test_object_hash_size_and_media_mismatch_fail_closed(
    tmp_path: Path,
    descriptor: str,
    attribute: str,
    reason_code: str,
) -> None:
    service, submission = _registered_service(tmp_path)
    target = (
        submission["result_object"]
        if descriptor == "result"
        else submission["attachments"][0]["object"]
    )
    if attribute == "sha256":
        target[attribute] = "0" * 64
    elif attribute == "size_bytes":
        target[attribute] += 1
    else:
        target[attribute] = "image/jpeg"

    receipt = service.submit_registered(
        submission=submission,
        idempotency_key=f"object-{descriptor}-{attribute}",
        principal=INTEGRATOR,
    )

    assert receipt.disposition is AdmissionDisposition.QUARANTINED
    assert receipt.reason_code == reason_code
    assert receipt.lifecycle_revision is None
    assert receipt.evidence_revision is None
    assert receipt.fact_counts["applications"] == 0
    assert receipt.fact_counts["receipts"] == 1
    assert receipt.fact_counts["evidence_events"] == 0


@pytest.mark.parametrize(
    ("options", "disposition", "reason_code"),
    (
        ({"page_bytes": b"not-readable"}, "quarantined", "evidence.content_type_mismatch"),
        ({"max_attachment_bytes": 1}, "rejected", "intake.resource_limit_exceeded"),
        (
            {"page_media_type": "application/octet-stream"},
            "quarantined",
            "evidence.media_type_unsupported",
        ),
        (
            {"page_bytes": _png(width=20_001)},
            "rejected",
            "intake.resource_limit_exceeded",
        ),
    ),
)
def test_readability_and_registered_resource_limits_precede_admission(
    tmp_path: Path,
    options: dict[str, object],
    disposition: str,
    reason_code: str,
) -> None:
    service, submission = _registered_service(tmp_path, **options)

    receipt = service.submit_registered(
        submission=submission,
        idempotency_key="object-safety-limit",
        principal=INTEGRATOR,
    )

    assert receipt.disposition.value == disposition
    assert receipt.reason_code == reason_code
    assert receipt.fact_counts["applications"] == 0
    assert receipt.fact_counts["lifecycle_events"] == 0
    assert receipt.fact_counts["evidence_events"] == 0


@pytest.mark.parametrize("damage", ("crc", "deflate"))
def test_png_crc_and_deflate_structure_are_verified_before_admission(
    tmp_path: Path, damage: str
) -> None:
    service, submission = _registered_service(
        tmp_path, page_bytes=_damaged_png(damage)
    )

    receipt = service.submit_registered(
        submission=submission,
        idempotency_key=f"png-structure-{damage}",
        principal=INTEGRATOR,
    )

    assert receipt.disposition is AdmissionDisposition.QUARANTINED
    assert receipt.reason_code == "evidence.content_type_mismatch"
    assert receipt.lifecycle_revision is None
    assert receipt.evidence_revision is None
    assert receipt.fact_counts["applications"] == 0
    assert receipt.fact_counts["evidence_events"] == 0


@pytest.mark.parametrize(
    "unsafe_key",
    ("url", "path", "callback", "credential"),
)
def test_transport_locators_paths_redirects_and_credentials_are_rejected(
    tmp_path: Path, unsafe_key: str
) -> None:
    service, submission = _registered_service(tmp_path)
    submission[unsafe_key] = "redacted"

    receipt = service.submit_registered(
        submission=submission,
        idempotency_key=f"forbidden-{unsafe_key}",
        principal=INTEGRATOR,
    )

    assert receipt.disposition is AdmissionDisposition.REJECTED
    assert receipt.reason_code == "intake.forbidden_locator"
    assert receipt.fact_counts["applications"] == 0
    assert receipt.fact_counts["receipts"] == 1


def test_labels_ambiguous_registration_and_unknown_shapes_fail_closed(
    tmp_path: Path,
) -> None:
    labeled = _detection_result()
    labeled["label"] = "redacted"
    cases = (
        (
            _registered_service(tmp_path / "label", result=labeled),
            "intake.data_track_mismatch",
            AdmissionDisposition.REJECTED,
        ),
        (
            _registered_service(tmp_path / "ambiguous", duplicate_registration=True),
            "adapter.mapping_ambiguous",
            AdmissionDisposition.QUARANTINED,
        ),
        (
            _registered_service(tmp_path / "unknown", source_shape="unregistered/v1"),
            "adapter.source_format_unsupported",
            AdmissionDisposition.QUARANTINED,
        ),
    )

    for index, ((service, submission), reason, disposition) in enumerate(cases):
        receipt = service.submit_registered(
            submission=submission,
            idempotency_key=f"fail-closed-{index}",
            principal=INTEGRATOR,
        )
        assert receipt.disposition is disposition
        assert receipt.reason_code == reason
        assert receipt.fact_counts["applications"] == 0


def test_registered_shape_drift_fails_closed(tmp_path: Path) -> None:
    drifted = _detection_result()
    drifted["unregistered_extension"] = "redacted"
    service, submission = _registered_service(tmp_path, result=drifted)

    receipt = service.submit_registered(
        submission=submission,
        idempotency_key="shape-drift",
        principal=INTEGRATOR,
    )

    assert receipt.disposition is AdmissionDisposition.QUARANTINED
    assert receipt.reason_code == "adapter.source_format_unsupported"
    assert receipt.fact_counts["applications"] == 0


def test_missing_producer_run_model_coordinate_and_confidence_stay_ineligible(
    tmp_path: Path,
) -> None:
    service, submission = _registered_service(tmp_path)
    producer = submission["producer"]
    for key in ("run_id", "model_id", "model_version", "coordinate_system", "confidence_semantics"):
        producer.pop(key)

    receipt = service.submit_registered(
        submission=submission,
        idempotency_key="missing-provenance",
        principal=INTEGRATOR,
    )
    completed = service.process_next_job()
    service.refresh_projection()
    workspace = service.workspace_view(
        receipt.application_id or "",
        role="reviewer",
        scope=TENANT_SCOPE,
        subject=INTEGRATOR.subject,
    )
    finding = next(
        item for item in workspace["mandatory_blockers"] if item["rule_id"] == "R-OBSERVED"
    )
    link = finding["evidence_links"][0]

    assert receipt.disposition is AdmissionDisposition.ACCEPTED
    assert "provenance:ineligible" in receipt.gate_results
    assert completed.status == "complete"
    assert link["source_page"] == 1
    assert link["source_region"] == "region:1"
    assert link["source_receipt_id"] == receipt.receipt_id
    assert link["producer_id"] == "registered-producer"
    assert link["producer_family"] == "registration-ocr"
    assert all(
        field not in link
        for field in (
            "source_object_ref",
            "coordinate_system",
            "producer_run_id",
            "model_id",
            "model_version",
        )
    )
    assert link["evidence_eligible"] is False
    assert link["eligibility_reason"] == "evidence.producer_metadata_incomplete"


def test_layout_only_and_raw_null_observations_never_become_field_values(
    tmp_path: Path,
) -> None:
    service, submission = _shape_service(
        tmp_path,
        shape="step2-page-order/unversioned",
        payload={
            "sample_id": "unseen-layout-only",
            "pages": [
                {
                    "filename": "page.png",
                    "order": 1,
                    "detections": [
                        {
                            "bbox": [0, 0, 1, 1],
                            "class_name_cn": "车辆识别代号/车架号",
                            "confidence": 0.9,
                        },
                        {
                            "bbox": [0, 0, 1, 1],
                            "class_name_cn": "车辆识别代号/车架号",
                            "confidence": 0.4,
                        },
                    ],
                }
            ],
        },
        suffix="layout-only",
    )
    receipt = service.submit_registered(
        submission=submission,
        idempotency_key="layout-only",
        principal=INTEGRATOR,
    )
    service.process_next_job()
    service.refresh_projection()
    workspace = service.workspace_view(
        receipt.application_id or "",
        role="reviewer",
        scope=TENANT_SCOPE,
        subject=INTEGRATOR.subject,
    )
    finding = next(
        item for item in workspace["mandatory_blockers"] if item["rule_id"] == "R-OBSERVED"
    )

    assert len(finding["evidence_links"]) == 2
    assert all(link["value_state"] == "not_detected" for link in finding["evidence_links"])
    assert all(link["raw_masked"] is None for link in finding["evidence_links"])
    assert all(link["evidence_eligible"] is False for link in finding["evidence_links"])


def test_retries_duplicates_and_later_revisions_never_duplicate_accepted_facts(
    tmp_path: Path,
) -> None:
    service, submission = _registered_service(tmp_path)
    accepted = service.submit_registered(
        submission=submission,
        idempotency_key="stable-command",
        principal=INTEGRATOR,
    )
    replay = service.submit_registered(
        submission=copy.deepcopy(submission),
        idempotency_key="stable-command",
        principal=INTEGRATOR,
    )
    duplicate = service.submit_registered(
        submission=copy.deepcopy(submission),
        idempotency_key="same-revision-new-key",
        principal=INTEGRATOR,
    )
    conflicting = copy.deepcopy(submission)
    conflicting["envelope_id"] = "envelope-real-conflict"
    conflict = service.submit_registered(
        submission=conflicting,
        idempotency_key="same-revision-conflict",
        principal=INTEGRATOR,
    )
    later = copy.deepcopy(submission)
    later.update(
        {
            "envelope_id": "envelope-real-later",
            "source_revision": 2,
            "predecessor_revision": 1,
        }
    )
    late = service.submit_registered(
        submission=later,
        idempotency_key="later-revision",
        principal=INTEGRATOR,
    )

    assert accepted.disposition is AdmissionDisposition.ACCEPTED
    assert replay.replayed is True
    assert duplicate.replayed is True
    assert replay.receipt_id == duplicate.receipt_id == accepted.receipt_id
    assert all(count == 0 for count in replay.fact_counts.values())
    assert all(count == 0 for count in duplicate.fact_counts.values())
    assert conflict.disposition is AdmissionDisposition.QUARANTINED
    assert conflict.reason_code == "intake.source_revision_conflict"
    assert late.disposition is AdmissionDisposition.REJECTED
    assert late.reason_code == "evidence.late_input_requires_reopen"
    assert conflict.lifecycle_revision is None
    assert conflict.evidence_revision is None
    assert late.lifecycle_revision is None
    assert late.evidence_revision is None
    counts = service.fact_counts()
    assert counts["applications"] == 1
    assert counts["evidence_events"] == 1


def test_sequence_gap_waits_without_application_or_evidence_revision(tmp_path: Path) -> None:
    service, submission = _registered_service(tmp_path)
    submission.update(
        {
            "envelope_id": "envelope-gap",
            "source_revision": 3,
            "predecessor_revision": 2,
        }
    )

    waiting = service.submit_registered(
        submission=submission,
        idempotency_key="sequence-gap",
        principal=INTEGRATOR,
    )
    replay = service.submit_registered(
        submission=copy.deepcopy(submission),
        idempotency_key="sequence-gap",
        principal=INTEGRATOR,
    )

    assert waiting.disposition is AdmissionDisposition.AWAITING_PREDECESSOR
    assert waiting.reason_code == "intake.sequence_gap"
    assert waiting.responsible_party == "source_owner"
    assert waiting.recovery_action == "submit_and_reconcile_the_declared_predecessor"
    assert waiting.lifecycle_revision is None
    assert waiting.evidence_revision is None
    assert waiting.fact_counts["applications"] == 0
    assert waiting.fact_counts["receipts"] == 1
    assert waiting.fact_counts["lifecycle_events"] == 0
    assert waiting.fact_counts["evidence_events"] == 0
    assert replay.replayed is True
    assert all(count == 0 for count in replay.fact_counts.values())


def test_tenant_source_binding_and_reviewer_queries_are_isolated(tmp_path: Path) -> None:
    foreign_object_service, foreign_submission = _registered_service(
        tmp_path / "object-binding", object_tenant="tenant-other"
    )
    foreign_object = foreign_object_service.submit_registered(
        submission=foreign_submission,
        idempotency_key="foreign-object",
        principal=INTEGRATOR,
    )
    service, submission = _registered_service(tmp_path / "query")
    accepted = service.submit_registered(
        submission=submission,
        idempotency_key="tenant-query",
        principal=INTEGRATOR,
    )
    service.process_next_job()
    service.refresh_projection()

    assert foreign_object.disposition is AdmissionDisposition.QUARANTINED
    assert foreign_object.reason_code == "evidence.provenance_invalid"
    with pytest.raises(QueryNotFound):
        service.workspace_view(
            accepted.application_id or "",
            role="reviewer",
            scope="R-OBSERVED/tenant-other",
            subject=INTEGRATOR.subject,
        )
    assert service.queue_view(
        role="reviewer",
        scope="R-OBSERVED/tenant-other",
        subject=INTEGRATOR.subject,
    ) == {"items": [], "recovery_items": [], "projection_watermark": 0}


def test_quarantine_reconciliation_requires_a_new_key_and_preserves_history(
    tmp_path: Path,
) -> None:
    broken, submission = _registered_service(tmp_path, object_tenant="tenant-other")
    quarantined = broken.submit_registered(
        submission=submission,
        idempotency_key="quarantine-attempt",
        principal=INTEGRATOR,
    )
    repaired, repaired_submission = _registered_service(tmp_path)
    same_key = repaired.submit_registered(
        submission=repaired_submission,
        idempotency_key="quarantine-attempt",
        principal=INTEGRATOR,
    )
    accepted = repaired.submit_registered(
        submission=repaired_submission,
        idempotency_key="reconciled-attempt",
        principal=INTEGRATOR,
    )

    assert quarantined.disposition is AdmissionDisposition.QUARANTINED
    assert same_key.disposition is AdmissionDisposition.QUARANTINED
    assert same_key.replayed is True
    assert accepted.disposition is AdmissionDisposition.ACCEPTED
    assert quarantined.receipt_id != accepted.receipt_id
    counts = repaired.fact_counts()
    assert counts["applications"] == 1
    assert counts["receipts"] == 2
    assert counts["evidence_events"] == 1


def test_adapter_disable_preserves_accepted_snapshot_and_blocks_new_intake(
    tmp_path: Path,
) -> None:
    service, submission = _registered_service(tmp_path)
    accepted = service.submit_registered(
        submission=submission,
        idempotency_key="before-disable",
        principal=INTEGRATOR,
    )
    completed = service.process_next_job()
    service.refresh_projection()
    before = service.workspace_view(
        accepted.application_id or "",
        role="reviewer",
        scope=TENANT_SCOPE,
        subject=INTEGRATOR.subject,
    )

    disabled, new_submission = _registered_service(tmp_path, enabled=False)
    new_submission["envelope_id"] = "envelope-after-disable"
    blocked = disabled.submit_registered(
        submission=new_submission,
        idempotency_key="after-disable",
        principal=INTEGRATOR,
    )
    after = disabled.workspace_view(
        accepted.application_id or "",
        role="reviewer",
        scope=TENANT_SCOPE,
        subject=INTEGRATOR.subject,
    )

    assert completed.status == "complete"
    assert blocked.disposition is AdmissionDisposition.REJECTED
    assert blocked.reason_code == "intake.source_disabled"
    assert blocked.fact_counts["applications"] == 0
    assert after["evidence_snapshot_id"] == before["evidence_snapshot_id"]
    assert after["evidence_snapshot_digest"] == before["evidence_snapshot_digest"]
    assert after["selected_finding"] == before["selected_finding"]


def test_registered_observations_are_not_backfilled_into_the_legacy_model(
    tmp_path: Path,
) -> None:
    legacy = RuleEngine(load_rules(ROOT / "configs" / "rules_auto_lease.yaml"))
    observed_legacy_fields: list[list[dict[str, object]]] = []

    def legacy_runner(application: Any) -> Any:
        observed_legacy_fields.append([document.fields for document in application.documents])
        return legacy.run(application)

    service, submission = _registered_service(tmp_path, checker_runner=legacy_runner)
    receipt = service.submit_registered(
        submission=submission,
        idempotency_key="legacy-model-isolation",
        principal=INTEGRATOR,
    )
    result = service.process_next_job()

    assert receipt.disposition is AdmissionDisposition.ACCEPTED
    assert result.status == "complete"
    assert observed_legacy_fields == [[{}]]
