"""S04 correction and immutable-history acceptance over a real HTTP connection."""

from __future__ import annotations

import json
from pathlib import Path
import time

from tests.test_s01_http import (
    headers,
    s01_test_loopback,
    submit,
    wait_for_projected_queue_item,
)


def test_correction_rerun_history_and_current_route_over_http(tmp_path: Path) -> None:
    state_path = tmp_path / "target.sqlite3"
    with s01_test_loopback({"TASK4_S01_STATE_PATH": str(state_path)}) as server:
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
                "raw": "S2ENG54A",
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
    assert after_rejection == before_rejection
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
