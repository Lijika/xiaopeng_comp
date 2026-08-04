from __future__ import annotations

import copy
import hashlib
import json
import struct
import time
import zlib
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import pytest

from tests.test_s01_http import UvicornLoopback


TENANT = "tenant-http"
SCOPE = f"R-OBSERVED/{TENANT}"
SOURCE = "registered-http-source"
SUBJECT = "registered-http-reviewer"
CREDENTIAL = "synthetic-s02-http-credential"


def _png() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
        + chunk(b"IEND", b"")
    )


def _descriptor(ref: str, media_type: str, content: bytes) -> dict[str, Any]:
    return {
        "controlled_object_ref": ref,
        "media_type": media_type,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _configured_http_source(tmp_path: Path) -> tuple[dict[str, str], dict[str, Any]]:
    object_root = tmp_path / "objects"
    object_root.mkdir()
    page_bytes = _png()
    result = {
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
                        "ocr_text": "SAFE-VIN-A",
                        "value": "SAFE-VIN-A",
                    }
                ],
            }
        ]
    }
    result_bytes = json.dumps(
        result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    (object_root / "result.json").write_bytes(result_bytes)
    (object_root / "page.png").write_bytes(page_bytes)
    registry = {
        "schema_version": "s02-runtime-registry/1",
        "sources": [
            {
                "tenant_id": TENANT,
                "source_system_id": SOURCE,
                "workload_identity_id": "http-workload",
                "adapter_id": "http-detection-adapter",
                "adapter_version": "1",
                "source_shape": "ocr-detection/unversioned",
                "producer_family": "http-ocr",
                "enabled": True,
            }
        ],
        "objects": [
            {
                "tenant_id": TENANT,
                "source_system_id": SOURCE,
                "object_ref": "http-result-object",
                "media_type": "application/json",
                "file": "result.json",
            },
            {
                "tenant_id": TENANT,
                "source_system_id": SOURCE,
                "object_ref": "http-page-object",
                "media_type": "image/png",
                "file": "page.png",
            },
        ],
    }
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    submission = {
        "envelope_id": "http-envelope-1",
        "schema_version": "1.0.0",
        "semantic_version": "1.0.0",
        "command_type": "submit_observation_result",
        "upstream_application_ref": "http-upstream-1",
        "stream_id": "http-stream-1",
        "source_revision": 1,
        "predecessor_revision": None,
        "must_understand": [],
        "workload_identity_id": "http-workload",
        "document_binding": {
            "source_document_ref": "http-document-1",
            "document_type": "motor_vehicle_registration_certificate",
            "document_role": "registration_certificate",
        },
        "result_object": _descriptor(
            "http-result-object", "application/json", result_bytes
        ),
        "attachments": [
            {
                "source_attachment_ref": "http-attachment-1",
                "page_ref": "http-page-1",
                "page_ordinal": 1,
                "source_name_sha256": hashlib.sha256(b"page.png").hexdigest(),
                "object": _descriptor("http-page-object", "image/png", page_bytes),
            }
        ],
        "producer": {
            "producer_id": "http-producer",
            "producer_family": "http-ocr",
            "task_id": "registration-extraction",
            "task_version": "1",
            "run_id": "http-run-1",
            "model_id": "http-model",
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
    state_path = tmp_path / "target.sqlite3"
    environment = {
        "TASK4_S01_STATE_PATH": str(state_path),
        "TASK4_S02_TEST_STATE_PATH": str(state_path),
        "TASK4_S02_TEST_REGISTRY_PATH": str(registry_path),
        "TASK4_S02_TEST_OBJECT_ROOT": str(object_root),
        "TASK4_S02_CREDENTIAL": CREDENTIAL,
        "TASK4_S02_SUBJECT": SUBJECT,
        "TASK4_S02_TENANT_ID": TENANT,
        "TASK4_S02_SOURCE_SYSTEM_ID": SOURCE,
        "TASK4_S02_TEST_BACKGROUND_ENABLED": "1",
    }
    return environment, submission


def _session_cookie(response: Any) -> str:
    cookies = SimpleCookie()
    cookies.load(response.headers["set-cookie"])
    value = cookies["s02_session"].value
    return f"s02_session={value}"


def _open_session(server: UvicornLoopback) -> str:
    response = server.request(
        "GET",
        "/controlled/s02",
        headers={"Authorization": f"Bearer {CREDENTIAL}"},
        use_session=False,
    )
    assert response.status == 200
    return _session_cookie(response)


def test_integrator_receipt_reaches_minimized_reviewer_trace_over_http(
    tmp_path: Path,
) -> None:
    environment, submission = _configured_http_source(tmp_path)
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        page = server.request(
            "GET",
            "/controlled/s02",
            headers={"Authorization": f"Bearer {CREDENTIAL}"},
            use_session=False,
        )
        cookie = _session_cookie(page)
        request_headers = {"Cookie": cookie}
        admission = server.request(
            "POST",
            "/controlled/s02/api/commands/submit",
            body={"idempotency_key": "http-command-1", "submission": submission},
            headers=request_headers,
            use_session=False,
        )
        receipt = admission.json()
        deadline = time.monotonic() + 8
        queue: dict[str, Any] = {"items": []}
        while time.monotonic() < deadline:
            queue_response = server.request(
                "GET",
                "/controlled/s02/api/queries/queue",
                headers=request_headers,
                use_session=False,
            )
            queue = queue_response.json()
            if queue["items"]:
                break
            time.sleep(0.05)
        item = queue["items"][0]
        workspace_response = server.request(
            "GET",
            f"/controlled/s02/api/queries/applications/{receipt['application_id']}/workspace",
            headers=request_headers,
            use_session=False,
        )
        workspace = workspace_response.json()

        assert page.status == 200
        assert admission.status == 200
        assert receipt["disposition"] == "accepted"
        assert receipt["tenant_id"] == TENANT
        assert receipt["source_system_id"] == SOURCE
        assert receipt["claim_label"] == "R-OBSERVED"
        assert receipt["real_cross_document_opportunities"] == 0
        assert receipt["performance_status"] == "not_estimable"
        assert receipt["fact_counts"]["observations"] == 1
        assert "provenance:eligible" in receipt["gate_results"]
        assert item["application_id"] == receipt["application_id"]
        assert item["phase"] == "Manual Review"
        assert workspace_response.status == 200
        assert workspace["track"] == "R-OBSERVED"
        finding = workspace["selected_finding"]
        assert finding["rule_id"] == "R-OBSERVED"
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
        assert len(link["source_sha256"]) == 64
        assert len(link["provenance_manifest_digest"]) == 64
        assert link["source_page"] == 1
        assert link["source_region"] == "region:1"
        assert link["producer_id"] == "http-producer"
        assert link["producer_family"] == "http-ocr"
        assert link["producer_run_id"] == "http-run-1"
        assert link["model_id"] == "http-model"
        assert link["model_version"] == "1"
        assert link["source_receipt_id"] == receipt["receipt_id"]
        assert "SAFE-VIN-A" not in json.dumps(receipt)
        assert "SAFE-VIN-A" not in json.dumps(workspace)
        assert "bbox" not in json.dumps(workspace, sort_keys=True)
        assert "[0,0,1,1]" not in json.dumps(workspace, sort_keys=True)
        for response in (page, admission, queue_response, workspace_response):
            assert response.headers["cache-control"] == "no-store"

        hidden_queue = server.request(
            "GET", "/controlled/s02/api/queries/queue", use_session=False
        )
        hidden_workspace = server.request(
            "GET",
            f"/controlled/s02/api/queries/applications/{receipt['application_id']}/workspace",
            use_session=False,
        )
        assert hidden_queue.status == 200
        assert hidden_queue.json() == {"items": [], "projection_watermark": 0}
        assert hidden_workspace.status == 404


@pytest.mark.parametrize(
    "location",
    (
        "envelope",
        "document_binding",
        "result_object",
        "attachment",
        "attachment_object",
        "producer",
        "coordinate_system",
        "confidence_semantics",
    ),
)
def test_unknown_canonical_contract_keys_fail_closed_over_http(
    tmp_path: Path, location: str
) -> None:
    environment, baseline = _configured_http_source(tmp_path)
    submission = copy.deepcopy(baseline)
    if location == "envelope":
        submission["unregistered_extension"] = "synthetic"
    elif location == "document_binding":
        submission["document_binding"]["unregistered_extension"] = "synthetic"
    elif location == "result_object":
        submission["result_object"]["unregistered_extension"] = "synthetic"
    elif location == "attachment":
        submission["attachments"][0]["unregistered_extension"] = "synthetic"
    elif location == "attachment_object":
        submission["attachments"][0]["object"]["unregistered_extension"] = "synthetic"
    elif location == "producer":
        submission["producer"]["unregistered_extension"] = "synthetic"
    else:
        submission["producer"][location]["unregistered_extension"] = "synthetic"

    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        cookie = _open_session(server)
        response = server.request(
            "POST",
            "/controlled/s02/api/commands/submit",
            body={"idempotency_key": f"strict-{location}", "submission": submission},
            headers={"Cookie": cookie},
            use_session=False,
        )

    receipt = response.json()
    assert response.status == 200
    assert receipt["disposition"] == "rejected"
    assert receipt["reason_code"] == "intake.schema_unsupported"
    assert receipt["fact_counts"]["applications"] == 0


def test_oversized_command_body_is_rejected_before_intake(tmp_path: Path) -> None:
    environment, _ = _configured_http_source(tmp_path)
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        cookie = _open_session(server)
        response = server.request(
            "POST",
            "/controlled/s02/api/commands/submit",
            body={
                "idempotency_key": "body-limit",
                "submission": {"padding": "x" * (300 * 1024)},
            },
            headers={"Cookie": cookie},
            use_session=False,
        )
        queue = server.request(
            "GET",
            "/controlled/s02/api/queries/queue",
            headers={"Cookie": cookie},
            use_session=False,
        )

    assert response.status == 413
    assert response.json()["detail"]["error"] == "S02_COMMAND_TOO_LARGE"
    assert queue.json() == {"items": [], "projection_watermark": 0}


def test_invalid_runtime_registry_is_confined_without_configuration_leakage(
    tmp_path: Path,
) -> None:
    environment, _ = _configured_http_source(tmp_path)
    registry_path = Path(environment["TASK4_S02_TEST_REGISTRY_PATH"])
    synthetic_locator = "/synthetic/private/registry-object"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": "s02-runtime-registry/1",
                "sources": [],
                "objects": [{"file": synthetic_locator}],
            }
        ),
        encoding="utf-8",
    )

    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        health = server.request("GET", "/api/health", use_session=False)
        page = server.request(
            "GET",
            "/controlled/s02",
            headers={"Authorization": f"Bearer {CREDENTIAL}"},
            use_session=False,
        )

    assert health.status == 200
    assert page.status == 503
    assert page.json()["detail"]["error"] == "S02_UNAVAILABLE"
    for private_value in (
        str(registry_path),
        environment["TASK4_S02_TEST_OBJECT_ROOT"],
        CREDENTIAL,
        synthetic_locator,
    ):
        assert private_value not in page.text
