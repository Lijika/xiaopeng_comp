"""S11 application-local entity-link correction over a real HTTP loopback
connection.

The Reviewer accepts an explicit application-local entity link for an
unresolved, ambiguous or conflicting mention; the command is the highest
public seam (browser -> HTTP -> domain -> SQLite -> worker -> current-run CAS
-> route).  The old run stays immutable; the route changes only after one
fresh complete run wins current-run CAS.  No candidate auto-links.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from tests.test_s01_http import (
    headers,
    s01_fault_test_loopback,
    s01_test_loopback,
    wait_for_projected_queue_item,
)

S11_SCENARIO = "app_s11_entity_ambiguity.json"
MENTION_ORG = "s11_mention_org_pol"
MENTION_CITY = "s11_mention_city_lease"
MENTION_BRAND = "s11_mention_brand_inv"
ENTITY_LINK_RULE_IDS = {
    "ENTITY_LINK_UNRESOLVED",
    "ENTITY_LINK_AMBIGUOUS",
    "ENTITY_LINK_CONFLICT",
}


def _s11_loopback(state_path: Path):
    return s01_test_loopback(
        {
            "TASK4_S01_STATE_PATH": str(state_path),
            "TASK4_S01_TEST_STATE_PATH": str(state_path),
            "TASK4_S01_TEST_SCENARIO_ID": S11_SCENARIO,
        }
    )


def _submit(server, key: str = "s11-http-intake"):
    server.open_s01_session()
    return server.request(
        "POST",
        "/controlled/s01/api/commands/submit",
        body={"scenario_id": S11_SCENARIO, "idempotency_key": key},
        headers=headers("integrator"),
    )


def _accept_command(
    application_id: str,
    work_item,
    claimed,
    finding,
    *,
    entity_id: str,
    idempotency_key: str = "s11-http-accept",
) -> dict:
    entity_link = finding["entity_link"]
    candidate = next(
        candidate
        for candidate in entity_link["candidates"]
        if candidate["entity_id"] == entity_id
    )
    provenance = candidate["provenance"]
    return {
        "application_id": application_id,
        "expected_fence": claimed["claim_fence"],
        "expected_context": work_item["command_context"],
        "idempotency_key": idempotency_key,
        "entity_link": {
            "schema_version": "entity-link-correction/1",
            "finding_id": finding["finding_id"],
            "candidate_claim_id": candidate["claim_id"],
            "mention_id": entity_link["mention_id"],
            "source_evidence": entity_link["source_evidence"],
            "expected_active_decision_ids": entity_link[
                "active_decision_ids"
            ],
            "decision": "accept",
            "entity_id": candidate["entity_id"],
            "entity_type": candidate["entity_type"],
            "label": candidate["label"],
            "relationship": "same_as",
            "matcher_id": provenance["matcher_id"],
            "matcher_version": provenance["matcher_version"],
            "knowledge_release_id": provenance["knowledge_release_id"],
            "reason_code": "ENTITY_LINK_SOURCE_VERIFIED",
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


def test_entity_link_successor_requires_fresh_current_run(tmp_path) -> None:
    """An entity-link correction succeeds only when issued against the current
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
            "TASK4_S01_TEST_SCENARIO_ID": S11_SCENARIO,
        }
    )
    with loopback as server:
        admission = _submit(server).json()
        application_id = admission["application_id"]

        def run_worker(now: int) -> str:
            result = server.request(
                "POST",
                "/controlled/s01/api/_test/commands/process",
                body={"worker_id": "s11-http-worker", "now": now},
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
            if candidate["rule_id"] == "ENTITY_LINK_AMBIGUOUS"
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
            f"/controlled/s01/api/commands/review-work-items/{work_item_id}/correct-entity-link",
            body=_accept_command(
                application_id,
                work_item,
                claimed,
                ambiguous,
                entity_id="org:picc_full",
            ),
            headers=headers("reviewer"),
        )
        assert accepted.status == 200
        payload = accepted.json()
        assert payload["status"] == "accepted"
        assert payload["evidence_revision"] == 2
        assert payload["route"] == "pending_check"
        assert payload["cycle"] == 1
        assert payload["entity_link_decision_id"]

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
            f"/controlled/s01/api/commands/review-work-items/{work_item_id}/correct-entity-link",
            body=_accept_command(
                application_id,
                work_item,
                claimed,
                ambiguous,
                entity_id="org:pingan_full",
                idempotency_key="s11-http-accept-stale-attempt",
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
        assert len(history["entity_link_history"]) == 1
        decision = next(
            record
            for record in history["entity_links"]
            if record["record_kind"] == "accepted"
        )
        assert decision["cycle"] == 1
        assert history["entity_link_history"][0]["cycle"] == 1
        assert {record["record_kind"] for record in history["entity_links"]} == {
            "candidate",
            "accepted",
        }


def test_entity_link_resolution_sequence_to_auto_complete(tmp_path) -> None:
    """Resolve the ambiguous org, the unresolved low-confidence city and the
    conflicting brand mentions over HTTP, rerun after each, and reach
    automatic completion with every run retained immutable."""
    state_path = tmp_path / "target.sqlite3"
    with _s11_loopback(state_path) as server:
        admission = _submit(server, key="s11-http-intake-2").json()
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
            if candidate["rule_id"] == "ENTITY_LINK_AMBIGUOUS"
        )
        assert set(
            candidate["entity_id"]
            for candidate in ambiguous["entity_link"]["candidates"]
        ) == {"org:picc_full", "org:pingan_full"}
        accepted = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{work_item_id}/correct-entity-link",
            body=_accept_command(
                application_id,
                work_item,
                claimed,
                ambiguous,
                entity_id="org:picc_full",
                idempotency_key="s11-http-accept",
            ),
            headers=headers("reviewer"),
        )
        assert accepted.status == 200
        _wait_for_run_count(server, application_id, run_count=2)

        workspace2 = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/workspace",
            headers=headers("reviewer"),
        ).json()
        unresolved = next(
            candidate
            for candidate in workspace2["mandatory_blockers"]
            if candidate["rule_id"] == "ENTITY_LINK_UNRESOLVED"
        )
        assert unresolved["entity_link"]["mention_id"] == MENTION_CITY
        assert unresolved["entity_link"]["low_confidence"] is True
        item2 = server.request(
            "GET",
            "/controlled/s01/api/queries/queue",
            headers=headers("reviewer"),
        ).json()["items"][0]
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
        accepted2 = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{item2['work_item_id']}/correct-entity-link",
            body=_accept_command(
                application_id,
                work_item2,
                claimed2,
                unresolved,
                entity_id="addr:nanjing",
                idempotency_key="s11-http-city-accept",
            ),
            headers=headers("reviewer"),
        )
        assert accepted2.status == 200
        _wait_for_run_count(server, application_id, run_count=3)

        workspace3 = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/workspace",
            headers=headers("reviewer"),
        ).json()
        conflict = next(
            candidate
            for candidate in workspace3["mandatory_blockers"]
            if candidate["rule_id"] == "ENTITY_LINK_CONFLICT"
        )
        assert conflict["entity_link"]["mention_id"] == MENTION_BRAND
        assert {
            candidate["entity_id"]
            for candidate in conflict["entity_link"]["candidates"]
        } == {"brand:faw-vw", "brand:saic-vw"}
        item3 = server.request(
            "GET",
            "/controlled/s01/api/queries/queue",
            headers=headers("reviewer"),
        ).json()["items"][0]
        work_item3 = server.request(
            "GET",
            f"/controlled/s01/api/queries/review-work-items/{item3['work_item_id']}",
            headers=headers("reviewer"),
        ).json()
        claimed3 = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{item3['work_item_id']}/claim",
            body={"expected_context": work_item3["command_context"]},
            headers=headers("reviewer"),
        ).json()
        accepted3 = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{item3['work_item_id']}/correct-entity-link",
            body=_accept_command(
                application_id,
                work_item3,
                claimed3,
                conflict,
                entity_id="brand:faw-vw",
                idempotency_key="s11-http-brand-accept",
            ),
            headers=headers("reviewer"),
        )
        assert accepted3.status == 200
        _wait_for_run_count(server, application_id, run_count=4)

        current = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/current-route",
            headers=headers("reviewer"),
        ).json()
        assert current["route"] == "auto_complete"
        assert current["evidence_revision"] == 4
        history = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/history",
            headers=headers("reviewer"),
        ).json()
        assert len(history["runs"]) == 4
        assert len(history["entity_link_history"]) == 3
        assert len(history["entity_links"]) == 8  # 5 candidates + 3 decisions


