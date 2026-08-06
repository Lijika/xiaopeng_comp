from __future__ import annotations

from http.cookies import SimpleCookie
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from tests.test_s01_http import (
    LoopbackResponse,
    UvicornLoopback,
    demo_auth_headers,
    headers,
)
from tests.test_s06_controlled import (
    INTEGRATOR_BATCH_KEYS,
    INTEGRATOR_INTERNAL_KEYS,
    INTEGRATOR_MATERIAL_KEYS,
    INTEGRATOR_PROJECTION_KEYS,
    _attachment_submission_from_projection,
    _attachment_submission,
    _png,
)


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


def _s06_http_ready_supplement_request(
    server: UvicornLoopback,
) -> tuple[dict[str, Any], dict[str, Any]]:
    server.open_s01_session()
    admission = server.request(
        "POST",
        "/controlled/s01/api/commands/submit",
        body={
            "scenario_id": "app_missing_vin_docs.json",
            "idempotency_key": "s06-http-t04-demo",
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
            "idempotency_key": "s06-http-t04-request",
        },
        headers=headers("reviewer"),
    )
    assert request_response.status == 200, request_response.text
    return application_id, request_response.json()


def test_t04_integrator_projection_http_closed_contract(tmp_path: Path) -> None:
    environment, source = _http_source(tmp_path)
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        application_id, request = _s06_http_ready_supplement_request(server)

        command_view = server.request(
            "GET",
            f"/controlled/s01/api/queries/supplement-requests/{request['request_id']}",
            headers=headers("reviewer"),
        )
        assert command_view.status == 200
        assert set(command_view.json()) == {
            "schema_version",
            "request_id",
            "work_item_id",
            "source_work_item_id",
            "application_id",
            "cycle",
            "run_id",
            "finding_id",
            "rule_id",
            "finding_reason_code",
            "finding_verdict",
            "requester_claim_fence",
            "requested_at",
            "due_at",
            "fixed_context",
            "context_digest",
            "expected_predecessor_attachment_id",
            "expected_predecessor_attachment_version",
            "satisfaction_policy_digest",
            "status",
            "current",
            "phase",
            "route",
            "lifecycle_revision",
            "evidence_revision",
            "projection_watermark",
            "material_requirement",
        }

        integrator_page = server.request(
            "GET",
            "/controlled/s02",
            headers={"Authorization": f"Bearer {S02_CREDENTIAL}"},
            use_session=False,
        )
        integrator_cookie = _s02_cookie(integrator_page)

        def integrator_projection(request_id: str) -> LoopbackResponse:
            return server.request(
                "GET",
                f"/controlled/s02/api/queries/supplement-requests/{request_id}",
                headers={"Cookie": integrator_cookie},
                use_session=False,
            )

        projection_response = integrator_projection(request["request_id"])
        assert projection_response.status == 200, projection_response.text
        projection = projection_response.json()
        assert set(projection) == set(INTEGRATOR_PROJECTION_KEYS)
        assert set(projection["material_requirement"]) == set(
            INTEGRATOR_MATERIAL_KEYS
        )
        assert set(projection["batch"]) == set(INTEGRATOR_BATCH_KEYS)
        assert INTEGRATOR_INTERNAL_KEYS.isdisjoint(projection)
        assert projection["request_id"] == request["request_id"]
        assert projection["context_digest"] == command_view.json()["context_digest"]
        assert projection["status"] == "open"
        assert projection["current"] is True
        assert projection["expected_predecessor_attachment_id"] == command_view.json()[
            "expected_predecessor_attachment_id"
        ]
        assert projection["next_attachment_version"] == 2

        wrong_role = server.request(
            "GET",
            f"/controlled/s02/api/queries/supplement-requests/{request['request_id']}",
            headers=headers("reviewer"),
        )
        assert wrong_role.status == 404
        assert wrong_role.json() == {"detail": {"error": "S02_NOT_FOUND"}}
        unknown = integrator_projection("supplement_request_missing00000000000000000000000")
        assert unknown.status == 404
        assert unknown.json() == wrong_role.json()
        anonymous = server.request(
            "GET",
            f"/controlled/s02/api/queries/supplement-requests/{request['request_id']}",
            use_session=False,
        )
        assert anonymous.status == 404
        assert anonymous.json() == wrong_role.json()

        submission = _attachment_submission_from_projection(
            projection,
            source,
            closed=False,
            batch_id="s06-http-t04-batch",
            stream_id="s06-http-t04-stream",
        )
        progress = server.request(
            "POST",
            "/controlled/s02/api/commands/submit-attachment-version",
            body={
                "idempotency_key": "s06-http-t04-progress",
                "submission": submission,
            },
            headers={"Cookie": integrator_cookie},
            use_session=False,
        )
        assert progress.status == 200, progress.text
        assert progress.json()["request_status"] == "open"
        assert progress.json()["phase"] == "Awaiting Evidence"
        assert set(progress.json()) == {
            "disposition",
            "reason_code",
            "responsible_party",
            "recovery_action",
            "retryable",
            "application_id",
            "receipt_id",
            "job_id",
            "lifecycle_revision",
            "evidence_revision",
            "replayed",
            "envelope_version",
            "schema_version",
            "semantic_version",
            "envelope_id",
            "stream_id",
            "source_revision",
            "source_revision_id",
            "envelope_fingerprint",
            "adapter_id",
            "adapter_version",
            "source_registration_digest",
            "artifact_manifest_digest",
            "fact_counts",
            "gate_results",
            "tenant_id",
            "source_system_id",
            "claim_label",
            "real_cross_document_opportunities",
            "performance_status",
            "request_id",
            "request_status",
            "batch_id",
            "batch_closed",
            "request_progress_revision",
            "attachment_id",
            "attachment_version",
            "supersedes_attachment_id",
            "fulfilled",
            "phase",
            "route",
            "recovery_target",
        }

        after_progress = integrator_projection(request["request_id"])
        assert after_progress.status == 200
        assert after_progress.json()["next_request_progress_revision"] == 2
        assert after_progress.json()["next_source_revision"] == 2
        assert after_progress.json()["expected_predecessor_revision"] == 1
        assert after_progress.json()["batch"]["batch_id"] == "s06-http-t04-batch"

        closed_submission = _attachment_submission_from_projection(
            after_progress.json(),
            source,
            closed=True,
            batch_id="s06-http-t04-batch",
            stream_id="s06-http-t04-stream",
        )
        fulfilled = server.request(
            "POST",
            "/controlled/s02/api/commands/submit-attachment-version",
            body={
                "idempotency_key": "s06-http-t04-closure",
                "submission": closed_submission,
            },
            headers={"Cookie": integrator_cookie},
            use_session=False,
        )
        assert fulfilled.status == 200, fulfilled.text
        assert fulfilled.json()["request_status"] == "fulfilled"
        assert fulfilled.json()["fulfilled"] is True
        terminal = integrator_projection(request["request_id"])
        assert terminal.status == 200
        assert terminal.json()["status"] == "fulfilled"
        assert terminal.json()["current"] is False

        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            history_response = server.request(
                "GET",
                f"/controlled/s01/api/queries/applications/{application_id}/history",
                headers=headers("reviewer"),
            )
            if (
                history_response.status == 200
                and len(history_response.json()["runs"]) == 2
            ):
                break
            time.sleep(0.05)
        history = history_response.json()
        assert [run["current"] for run in history["runs"]] == [False, True]
        assert [item["current"] for item in history["attachment_versions"]] == [
            False,
            True,
        ]


