"""T01 focused HTTP contract tests for the migrated frontend seams (#35).

These tests are the Slice-1 red/green driver:

- the public Reviewer queue gains a typed, additive, server-owned
  ``recovery_items`` collection while ``items`` keeps its exact meaning and
  fields (backward compatibility);
- the migrated seams (queue, Recovery Work detail, VerifyRecovery success and
  errors, current route) expose explicit typed OpenAPI DTOs with no remaining
  ``additionalProperties: true`` success responses;
- Reviewer-only current-route and Operator-only VerifyRecovery stay
  existence-hiding across wrong role, scope, and nonexistent work;
- an accepted VerifyRecovery produces exactly one business effect.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from task4_consistency.controlled.s01 import ControlledScenarioService

from tests.test_s01_http import (
    UvicornLoopback,
    demo_auth_headers,
    headers,
    operator_auth_headers,
    s01_fault_test_loopback,
    submit,
)
from tests.test_s07_http import (
    _environment,
    _recovery_path,
    _verify_path,
    create_s07_test_app,
)


ROOT = Path(__file__).resolve().parents[1]
APP_FACTORY = "tests.test_s07_http:create_s07_test_app"
SCENARIO = "app_r53_bad_engine.json"


def create_t01_test_app() -> Any:
    """S07-style test app that also lets T01 pin the React shell directory.

    The React shell directory override keeps the missing-build contract test
    independent of whether a production build exists in the workspace; the
    default app factory (tests.test_s07_http:create_s07_test_app) serves the
    real committed build.
    """
    import task4_consistency.web.app as web

    web.S01_BACKGROUND_ENABLED = False
    web.S01_REQUIRE_CONFIGURED_STARTUP = False
    web.S01_SERVICE = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=Path(os.environ["TASK4_S01_TEST_STATE_PATH"]),
        recovery_verifier=None,
        worker_identity="t01-http-worker",
    )
    web.S01_TEST_DRIVER = None
    react_dir = os.environ.get("TASK4_S01_TEST_REACT_DIR", "").strip()
    if react_dir:
        web.S01_REACT_INDEX = Path(react_dir).resolve() / "index.html"
    return web.app

MANUAL_ITEM_FIELDS = {
    "application_id",
    "work_item_id",
    "assigned_subject",
    "claim_fence",
    "claim_expires_at",
    "phase",
    "route",
    "evidence_ready",
    "mandatory_blockers",
    "lifecycle_revision",
    "evidence_revision",
    "projection_watermark",
}
RECOVERY_ITEM_FIELDS = {
    "recovery_work_id",
    "application_id",
    "status",
    "phase",
    "primary_reason_code",
    "responsible_party",
    "lifecycle_revision",
    "projection_watermark",
}


def _restricted_strings() -> list[str]:
    fixture = json.loads(
        (ROOT / "fixtures" / "applications" / SCENARIO).read_text(encoding="utf-8")
    )
    values = [str(fixture["application_id"])]
    for document in fixture["documents"]:
        for field in document["fields"].values():
            if isinstance(field.get("raw"), str) and len(field["raw"]) > 1:
                values.append(field["raw"])
    return values


def _admit_and_block(
    server: UvicornLoopback, key: str, *, now: int = 10
) -> dict[str, Any]:
    """Submit one scenario and force the first worker call to fail closed.

    The first ``process`` call on a fresh S07 test server always injects the
    checker-incompatibility fault, producing an ``Unprocessable`` application
    with one open Recovery Work item.
    """
    admission = submit(server, key).json()
    failed = server.request(
        "POST",
        "/controlled/s01/api/_test/commands/process",
        body={"worker_id": f"t01-worker-{key}", "now": now},
        use_session=False,
    )
    assert failed.status == 200, failed.text
    assert failed.json()["status"] == "blocked", failed.text
    return {
        "application_id": admission["application_id"],
        "recovery_work_id": failed.json()["recovery_work_id"],
        "job_id": failed.json()["job_id"],
        "fence": failed.json()["fence"],
    }


def _reviewer_queue(server: UvicornLoopback) -> dict[str, Any]:
    response = server.request(
        "GET",
        "/controlled/s01/api/queries/queue",
        headers=headers("reviewer"),
    )
    assert response.status == 200, response.text
    return response.json()


def test_queue_recovery_items_are_additive_typed_and_minimized(tmp_path: Path) -> None:
    state_path = tmp_path / "t01-queue.sqlite3"
    with UvicornLoopback(
        _environment(state_path, "verified"),
        app_target=APP_FACTORY,
        app_factory=True,
    ) as server:
        blocked = _admit_and_block(server, "t01-queue-a", now=10)
        queue = _reviewer_queue(server)

        assert set(queue) == {"items", "recovery_items", "projection_watermark"}
        assert queue["items"] == []
        assert [item["recovery_work_id"] for item in queue["recovery_items"]] == [
            blocked["recovery_work_id"]
        ]
        assert all(
            set(item) == RECOVERY_ITEM_FIELDS for item in queue["recovery_items"]
        )
        recovery_item = queue["recovery_items"][0]
        assert recovery_item["application_id"] == blocked["application_id"]
        assert recovery_item["status"] == "open"
        assert recovery_item["phase"] == "Unprocessable"
        assert recovery_item["primary_reason_code"] == "configuration.checker_unavailable"
        assert recovery_item["responsible_party"] == "policy_owner"
        assert recovery_item["projection_watermark"] == queue["projection_watermark"]

        serialized = json.dumps(recovery_item, ensure_ascii=False, sort_keys=True)
        assert all(value not in serialized for value in _restricted_strings())
        for forbidden in ('"raw"', "run_spec", "evidence_snapshot", "object_ref"):
            assert forbidden not in serialized

        reviewer_view = server.request(
            "GET",
            _recovery_path(blocked["recovery_work_id"]),
            headers=headers("reviewer"),
        ).json()
        assert (
            recovery_item["lifecycle_revision"]
            == reviewer_view["lifecycle_revision"]
        )
        assert reviewer_view["status"] == "open"

    with s01_fault_test_loopback(
        {"TASK4_S01_TEST_STATE_PATH": str(tmp_path / "t01-manual.sqlite3")}
    ) as manual_server:
        admission = submit(manual_server, "t01-manual-compat").json()
        processed = manual_server.request(
            "POST",
            "/controlled/s01/api/_test/commands/process",
            body={"worker_id": "t01-manual-worker", "now": 0},
            use_session=False,
        )
        assert processed.status == 200, processed.text
        projected = manual_server.request(
            "POST",
            "/controlled/s01/api/_test/commands/project",
            body={},
            use_session=False,
        )
        assert projected.json()["updated"] == 1
        queue = _reviewer_queue(manual_server)
        assert queue["recovery_items"] == []
        manual_ids = [item["application_id"] for item in queue["items"]]
        assert manual_ids == [admission["application_id"]]
        assert all(set(item) == MANUAL_ITEM_FIELDS for item in queue["items"])
        manual = queue["items"][0]
        assert manual["phase"] == "Manual Review"
        assert manual["route"] == "manual_review"
        assert manual["evidence_ready"] is True
        assert manual["mandatory_blockers"][0]["rule_id"] == "R_ENGINE_CROSS"


def test_openapi_typed_dto_contract_for_migrated_seams(tmp_path: Path) -> None:
    state_path = tmp_path / "t01-openapi.sqlite3"
    with UvicornLoopback(
        _environment(state_path, "verified"),
        app_target=APP_FACTORY,
        app_factory=True,
    ) as server:
        spec = server.request("GET", "/openapi.json").json()

    components: dict[str, Any] = spec["components"]["schemas"]

    def resolve(schema: Any) -> dict[str, Any]:
        while isinstance(schema, dict) and "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[-1]
            schema = components[name]
        return schema

    def assert_typed(schema: Any) -> dict[str, Any]:
        resolved = resolve(schema)
        assert resolved.get("additionalProperties") is not True, resolved
        assert "properties" in resolved, resolved
        return resolved

    def success_schema(path: str, status: int = 200) -> dict[str, Any]:
        operation = next(
            operation
            for operation in spec["paths"][path].values()
            if isinstance(operation, dict) and "responses" in operation
        )
        schema = (
            operation["responses"]
            .get(str(status), {})
            .get("content", {})
            .get("application/json", {})
            .get("schema")
        )
        assert schema is not None, operation["responses"]
        return assert_typed(schema)

    queue_path = "/controlled/s01/api/queries/queue"
    queue_schema = success_schema(queue_path)
    queue_required = set(queue_schema["required"])
    assert {"items", "recovery_items", "projection_watermark"} <= queue_required
    items_schema = resolve(queue_schema["properties"]["items"])
    assert items_schema["type"] == "array"
    item_schema = assert_typed(items_schema["items"])
    assert set(item_schema["required"]) == MANUAL_ITEM_FIELDS
    recovery_items_schema = resolve(queue_schema["properties"]["recovery_items"])
    assert recovery_items_schema["type"] == "array"
    recovery_item_schema = assert_typed(recovery_items_schema["items"])
    assert set(recovery_item_schema["required"]) == RECOVERY_ITEM_FIELDS

    recovery_path = (
        "/controlled/s01/api/queries/recovery-work-items/{recovery_work_id}"
    )
    recovery_schema = success_schema(recovery_path)
    recovery_required = set(recovery_schema["required"])
    assert {
        "schema_version",
        "recovery_work_id",
        "status",
        "application_id",
        "cycle",
        "phase",
        "route",
        "lifecycle_revision",
        "evidence_revision",
        "primary_reason_code",
        "related_reason_codes",
        "operation",
        "dependency",
        "logical_operation_id",
        "attempts",
        "responsible_party",
        "recovery_action",
        "recovery_target",
        "criterion",
        "retry_policy",
        "outcome_known",
        "retryable",
        "recovery_fact_count",
        "resolution_count",
        "job_status",
        "delivery_semantics",
        "protected_business_revision",
        "projection_watermark",
        "can_verify",
    } <= recovery_required
    assert "current_run_id" in recovery_schema["properties"]
    criterion_schema = assert_typed(recovery_schema["properties"]["criterion"])
    assert {"id", "version", "digest", "evidence_kind", "trusted_verifier"} <= set(
        criterion_schema["required"]
    )
    attempt_schema = assert_typed(
        resolve(recovery_schema["properties"]["attempts"])["items"]
    )
    assert {"attempt", "classification", "status", "started_at"} <= set(
        attempt_schema["required"]
    )

    verify_path = (
        "/controlled/s01/api/commands/recovery-work-items/{recovery_work_id}/verify"
    )
    verify_operation = next(
        operation
        for operation in spec["paths"][verify_path].values()
        if isinstance(operation, dict) and "responses" in operation
    )
    verify_success = assert_typed(
        verify_operation["responses"]["200"]["content"]["application/json"]["schema"]
    )
    assert {
        "status",
        "replayed",
        "recovery_work_id",
        "recovery_fact_id",
        "application_id",
        "phase",
        "lifecycle_revision",
        "evidence_revision",
        "successor_job_id",
        "successor_fence",
    } <= set(verify_success["required"])

    verify_body = components.get("S07VerifyRecoveryBody")
    assert verify_body is not None
    assert verify_body.get("additionalProperties") is False
    assert set(verify_body["required"]) == {
        "expected_lifecycle_revision",
        "expected_criterion_digest",
        "idempotency_key",
    }
    for status in ("404", "409", "422", "503"):
        error_schema = (
            verify_operation["responses"]
            .get(status, {})
            .get("content", {})
            .get("application/json", {})
            .get("schema")
        )
        assert error_schema is not None, f"verify {status} error schema missing"
        error_model = resolve(error_schema)
        assert error_model.get("additionalProperties") is not True
        detail_schema = assert_typed(error_model["properties"]["detail"])
        assert "error" in detail_schema["required"]
        assert "reason_code" in detail_schema["properties"]
        assert "message" in detail_schema["properties"]

    current_route_path = (
        "/controlled/s01/api/queries/applications/{application_id}/current-route"
    )
    current_route_schema = success_schema(current_route_path)
    current_route_required = set(current_route_schema["required"])
    assert {
        "schema_version",
        "application_id",
        "phase",
        "route",
        "cycle",
        "lifecycle_revision",
        "evidence_revision",
        "currentness_reason",
    } <= current_route_required
    for optional in (
        "current_run_id",
        "evidence_snapshot_id",
        "evidence_snapshot_digest",
        "release_id",
        "release_digest",
        "checker_build",
    ):
        assert optional in current_route_schema["properties"]


def test_reviewer_operator_boundaries_accept_once_and_authoritative_gate(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "t01-chain.sqlite3"
    with UvicornLoopback(
        _environment(state_path, "verified"),
        app_target=APP_FACTORY,
        app_factory=True,
    ) as server:
        blocked = _admit_and_block(server, "t01-chain", now=10)

        queue = _reviewer_queue(server)
        assert [item["recovery_work_id"] for item in queue["recovery_items"]] == [
            blocked["recovery_work_id"]
        ]
        work_id = queue["recovery_items"][0]["recovery_work_id"]

        reviewer_view = server.request(
            "GET",
            _recovery_path(work_id),
            headers=headers("reviewer"),
        )
        assert reviewer_view.status == 200, reviewer_view.text
        reviewer_body = reviewer_view.json()
        assert reviewer_body["can_verify"] is False
        assert reviewer_body["status"] == "open"
        assert reviewer_body["phase"] == "Unprocessable"
        assert reviewer_body["route"] == "unprocessable"

        reviewer_verify = server.request(
            "POST",
            _verify_path(work_id),
            body={
                "expected_lifecycle_revision": reviewer_body["lifecycle_revision"],
                "expected_criterion_digest": reviewer_body["criterion"]["digest"],
                "idempotency_key": "t01-reviewer-cannot-verify",
            },
            headers=headers("reviewer"),
        )
        assert reviewer_verify.status == 404
        assert work_id not in reviewer_verify.text

        reviewer_route = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{blocked['application_id']}/current-route",
            headers=headers("reviewer"),
        )
        assert reviewer_route.status == 200, reviewer_route.text
        assert reviewer_route.json()["phase"] == "Unprocessable"
        assert reviewer_route.json()["route"] == "unprocessable"

        operator_view = server.request(
            "GET",
            _recovery_path(work_id),
            headers=operator_auth_headers(),
            use_session=False,
        )
        assert operator_view.status == 200, operator_view.text
        assert operator_view.json()["can_verify"] is True
        assert operator_view.json() == {**reviewer_body, "can_verify": True}

        operator_route = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{blocked['application_id']}/current-route",
            headers=operator_auth_headers(),
            use_session=False,
        )
        assert operator_route.status == 404
        assert blocked["application_id"] not in operator_route.text

        hidden_work = server.request(
            "GET",
            _recovery_path("recovery_work_not_present"),
            headers=headers("reviewer"),
        )
        assert hidden_work.status == 404
        assert "recovery_work_not_present" not in hidden_work.text

        stale = server.request(
            "POST",
            _verify_path(work_id),
            body={
                "expected_lifecycle_revision": reviewer_body["lifecycle_revision"] - 1,
                "expected_criterion_digest": reviewer_body["criterion"]["digest"],
                "idempotency_key": "t01-stale-command",
            },
            headers=operator_auth_headers(),
            use_session=False,
        )
        assert stale.status == 409
        assert stale.json()["detail"] == {
            "error": "S07_STALE",
            "reason_code": "recovery.context_changed",
        }
        unchanged = server.request(
            "GET",
            _recovery_path(work_id),
            headers=headers("reviewer"),
        ).json()
        assert unchanged == reviewer_body

        accepted = server.request(
            "POST",
            _verify_path(work_id),
            body={
                "expected_lifecycle_revision": reviewer_body["lifecycle_revision"],
                "expected_criterion_digest": reviewer_body["criterion"]["digest"],
                "idempotency_key": "t01-accepted-recovery",
            },
            headers=operator_auth_headers(),
            use_session=False,
        )
        assert accepted.status == 200, accepted.text
        accepted_body = accepted.json()
        assert accepted_body["status"] == "accepted"
        assert accepted_body["replayed"] is False
        assert accepted_body["phase"] == "Evidence Ready"
        assert accepted_body["successor_job_id"] != blocked["job_id"]
        assert accepted_body["successor_fence"] > blocked["fence"]

        replay = server.request(
            "POST",
            _verify_path(work_id),
            body={
                "expected_lifecycle_revision": reviewer_body["lifecycle_revision"],
                "expected_criterion_digest": reviewer_body["criterion"]["digest"],
                "idempotency_key": "t01-accepted-recovery",
            },
            headers=operator_auth_headers(),
            use_session=False,
        )
        assert replay.status == 200, replay.text
        replay_body = replay.json()
        assert replay_body["replayed"] is True
        assert replay_body["recovery_fact_id"] == accepted_body["recovery_fact_id"]
        assert replay_body["lifecycle_revision"] == accepted_body["lifecycle_revision"]

        wrong_revision = server.request(
            "POST",
            _verify_path(work_id),
            body={
                "expected_lifecycle_revision": reviewer_body["lifecycle_revision"] + 1,
                "expected_criterion_digest": reviewer_body["criterion"]["digest"],
                "idempotency_key": "t01-wrong-revision-after-resolution",
            },
            headers=operator_auth_headers(),
            use_session=False,
        )
        assert wrong_revision.status == 409
        assert wrong_revision.json()["detail"] == {
            "error": "S07_STALE",
            "reason_code": "recovery.context_changed",
        }

        reconciled = server.request(
            "POST",
            _verify_path(work_id),
            body={
                "expected_lifecycle_revision": reviewer_body["lifecycle_revision"],
                "expected_criterion_digest": reviewer_body["criterion"]["digest"],
                "idempotency_key": "t01-reconcile-after-resolution",
            },
            headers=operator_auth_headers(),
            use_session=False,
        )
        assert reconciled.status == 200, reconciled.text
        reconciled_body = reconciled.json()
        assert reconciled_body["replayed"] is True
        assert reconciled_body["recovery_fact_id"] == accepted_body["recovery_fact_id"]
        assert (
            reconciled_body["lifecycle_revision"]
            == accepted_body["lifecycle_revision"]
        )
        assert reconciled_body["status"] == "accepted"

        resolved = server.request(
            "GET",
            _recovery_path(work_id),
            headers=headers("reviewer"),
        ).json()
        assert resolved["status"] == "resolved"
        assert resolved["phase"] == "Evidence Ready"
        assert resolved["route"] == "pending_check"
        assert resolved["lifecycle_revision"] == reviewer_body["lifecycle_revision"] + 1
        assert resolved["recovery_fact_count"] == 1
        assert resolved["resolution_count"] == 1
        assert resolved["current_run_id"] is None

        gate = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{blocked['application_id']}/current-route",
            headers=headers("reviewer"),
        )
        assert gate.status == 200, gate.text
        assert gate.json()["phase"] == "Evidence Ready"
        assert gate.json()["route"] == "pending_check"
        assert gate.json()["current_run_id"] is None

        second_session = server.request(
            "POST",
            "/controlled/s01/api/session",
            body={},
            headers=demo_auth_headers(),
            use_session=False,
        )
        assert second_session.status == 204
        cross_scope_cookie = server._session_cookie
        assert cross_scope_cookie is not None
        cross_scope = server.request(
            "GET",
            _recovery_path(work_id),
            headers={**headers("reviewer"), "Cookie": cross_scope_cookie},
            use_session=False,
        )
        assert cross_scope.status == 404
        assert work_id not in cross_scope.text


def test_queue_contract_is_empty_and_scope_hidden_without_state(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "t01-empty.sqlite3"
    with UvicornLoopback(
        _environment(state_path, "verified"),
        app_target=APP_FACTORY,
        app_factory=True,
    ) as server:
        empty = _reviewer_queue(server)
        assert empty == {"items": [], "recovery_items": [], "projection_watermark": 0}

        anonymous = server.request(
            "GET",
            "/controlled/s01/api/queries/queue",
            use_session=False,
        )
        assert anonymous.status == 200
        assert anonymous.json() == {
            "items": [],
            "recovery_items": [],
            "projection_watermark": 0,
        }

        blocked = _admit_and_block(server, "t01-empty-a", now=10)
        second_session = server.request(
            "POST",
            "/controlled/s01/api/session",
            body={},
            headers=demo_auth_headers(),
            use_session=False,
        )
        assert second_session.status == 204
        cross_scope_cookie = server._session_cookie
        assert cross_scope_cookie is not None
        cross_scope = server.request(
            "GET",
            "/controlled/s01/api/queries/queue",
            headers={**headers("reviewer"), "Cookie": cross_scope_cookie},
            use_session=False,
        )
        assert cross_scope.status == 200
        assert cross_scope.json()["items"] == []
        assert cross_scope.json()["recovery_items"] == []
        assert blocked["recovery_work_id"] not in cross_scope.text


def _create_t01_app_environment(
    state_path: Path, verifier: str, react_dir: str
) -> dict[str, str]:
    values = _environment(state_path, verifier)
    values["TASK4_S01_TEST_REACT_DIR"] = react_dir
    return values


def test_react_shell_missing_build_fails_explicitly_and_legacy_route_stays(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "t01-react-missing.sqlite3"
    missing_build = tmp_path / "no-react-build"
    missing_build.mkdir()
    env = _create_t01_app_environment(state_path, "verified", str(missing_build))
    with UvicornLoopback(
        env,
        app_target="tests.test_t01_http:create_t01_test_app",
        app_factory=True,
    ) as server:
        unavailable = server.request(
            "GET",
            "/controlled/s01/react",
            use_session=False,
        )
        assert unavailable.status == 503
        assert unavailable.json() == {
            "detail": {
                "error": "S01_REACT_UNAVAILABLE",
                "message": "Controlled S01 React shell is not built",
            }
        }
        assert unavailable.headers["cache-control"] == "no-store"
        assert str(missing_build) not in unavailable.text

        legacy = server.request(
            "GET",
            "/controlled/s01",
            headers=demo_auth_headers(),
            use_session=False,
        )
        assert legacy.status == 200, legacy.text
        assert legacy.headers["cache-control"] == "no-store"

        queue = _reviewer_queue(server)
        assert queue == {"items": [], "recovery_items": [], "projection_watermark": 0}


def test_react_shell_serves_committed_build_with_no_store_shell_and_immutable_assets(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "t01-react-served.sqlite3"
    with UvicornLoopback(
        _environment(state_path, "verified"),
        app_target="tests.test_t01_http:create_t01_test_app",
        app_factory=True,
    ) as server:
        forbidden = server.request(
            "GET",
            "/controlled/s01/react",
            use_session=False,
        )
        assert forbidden.status == 403

        shell = server.request(
            "GET",
            "/controlled/s01/react",
            headers=demo_auth_headers(),
            use_session=False,
        )
        assert shell.status == 200, shell.text
        assert shell.headers["cache-control"] == "no-store"
        assert shell.headers["pragma"] == "no-cache"
        assert "set-cookie" in shell.headers

        import re

        assets = sorted(
            set(
                re.findall(
                    r"/static/react/assets/[A-Za-z0-9._/-]+\.js",
                    shell.text,
                )
            )
        )
        assert assets, "committed build index.html must reference hashed assets"
        for asset in assets:
            asset_response = server.request("GET", asset, use_session=False)
            assert asset_response.status == 200, asset
            assert "immutable" in asset_response.headers.get("cache-control", "")

        direct_index = server.request(
            "GET",
            "/static/react/index.html",
            use_session=False,
        )
        assert direct_index.status == 200
        assert direct_index.headers["cache-control"] == "no-store"

        legacy = server.request(
            "GET",
            "/controlled/s01",
            headers=demo_auth_headers(),
            use_session=False,
        )
        assert legacy.status == 200, legacy.text