def test_entity_link_http_unauthorized_is_sanitized_404(tmp_path) -> None:
    """An unauthenticated Reviewer cannot issue an entity-link correction: the
    command hides identity and returns the same sanitized 404."""
    state_path = tmp_path / "target.sqlite3"
    with _s11_loopback(state_path) as server:
        admission = _submit(server, key="s11-http-intake-3").json()
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
            if candidate["rule_id"] == "ENTITY_LINK_AMBIGUOUS"
        )
        hidden = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{work_item_id}/correct-entity-link",
            body=_accept_command(
                application_id,
                work_item,
                claimed,
                ambiguous,
                entity_id="org:picc_full",
            ),
            use_session=False,
        )
        assert hidden.status == 404
        assert hidden.json()["detail"] == {"error": "S03_NOT_FOUND"}


def test_entity_link_http_openapi_contract_is_closed(tmp_path) -> None:
    """The S11 command is consumable through generated types: it declares a
    required, closed request body and a closed 200 schema."""
    state_path = tmp_path / "openapi.sqlite3"
    with _s11_loopback(state_path) as server:
        document = server.request("GET", "/openapi.json").json()
    operation = document["paths"][
        "/controlled/s01/api/commands/review-work-items/{work_item_id}/"
        "correct-entity-link"
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
        "entity_link",
    }
    entity_link = schema["properties"]["entity_link"]
    assert entity_link["discriminator"]["propertyName"] == "decision"
    accept = entity_link["oneOf"][0]
    assert accept["additionalProperties"] is False
    assert set(accept["properties"]) == {
        "schema_version",
        "finding_id",
        "candidate_claim_id",
        "mention_id",
        "source_evidence",
        "expected_active_decision_ids",
        "decision",
        "entity_id",
        "entity_type",
        "label",
        "relationship",
        "matcher_id",
        "matcher_version",
        "knowledge_release_id",
        "reason_code",
    }
    assert (
        accept["properties"]["relationship"].get("enum")
        == ["same_as"]
        or accept["properties"]["relationship"].get("const")
        == "same_as"
    )
    assert accept["properties"]["reason_code"]["enum"] == [
        "ENTITY_LINK_SOURCE_VERIFIED",
        "ENTITY_LINK_SOURCE_MISASSIGNED",
        "ENTITY_LINK_AMBIGUITY_RESOLVED",
    ]
    success = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert "$ref" in success
    assert set(operation["responses"]) == {"200", "404", "409", "413", "422", "503"}

    components = document["components"]["schemas"]
    mention = components["S01EntityLinkMention"]
    assert mention["additionalProperties"] is False
    assert set(mention["properties"]) == {
        "mention_id",
        "entity_type",
        "document_id",
        "document_role",
        "field",
        "raw",
    }
    candidate_entity = components["S01EntityLinkCandidateEntity"]
    assert candidate_entity["additionalProperties"] is False
    assert set(candidate_entity["properties"]) == {
        "entity_id",
        "entity_type",
        "label",
    }
    provenance = components["S01EntityLinkProvenance"]
    assert provenance["additionalProperties"] is False
    assert set(provenance["properties"]) == {
        "matcher_id",
        "matcher_version",
        "knowledge_release_id",
        "method",
        "source_pointer",
    }
    knowledge = components["S01EntityLinkKnowledge"]
    assert knowledge["additionalProperties"] is False
    assert set(knowledge["properties"]) == {"same_as", "conflict_with"}
    workspace_entity_link = components["S01WorkspaceEntityLink"]
    assert set(workspace_entity_link["properties"]) == {
        "mention_id",
        "mention",
        "state",
        "candidates",
        "active_decision_ids",
        "source_evidence",
        "low_confidence",
    }
    assert workspace_entity_link["properties"]["state"]["enum"] == [
        "unresolved",
        "ambiguous",
        "conflict",
    ]