def test_t04_s06_operations_are_closed_in_the_openapi_document(
    tmp_path: Path,
) -> None:
    environment, _ = _http_source(tmp_path)
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        spec = server.request("GET", "/openapi.json").json()
    supplement_path = (
        "/controlled/s01/api/commands/review-work-items/"
        "{work_item_id}/supplement"
    )
    assert (
        spec["paths"][supplement_path]["post"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
    )
    assert (
        spec["paths"]["/controlled/s01/api/queries/supplement-requests/{request_id}"][
            "get"
        ]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    )
    assert (
        spec["paths"][
            "/controlled/s02/api/commands/submit-attachment-version"
        ]["post"]["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ]
    )
    assert (
        spec["paths"][
            "/controlled/s02/api/queries/supplement-requests/{request_id}"
        ]["get"]["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ]
    )
    integrator_schema = spec["components"]["schemas"]["S01IntegratorSupplementRequestView"]
    assert integrator_schema["additionalProperties"] is False
    assert set(integrator_schema["properties"]) == set(INTEGRATOR_PROJECTION_KEYS)
    receipt_schema = spec["components"]["schemas"]["S01AttachmentSubmissionResponse"]
    assert receipt_schema["additionalProperties"] is False
    assert "disposition" in receipt_schema["properties"]
    assert "recovery_target" in receipt_schema["properties"]
