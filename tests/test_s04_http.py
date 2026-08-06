"""S04 correction and immutable-history acceptance over a real HTTP connection."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
import time

from tests.test_s01_http import (
    UvicornLoopback,
    headers,
    s01_test_loopback,
    submit,
    wait_for_projected_queue_item,
)
from tests.test_s02_http import _configured_http_source, _open_session
from tests.test_s03_http import _ready_review


def test_s01_reveal_and_correction_openapi_contracts_are_closed(
    tmp_path: Path,
) -> None:
    """The migrated S01 commands are consumable through generated types: the
    operations declare a required, closed request body and a closed 200
    schema instead of ``requestBody?: never`` and an open dictionary."""
    state_path = tmp_path / "openapi.sqlite3"
    with s01_test_loopback({"TASK4_S01_STATE_PATH": str(state_path)}) as server:
        document = server.request("GET", "/openapi.json").json()
    reveal = document["paths"][
        "/controlled/s01/api/commands/review-work-items/{work_item_id}/"
        "reveal-field-observation"
    ]["post"]
    correct = document["paths"][
        "/controlled/s01/api/commands/review-work-items/{work_item_id}/"
        "correct-field-observation"
    ]["post"]
    for operation in (reveal, correct):
        request_body = operation["requestBody"]
        assert request_body["required"] is True
        schema = request_body["content"]["application/json"]["schema"]
        assert schema["additionalProperties"] is False
        context = schema["properties"]["expected_context"]
        assert context["additionalProperties"] is False
        assert set(context["properties"]) == {
            "lifecycle_revision",
            "evidence_revision",
            "run_id",
            "projection_watermark",
            "current_context",
        }
        success = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert "$ref" in success
        assert set(operation["responses"]) == {"200", "404", "409", "413", "422", "503"}
        for status in ("404", "409", "413", "503"):
            assert (
                operation["responses"][status]["content"]["application/json"]["schema"][
                    "$ref"
                ]
                == "#/components/schemas/S01ErrorResponse"
            )
    reveal_body = reveal["requestBody"]["content"]["application/json"]["schema"]
    assert set(reveal_body["properties"]) == {
        "application_id",
        "expected_context",
        "expected_fence",
        "idempotency_key",
        "observation_id",
    }
    assert reveal_body["properties"]["expected_fence"]["minimum"] == 1
    correction_body = correct["requestBody"]["content"]["application/json"]["schema"]
    assert set(correction_body["properties"]) == {
        "application_id",
        "correction",
        "expected_context",
        "expected_fence",
        "idempotency_key",
    }
    assert correction_body["properties"]["expected_fence"]["minimum"] == 1
    nested = correction_body["properties"]["correction"]
    assert nested["additionalProperties"] is False
    assert set(nested["properties"]) == {
        "schema_version",
        "finding_id",
        "observation_id",
        "document_id",
        "document_role",
        "field",
        "raw",
        "source_location",
        "reason_code",
    }
    # The transport vocabulary is closed: the domain's registered schema
    # version and the two registered correction reasons are Pydantic
    # literals, so an unsupported value fails validation on the wire.
    assert nested["properties"]["schema_version"]["const"] == (
        "field-observation-correction/1"
    )
    assert nested["properties"]["reason_code"]["enum"] == [
        "SOURCE_VALUE_MISREAD",
        "SOURCE_VALUE_MISSING",
    ]
    source_location = nested["properties"]["source_location"]
    assert source_location["additionalProperties"] is False
    assert set(source_location["properties"]) == {
        "source_sha256",
        "source_page",
        "source_region",
    }
    reveal_result = document["components"]["schemas"]["S01RevealResult"]
    assert reveal_result.get("additionalProperties") is False
    assert set(reveal_result["properties"]) == {
        "status",
        "replayed",
        "application_id",
        "work_item_id",
        "observation_id",
        "source_location",
        "source_text",
        "revealed_at",
    }
    correction_result = document["components"]["schemas"]["S01CorrectionResult"]
    assert correction_result.get("additionalProperties") is False
    assert {
        "correction_id",
        "observation_id",
        "invalidated_run_id",
        "job_id",
        "phase",
        "route",
        "lifecycle_revision",
        "evidence_revision",
    }.issubset(set(correction_result["properties"]))


def test_correction_rerun_history_and_current_route_over_http(tmp_path: Path) -> None:
    state_path = tmp_path / "target.sqlite3"
    with s01_test_loopback(
        {
            "TASK4_S01_STATE_PATH": str(state_path),
            "TASK4_S01_TEST_STATE_PATH": str(state_path),
        }
    ) as server:
        admission = submit(server, "s04-http-intake").json()
        application_id = admission["application_id"]
        item = wait_for_projected_queue_item(server, application_id)
        work_item_id = item["work_item_id"]
        work_item = server.request(
            "GET",
            f"/controlled/s01/api/queries/review-work-items/{work_item_id}",
            headers=headers("reviewer"),
        ).json()
        claimed = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{work_item_id}/claim",
            body={"expected_context": work_item["command_context"]},
            headers=headers("reviewer"),
        ).json()
        workspace = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/workspace",
            headers=headers("reviewer"),
        ).json()
        finding = next(
            candidate
            for candidate in workspace["mandatory_blockers"]
            if candidate["rule_id"] == "R_ENGINE_CROSS"
        )
        source = next(
            link for link in finding["evidence_links"] if link["document_id"] == "inv"
        )
        command = {
            "application_id": application_id,
            "expected_fence": claimed["claim_fence"],
            "expected_context": work_item["command_context"],
            "idempotency_key": "s04-http-correction",
            "correction": {
                "schema_version": "field-observation-correction/1",
                "finding_id": finding["finding_id"],
                "observation_id": source["observation_id"],
                "document_id": source["document_id"],
                "document_role": source["document_role"],
                "field": source["field"],
                # The raw evidence crosses the boundary byte-for-byte: the
                # leading/trailing whitespace is part of the entered value and
                # must be accepted and persisted verbatim by the authority.
                "raw": "  S2ENG54A  ",
                "source_location": {
                    key: source[key]
                    for key in ("source_sha256", "source_page", "source_region")
                },
                "reason_code": "SOURCE_VALUE_MISREAD",
            },
        }
        before_rejection = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/current-route",
            headers=headers("reviewer"),
        ).json()
        hidden = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{work_item_id}/correct-field-observation",
            body=command,
            use_session=False,
        )
        hidden_reveal = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{work_item_id}/reveal-field-observation",
            body={
                "application_id": application_id,
                "observation_id": source["observation_id"],
                "expected_fence": claimed["claim_fence"],
                "expected_context": work_item["command_context"],
                "idempotency_key": "s04-http-hidden-reveal",
            },
            use_session=False,
        )
        malformed = json.loads(json.dumps(command))
        malformed["correction"]["unexpected"] = "RAW-CORRECTION-SENTINEL"
        invalid = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{work_item_id}/correct-field-observation",
            body=malformed,
            headers=headers("reviewer"),
        )
        # The transport vocabulary is closed: an unregistered reason code is
        # rejected by the Pydantic literal before it reaches the domain.
        unsupported_reason = json.loads(json.dumps(command))
        unsupported_reason["correction"]["reason_code"] = "SOURCE_VALUE_EDITED"
        unsupported = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{work_item_id}/correct-field-observation",
            body=unsupported_reason,
            headers=headers("reviewer"),
        )
        after_rejection = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/current-route",
            headers=headers("reviewer"),
        ).json()
        corrected = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{work_item_id}/correct-field-observation",
            body=command,
            headers=headers("reviewer"),
        )
        assert corrected.status == 200, corrected.text
        pending = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/current-route",
            headers=headers("reviewer"),
        )
        deadline = time.monotonic() + 8
        while True:
            history = server.request(
                "GET",
                f"/controlled/s01/api/queries/applications/{application_id}/history",
                headers=headers("reviewer"),
            )
            if history.status == 200 and len(history.json()["runs"]) == 2:
                break
            if time.monotonic() >= deadline:
                raise AssertionError((history.status, history.text))
            time.sleep(0.05)
        current = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/current-route",
            headers=headers("reviewer"),
        )

    assert hidden.status == 404
    assert hidden.json()["detail"] == {"error": "S03_NOT_FOUND"}
    assert hidden_reveal.status == 404
    assert hidden_reveal.json()["detail"] == {"error": "S03_NOT_FOUND"}
    assert invalid.status == 422
    assert invalid.json()["detail"]["error"] == "S03_INVALID_COMMAND"
    assert "RAW-CORRECTION-SENTINEL" not in invalid.text
    assert unsupported.status == 422
    assert unsupported.json()["detail"]["error"] == "S03_INVALID_COMMAND"
    assert after_rejection == before_rejection
    # The authoritative stored successor preserves the correction raw and
    # its lexeme byte-for-byte (leading/trailing whitespace included): only
    # the adapter/client boundary may alter the value, never the authority.
    connection = sqlite3.connect(state_path)
    stored = connection.execute(
        "SELECT payload FROM evidence_events"
    ).fetchall()
    connection.close()
    successors = []
    for (payload,) in stored:
        event = json.loads(payload)
        if event.get("kind") != "field_correction":
            continue
        successor_id = event["payload"]["correction"]["observation_id"]
        for document in event["payload"]["evidence"]:
            for observation in document.get("observations", []):
                if observation.get("observation_id") == successor_id:
                    successors.append(observation)
    assert len(successors) == 1
    assert successors[0]["raw"] == "  S2ENG54A  "
    assert successors[0]["raw_lexeme"] == "  S2ENG54A  "
    assert corrected.status == 200
    assert corrected.json()["status"] == "accepted"
    assert pending.status == 200
    assert corrected.json()["route"] == "pending_check"
    assert history.status == current.status == 200
    assert [run["current"] for run in history.json()["runs"]] == [False, True]
    assert history.json()["runs"][0]["run_id"] == work_item["run_authority"]["run_id"]
    assert current.json()["current_run_id"] == history.json()["runs"][1]["run_id"]
    assert current.json()["route"] == "auto_complete"
    public = json.dumps(
        [corrected.json(), pending.json(), history.json(), current.json()],
        ensure_ascii=False,
    )
    assert "S2ENG54Z" not in public
    assert "S2ENG54A" not in public


def test_registered_source_correction_reruns_to_fresh_review_work_over_http(
    tmp_path: Path,
) -> None:
    environment, submission = _configured_http_source(tmp_path)
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        cookie = _open_session(server)
        request_headers = {"Cookie": cookie}
        application_id, queue_item = _ready_review(server, submission, cookie)
        work_item_id = queue_item["work_item_id"]
        work_item = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=request_headers,
            use_session=False,
        ).json()
        claimed = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/claim",
            body={"expected_context": work_item["command_context"]},
            headers=request_headers,
            use_session=False,
        ).json()
        workspace = server.request(
            "GET",
            f"/controlled/s02/api/queries/applications/{application_id}/workspace",
            headers=request_headers,
            use_session=False,
        ).json()
        finding = workspace["selected_finding"]
        source = finding["evidence_links"][0]

        corrected = server.request(
            "POST",
            f"/controlled/s02/api/commands/review-work-items/{work_item_id}/correct-field-observation",
            body={
                "application_id": application_id,
                "expected_fence": claimed["claim_fence"],
                "expected_context": work_item["command_context"],
                "idempotency_key": "s04-http-registered-correction",
                "correction": {
                    "schema_version": "field-observation-correction/1",
                    "finding_id": finding["finding_id"],
                    "observation_id": source["observation_id"],
                    "document_id": source["document_id"],
                    "document_role": source["document_role"],
                    "field": source["field"],
                    "raw": "SAFE-VIN-B",
                    "source_location": {
                        key: source[key]
                        for key in (
                            "source_sha256",
                            "source_page",
                            "source_region",
                        )
                    },
                    "reason_code": "SOURCE_VALUE_MISREAD",
                },
            },
            headers=request_headers,
            use_session=False,
        )
        assert corrected.status == 200, corrected.text
        old_context = server.request(
            "GET",
            f"/controlled/s02/api/queries/review-work-items/{work_item_id}",
            headers=request_headers,
            use_session=False,
        )

        deadline = time.monotonic() + 8
        while True:
            history = server.request(
                "GET",
                f"/controlled/s02/api/queries/applications/{application_id}/history",
                headers=request_headers,
                use_session=False,
            )
            current = server.request(
                "GET",
                f"/controlled/s02/api/queries/applications/{application_id}/current-route",
                headers=request_headers,
                use_session=False,
            )
            queue = server.request(
                "GET",
                "/controlled/s02/api/queries/queue",
                headers=request_headers,
                use_session=False,
            )
            fresh_item = next(
                (
                    item
                    for item in queue.json()["items"]
                    if item["application_id"] == application_id
                    and item["work_item_id"] != work_item_id
                ),
                None,
            )
            if len(history.json()["runs"]) == 2 and fresh_item is not None:
                break
            if time.monotonic() >= deadline:
                raise AssertionError((history.text, current.text, queue.text))
            time.sleep(0.05)
        fresh_workspace = server.request(
            "GET",
            f"/controlled/s02/api/queries/applications/{application_id}/workspace",
            headers=request_headers,
            use_session=False,
        )

    assert corrected.json()["status"] == "accepted"
    assert corrected.json()["route"] == "pending_check"
    assert old_context.json()["status"] == "invalidated"
    assert [run["current"] for run in history.json()["runs"]] == [False, True]
    assert current.json()["route"] == "manual_review"
    assert current.json()["current_run_id"] == history.json()["runs"][1]["run_id"]
    assert fresh_item["route"] == "manual_review"
    assert [
        link["observation_id"]
        for link in fresh_workspace.json()["selected_finding"]["evidence_links"]
    ] == [corrected.json()["observation_id"]]
    public = json.dumps(
        [
            corrected.json(),
            old_context.json(),
            history.json(),
            current.json(),
            queue.json(),
            fresh_workspace.json(),
        ],
        ensure_ascii=False,
    )
    assert "SAFE-VIN-A" not in public
    assert "SAFE-VIN-B" not in public
