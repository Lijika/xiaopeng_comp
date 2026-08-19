"""S10 page-membership correction over a real HTTP loopback connection.

The Reviewer accepts or explicitly unassigns a page membership; the command is
the highest public seam (browser -> HTTP -> domain -> SQLite -> worker ->
current-run CAS -> route).  The old run stays immutable; the route changes only
after one fresh complete run wins current-run CAS.
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

from tests.test_s01_http import (
    headers,
    s01_fault_test_loopback,
    s01_test_loopback,
    wait_for_projected_queue_item,
)

S10_SCENARIO = "app_s10_ambiguous_membership.json"
PAGE1 = "1010101010101010101010101010101010101010101010101010101010101010"
PAGE2 = "2020202020202020202020202020202020202020202020202020202020202020"


def create_s10_react_test_app() -> Any:
    """The S02 test app plus the explicit S01 worker test driver.

    ``create_s02_test_app`` leaves ``S01_TEST_DRIVER`` unset, so the
    ``/controlled/s01/api/_test/commands/process`` endpoint would 404.  The
    production browser flow needs that boundary to advance each run
    deterministically while the S01 background runtime stays disabled.
    """
    import task4_consistency.web.app as web
    from task4_consistency.controlled.s01 import ControlledScenarioTestDriver
    from task4_consistency.web.app import create_s02_test_app

    app = create_s02_test_app()
    if web.S01_SERVICE is None:
        raise RuntimeError("S02 test app did not configure the S01 service")
    # The S02 factory wires the S01 background from its own env flag; the
    # production browser flow needs the background disabled so every worker
    # transition is driven explicitly through /_test/commands/process.
    web.S01_BACKGROUND_ENABLED = False
    web.S01_TEST_DRIVER = ControlledScenarioTestDriver(web.S01_SERVICE)
    return app


def _s10_loopback(state_path: Path):
    return s01_test_loopback(
        {
            "TASK4_S01_STATE_PATH": str(state_path),
            "TASK4_S01_TEST_STATE_PATH": str(state_path),
            "TASK4_S01_TEST_SCENARIO_ID": S10_SCENARIO,
        }
    )


def _submit(server, key: str = "s10-http-intake"):
    server.open_s01_session()
    return server.request(
        "POST",
        "/controlled/s01/api/commands/submit",
        body={"scenario_id": S10_SCENARIO, "idempotency_key": key},
        headers=headers("integrator"),
    )


def _accept_command(
    application_id: str, work_item, claimed, finding, idempotency_key: str = "s10-http-accept"
) -> dict:
    membership = finding["membership"]
    candidate = membership["candidates"][0]
    return {
        "application_id": application_id,
        "expected_fence": claimed["claim_fence"],
        "expected_context": work_item["command_context"],
        "idempotency_key": idempotency_key,
        "membership": {
            "schema_version": "page-membership-correction/2",
            "finding_id": finding["finding_id"],
            "candidate_claim_id": candidate["claim_id"],
            "attachment_id": membership["attachment_id"],
            "page_source_sha256": membership["page_source_sha256"],
            "page_ordinal": membership["page_ordinal"],
            "source_evidence": membership["source_evidence"],
            "expected_active_decision_ids": membership["active_decision_ids"],
            "decision": "accept",
            "document_instance_id": candidate["document_instance_id"],
            "document_role": candidate["document_role"],
            "reason_code": "MEMBERSHIP_SOURCE_VERIFIED",
        },
    }


def _unassign_command(application_id: str, work_item, claimed, finding) -> dict:
    membership = finding["membership"]
    return {
        "application_id": application_id,
        "expected_fence": claimed["claim_fence"],
        "expected_context": work_item["command_context"],
        "idempotency_key": "s10-http-unassign",
        "membership": {
            "schema_version": "page-membership-correction/2",
            "finding_id": finding["finding_id"],
            "candidate_claim_id": membership["candidates"][0]["claim_id"],
            "attachment_id": membership["attachment_id"],
            "page_source_sha256": membership["page_source_sha256"],
            "page_ordinal": membership["page_ordinal"],
            "source_evidence": membership["source_evidence"],
            "expected_active_decision_ids": membership["active_decision_ids"],
            "decision": "unassign",
            "reason_code": "MEMBERSHIP_PAGE_UNASSIGNED",
        },
    }


def _wait_for_run_count(server, application_id: str, run_count: int) -> None:
    deadline = time.monotonic() + 10
    while True:
        history = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/history",
            headers=headers("reviewer"),
        )
        if history.status == 200 and len(history.json()["runs"]) >= run_count:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                {
                    "history_status": history.status,
                    "run_count": (
                        len(history.json()["runs"])
                        if history.status == 200
                        else None
                    ),
                    "expected": run_count,
                }
            )
        time.sleep(0.05)


def test_membership_successor_requires_fresh_current_run(tmp_path) -> None:
    """A membership correction succeeds only when issued against the current
    complete run; the old run stays immutable in history and the route changes
    only after one fresh complete run wins current-run CAS.  A second command
    against the stale pre-acceptance context conflicts and creates no second
    successor.  The worker is driven manually so the pending window is
    deterministic."""
    state_path = tmp_path / "target.sqlite3"
    loopback = s01_fault_test_loopback(
        {
            "TASK4_S01_STATE_PATH": str(state_path),
            "TASK4_S01_TEST_STATE_PATH": str(state_path),
            "TASK4_S01_TEST_SCENARIO_ID": S10_SCENARIO,
        }
    )
    with loopback as server:
        admission = _submit(server).json()
        application_id = admission["application_id"]

        def run_worker(now: int) -> str:
            result = server.request(
                "POST",
                "/controlled/s01/api/_test/commands/process",
                body={"worker_id": "s10-http-worker", "now": now},
                use_session=False,
            )
            assert result.status == 200, result.text
            return result.json()["run_id"]

        run1 = run_worker(100)
        project = server.request(
            "POST",
            "/controlled/s01/api/_test/commands/project",
            use_session=False,
        )
        assert project.status == 200, project.text
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
        ambiguous = next(
            candidate
            for candidate in workspace["mandatory_blockers"]
            if candidate["rule_id"] == "MEMBERSHIP_AMBIGUOUS"
        )
        before = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/current-route",
            headers=headers("reviewer"),
        ).json()
        old_run_id = work_item["run_authority"]["run_id"]
        assert before["route"] == "manual_review"
        assert before["current_run_id"] == old_run_id

        accepted = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{work_item_id}/correct-page-membership",
            body=_accept_command(application_id, work_item, claimed, ambiguous),
            headers=headers("reviewer"),
        )
        assert accepted.status == 200
        payload = accepted.json()
        assert payload["status"] == "accepted"
        assert payload["evidence_revision"] == 2
        assert payload["route"] == "pending_check"
        assert payload["cycle"] == 1

        # While the replacement run is pending the stale old result must not be
        # re-issued: no current run, route held pending.
        pending = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/current-route",
            headers=headers("reviewer"),
        ).json()
        assert pending["route"] == "pending_check"
        assert pending["current_run_id"] is None
        assert pending["evidence_revision"] == 2

        # A second correction against the stale (pre-acceptance) context is a
        # conflict with no second successor.
        stale = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{work_item_id}/correct-page-membership",
            body=_accept_command(
                application_id,
                work_item,
                claimed,
                ambiguous,
                idempotency_key="s10-http-accept-stale-attempt",
            ),
            headers=headers("reviewer"),
        )
        assert stale.status == 409
        assert stale.json()["detail"]["error"] == "S03_STALE"
        unchanged = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/current-route",
            headers=headers("reviewer"),
        ).json()
        assert unchanged == pending

        # The replacement run completes and wins current-run CAS.
        run2 = run_worker(200)
        assert run2 != run1
        project = server.request(
            "POST",
            "/controlled/s01/api/_test/commands/project",
            use_session=False,
        )
        assert project.status == 200, project.text
        current = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/current-route",
            headers=headers("reviewer"),
        ).json()
        assert current["evidence_revision"] == 2
        assert current["route"] == "manual_review"
        assert current["current_run_id"] != old_run_id
        history = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/history",
            headers=headers("reviewer"),
        ).json()
        run_ids = [record["run_id"] for record in history["runs"]]
        assert old_run_id in run_ids  # old run retained immutable
        assert len(history["membership_history"]) == 1
        assert len(history["memberships"]) == 4
        decision = next(
            record
            for record in history["memberships"]
            if record["record_kind"] == "accepted"
        )
        assert decision["cycle"] == 1
        assert history["membership_history"][0]["cycle"] == 1
        assert {record["record_kind"] for record in history["memberships"]} == {
            "candidate",
            "accepted",
        }


def test_membership_unassign_over_http_then_auto_complete(tmp_path) -> None:
    """Unassign the ambiguous page, rerun, then unassign the last unresolved
    page, rerun, and reach automatic completion with both runs retained."""
    state_path = tmp_path / "target.sqlite3"
    with _s10_loopback(state_path) as server:
        admission = _submit(server, key="s10-http-intake-2").json()
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
        ambiguous = next(
            candidate
            for candidate in workspace["mandatory_blockers"]
            if candidate["rule_id"] == "MEMBERSHIP_AMBIGUOUS"
        )
        unassigned = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{work_item_id}/correct-page-membership",
            body=_unassign_command(application_id, work_item, claimed, ambiguous),
            headers=headers("reviewer"),
        )
        assert unassigned.status == 200
        assert unassigned.json()["decision"] == "unassign"
        _wait_for_run_count(server, application_id, run_count=2)

        # The only remaining membership blocker is the unresolved page.
        item2 = server.request(
            "GET",
            "/controlled/s01/api/queries/queue",
            headers=headers("reviewer"),
        ).json()["items"][0]
        assert item2["application_id"] == application_id
        workspace2 = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/workspace",
            headers=headers("reviewer"),
        ).json()
        unresolved = next(
            candidate
            for candidate in workspace2["mandatory_blockers"]
            if candidate["rule_id"] == "MEMBERSHIP_UNRESOLVED"
        )
        assert unresolved["membership"]["page_source_sha256"] == PAGE2
        work_item2 = server.request(
            "GET",
            f"/controlled/s01/api/queries/review-work-items/{item2['work_item_id']}",
            headers=headers("reviewer"),
        ).json()
        claimed2 = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{item2['work_item_id']}/claim",
            body={"expected_context": work_item2["command_context"]},
            headers=headers("reviewer"),
        ).json()
        unassigned2 = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{item2['work_item_id']}/correct-page-membership",
            body=_unassign_command(
                application_id, work_item2, claimed2, unresolved
            ),
            headers=headers("reviewer"),
        )
        assert unassigned2.status == 200
        _wait_for_run_count(server, application_id, run_count=3)
        current = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/current-route",
            headers=headers("reviewer"),
        ).json()
        assert current["route"] == "auto_complete"
        assert current["evidence_revision"] == 3
        history = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/history",
            headers=headers("reviewer"),
        ).json()
        assert len(history["runs"]) == 3
        assert len(history["membership_history"]) == 2


def test_membership_http_unauthorized_is_sanitized_404(tmp_path) -> None:
    """An unauthenticated Reviewer cannot issue a membership correction: the
    command hides identity and returns the same sanitized 404."""
    state_path = tmp_path / "target.sqlite3"
    with _s10_loopback(state_path) as server:
        admission = _submit(server, key="s10-http-intake-3").json()
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
        ambiguous = next(
            candidate
            for candidate in workspace["mandatory_blockers"]
            if candidate["rule_id"] == "MEMBERSHIP_AMBIGUOUS"
        )
        hidden = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{work_item_id}/correct-page-membership",
            body=_accept_command(application_id, work_item, claimed, ambiguous),
            use_session=False,
        )
        assert hidden.status == 404
        assert hidden.json()["detail"] == {"error": "S03_NOT_FOUND"}


def test_membership_http_openapi_contract_is_closed(tmp_path) -> None:
    """The migrated S10 command is consumable through generated types: it
    declares a required, closed request body and a closed 200 schema."""
    state_path = tmp_path / "openapi.sqlite3"
    with _s10_loopback(state_path) as server:
        document = server.request("GET", "/openapi.json").json()
    operation = document["paths"][
        "/controlled/s01/api/commands/review-work-items/{work_item_id}/"
        "correct-page-membership"
    ]["post"]
    request_body = operation["requestBody"]
    assert request_body["required"] is True
    schema = request_body["content"]["application/json"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "application_id",
        "expected_fence",
        "expected_context",
        "idempotency_key",
        "membership",
    }
    membership = schema["properties"]["membership"]
    assert membership["discriminator"]["propertyName"] == "decision"
    accept, unassign = membership["oneOf"]
    assert accept["additionalProperties"] is False
    assert unassign["additionalProperties"] is False
    common = {
        "schema_version",
        "finding_id",
        "candidate_claim_id",
        "attachment_id",
        "page_source_sha256",
        "page_ordinal",
        "source_evidence",
        "expected_active_decision_ids",
        "decision",
        "reason_code",
    }
    assert set(accept["properties"]) == common | {
        "document_instance_id",
        "document_role",
    }
    assert set(unassign["properties"]) == common
    assert accept["properties"]["reason_code"]["enum"] == [
        "MEMBERSHIP_SOURCE_VERIFIED",
        "MEMBERSHIP_SOURCE_MISASSIGNED",
        "MEMBERSHIP_INSTANCE_WRONG",
    ]
    assert unassign["properties"]["reason_code"]["enum"] == [
        "MEMBERSHIP_SOURCE_VERIFIED",
        "MEMBERSHIP_SOURCE_MISASSIGNED",
        "MEMBERSHIP_PAGE_UNASSIGNED",
    ]
    success = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert "$ref" in success
    assert set(operation["responses"]) == {"200", "404", "409", "413", "422", "503"}

    components = document["components"]["schemas"]
    provenance = components["S01MembershipProvenance"]
    assert provenance["additionalProperties"] is False
    assert set(provenance["properties"]) == {
        "adapter_id",
        "adapter_version",
        "source_filename",
        "source_pointer",
        "fact",
        "page_type",
        "inferred",
    }
    assert {item.get("type") for item in provenance["properties"]["inferred"]["anyOf"]} == {
        "boolean",
        "null",
    }
    candidate = components["S01WorkspaceMembershipCandidate"]
    assert candidate["additionalProperties"] is False
    assert candidate["properties"]["provenance"] == {
        "$ref": "#/components/schemas/S01MembershipProvenance"
    }
    assert components["S01MembershipCandidateDocument"]["additionalProperties"] is False
    assert components["S01MembershipSourceEvidence"]["additionalProperties"] is False
    assert components["S01MembershipDecisionSourceEvidence"]["additionalProperties"] is False
