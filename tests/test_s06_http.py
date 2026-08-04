from __future__ import annotations

from http.cookies import SimpleCookie
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from tests.test_s01_http import UvicornLoopback, demo_auth_headers, headers
from tests.test_s06_controlled import _attachment_submission, _png


ROOT = Path(__file__).resolve().parents[1]
S02_CREDENTIAL = "s06-http-integrator-credential"
S02_SUBJECT = "s06-http-integrator"
S02_SOURCE = "s06-material-source"
S02_WORKLOAD = "s06-material-workload"


def _http_source(tmp_path: Path) -> tuple[dict[str, str], dict[str, Any]]:
    object_root = tmp_path / "objects"
    object_root.mkdir()
    page = _png()
    result = json.dumps(
        {
            "per_image_results": [
                {
                    "image_path": "lease-page.png",
                    "image_size": {"width": 1, "height": 1},
                    "detections": [
                        {
                            "bbox": [0, 0, 1, 1],
                            "class_id": 1,
                            "class_name": "vehicle_identifier",
                            "confidence": 0.99,
                            "field_key": "vin",
                            "ocr_text": "LSVAA4182N2444555",
                            "value": "LSVAA4182N2444555",
                        }
                    ],
                }
            ]
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    (object_root / "result.json").write_bytes(result)
    (object_root / "page.png").write_bytes(page)
    registry = {
        "schema_version": "s02-runtime-registry/1",
        "sources": [
            {
                "tenant_id": "c-demo",
                "source_system_id": S02_SOURCE,
                "workload_identity_id": S02_WORKLOAD,
                "adapter_id": "s06-http-detection-adapter",
                "adapter_version": "1",
                "source_shape": "ocr-detection/unversioned",
                "producer_family": "s06-ocr",
                "enabled": True,
            }
        ],
        "objects": [
            {
                "tenant_id": "c-demo",
                "source_system_id": S02_SOURCE,
                "object_ref": "s06-result-object",
                "media_type": "application/json",
                "file": "result.json",
            },
            {
                "tenant_id": "c-demo",
                "source_system_id": S02_SOURCE,
                "object_ref": "s06-page-object",
                "media_type": "image/png",
                "file": "page.png",
            },
        ],
    }
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    def descriptor(ref: str, media: str, content: bytes) -> dict[str, Any]:
        return {
            "controlled_object_ref": ref,
            "media_type": media,
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    submission = {
        "envelope_id": "s06-http-admission-envelope",
        "schema_version": "1.0.0",
        "semantic_version": "1.0.0",
        "command_type": "submit_observation_result",
        "upstream_application_ref": "S06-HTTP-ADMISSION",
        "stream_id": "s06-http-admission-stream",
        "source_revision": 1,
        "predecessor_revision": None,
        "must_understand": [],
        "workload_identity_id": S02_WORKLOAD,
        "document_binding": {
            "source_document_ref": "s06-http-source-document",
            "document_type": "financing_lease_contract",
            "document_role": "financing_lease_contract",
        },
        "result_object": descriptor(
            "s06-result-object", "application/json", result
        ),
        "attachments": [
            {
                "source_attachment_ref": "s06-http-source-attachment",
                "page_ref": "s06-http-source-page",
                "page_ordinal": 1,
                "source_name_sha256": hashlib.sha256(
                    b"lease-page.png"
                ).hexdigest(),
                "object": descriptor("s06-page-object", "image/png", page),
            }
        ],
        "producer": {
            "producer_id": "s06-http-producer",
            "producer_family": "s06-ocr",
            "task_id": "s06-http-extraction",
            "task_version": "1",
            "run_id": "s06-http-producer-run",
            "model_id": "s06-http-model",
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
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
        "TASK4_S02_TEST_STATE_PATH": str(state_path),
        "TASK4_S02_TEST_REGISTRY_PATH": str(registry_path),
        "TASK4_S02_TEST_OBJECT_ROOT": str(object_root),
        "TASK4_S02_CREDENTIAL": S02_CREDENTIAL,
        "TASK4_S02_SUBJECT": S02_SUBJECT,
        "TASK4_S02_TENANT_ID": "c-demo",
        "TASK4_S02_SOURCE_SYSTEM_ID": S02_SOURCE,
        "TASK4_S02_TEST_SCENARIO_ID": "app_missing_vin_docs.json",
        "TASK4_S02_TEST_BACKGROUND_ENABLED": "1",
    }
    return environment, {"submission": submission, "result": result, "page": page}


def _s02_cookie(response: Any) -> str:
    cookies = SimpleCookie()
    cookies.load(response.headers["set-cookie"])
    return f"s02_session={cookies['s02_session'].value}"


def _wait_queue(server: UvicornLoopback, application_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        response = server.request(
            "GET",
            "/controlled/s01/api/queries/queue",
            headers=headers("reviewer"),
        )
        item = next(
            (
                candidate
                for candidate in response.json()["items"]
                if candidate["application_id"] == application_id
            ),
            None,
        )
        if item is not None:
            return item
        time.sleep(0.05)
    raise AssertionError("S06 review queue did not become visible")


def test_s06_reviewer_integrator_request_receipt_rerun_and_restart_http(
    tmp_path: Path,
) -> None:
    environment, source = _http_source(tmp_path)
    state_path = environment["TASK4_S01_TEST_STATE_PATH"]
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        server.open_s01_session()
        admission = server.request(
            "POST",
            "/controlled/s01/api/commands/submit",
            body={
                "scenario_id": "app_missing_vin_docs.json",
                "idempotency_key": "s06-http-demo",
            },
            headers=headers("integrator"),
        )
        assert admission.status == 200, admission.text
        application_id = admission.json()["application_id"]
        item = _wait_queue(server, application_id)
        review = server.request(
            "GET",
            f"/controlled/s01/api/queries/review-work-items/{item['work_item_id']}",
            headers=headers("reviewer"),
        )
        finding = next(
            candidate
            for candidate in review.json()["automatic_findings"]
            if candidate["rule_id"] == "R_VIN_CROSS"
        )
        claim = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{item['work_item_id']}/claim",
            body={"expected_context": review.json()["command_context"]},
            headers=headers("reviewer"),
        )
        request_response = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{item['work_item_id']}/supplement",
            body={
                "finding_id": finding["finding_id"],
                "reason_code": "MISSING_REQUIRED_MATERIAL",
                "expected_fence": claim.json()["claim_fence"],
                "expected_context": review.json()["command_context"],
                "idempotency_key": "s06-http-request",
            },
            headers=headers("reviewer"),
        )
        assert request_response.status == 200, request_response.text
        request = request_response.json()
        request_view = server.request(
            "GET",
            f"/controlled/s01/api/queries/supplement-requests/{request['request_id']}",
            headers=headers("reviewer"),
        )
        assert request_view.status == 200
        assert request_view.json()["due_at"] > int(time.time())

        integrator_page = server.request(
            "GET",
            "/controlled/s02",
            headers={"Authorization": f"Bearer {S02_CREDENTIAL}"},
            use_session=False,
        )
        integrator_cookie = _s02_cookie(integrator_page)
        attachment = _attachment_submission(
            request_view.json(), source, closed=False
        )
        wrong_role = server.request(
            "POST",
            "/controlled/s02/api/commands/submit-attachment-version",
            body={"idempotency_key": "s06-http-wrong-role", "submission": attachment},
            headers=headers("reviewer"),
        )
        progress = server.request(
            "POST",
            "/controlled/s02/api/commands/submit-attachment-version",
            body={"idempotency_key": "s06-http-progress", "submission": attachment},
            headers={"Cookie": integrator_cookie},
            use_session=False,
        )
        assert wrong_role.status == 403
        assert progress.status == 200, progress.text
        assert progress.json()["request_status"] == "open"
        closure = _attachment_submission(request_view.json(), source, closed=True)
        fulfilled = server.request(
            "POST",
            "/controlled/s02/api/commands/submit-attachment-version",
            body={"idempotency_key": "s06-http-closure", "submission": closure},
            headers={"Cookie": integrator_cookie},
            use_session=False,
        )
        assert fulfilled.status == 200, fulfilled.text
        assert fulfilled.json()["request_status"] == "fulfilled"

        deadline = time.monotonic() + 8
        route = None
        history = None
        while time.monotonic() < deadline:
            route_response = server.request(
                "GET",
                f"/controlled/s01/api/queries/applications/{application_id}/current-route",
                headers=headers("reviewer"),
            )
            history_response = server.request(
                "GET",
                f"/controlled/s01/api/queries/applications/{application_id}/history",
                headers=headers("reviewer"),
            )
            if (
                route_response.status == 200
                and history_response.status == 200
                and len(history_response.json()["runs"]) == 2
                and route_response.json()["phase"] == "Manual Review"
                and route_response.json()["current_run_id"]
                == history_response.json()["runs"][1]["run_id"]
            ):
                route = route_response.json()
                history = history_response.json()
                break
            time.sleep(0.05)
        assert route is not None
        assert history is not None
        assert route["phase"] == "Manual Review"
        assert route["current_run_id"] == history["runs"][1]["run_id"]
        assert history["runs"][0]["current"] is False
        reviewer_cookie = server._session_cookie
        assert reviewer_cookie is not None

    with UvicornLoopback(
        {**environment, "TASK4_S01_TEST_STATE_PATH": state_path},
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as restarted:
        route_after_restart = restarted.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/current-route",
            headers={**headers("reviewer"), "Cookie": reviewer_cookie},
            use_session=False,
        )
        request_after_restart = restarted.request(
            "GET",
            f"/controlled/s01/api/queries/supplement-requests/{request['request_id']}",
            headers={**headers("reviewer"), "Cookie": reviewer_cookie},
            use_session=False,
        )

    assert route_after_restart.status == 200
    assert route_after_restart.json()["current_run_id"] == route["current_run_id"]
    assert request_after_restart.status == 200
    assert request_after_restart.json()["status"] == "fulfilled"
