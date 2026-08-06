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
import sqlite3
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


class _InjectedQueueProjectionFault(RuntimeError):
    """Unexpected item-local queue projection fault injected by the fixture.

    Raised inside ``_require_application_state_authority`` after the shared
    accepted-admission preflight succeeds; it is deliberately not an
    ``_ApplicationStateAuthorityUnavailable`` so the item-local catch must
    not swallow it.
    """


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


def create_t01_expiring_app() -> Any:
    """S07-style test app with an externally driven session clock (P1)."""
    from tests.test_s07_http import _FailFirstS07Driver

    import task4_consistency.web.app as web

    clock_path = Path(os.environ["TASK4_S01_TEST_SESSION_CLOCK_PATH"])
    web.S01_SESSION_CLOCK = lambda: float(clock_path.read_text(encoding="ascii"))
    web.S01_SESSION_TTL_SECONDS = int(
        os.environ["TASK4_S01_TEST_SESSION_TTL_SECONDS"]
    )
    web.S01_BACKGROUND_ENABLED = False
    web.S01_REQUIRE_CONFIGURED_STARTUP = False
    web.S01_SERVICE = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=Path(os.environ["TASK4_S01_TEST_STATE_PATH"]),
        recovery_verifier=None,
        worker_identity="t01-expiring-worker",    )
    web.S01_TEST_DRIVER = _FailFirstS07Driver(web.S01_SERVICE)
    return web.app


def create_t01_queue_unavailable_app() -> Any:
    """S07-style test app whose shared admission authority is corrupted.

    One application is admitted and blocked (producing open Recovery Work),
    then a malicious accepted-admission audit event is appended so the
    shared authority check fails for every queue projection.  The queue
    route must return a minimized explicit unavailable response instead of
    an authoritative-looking empty queue.
    """
    import task4_consistency.web.app as web

    from task4_consistency.controlled.s01 import (
        AdmissionDisposition,
        ControlledScenarioTestDriver,
        S01CommandPrincipal,
    )

    web.S01_BACKGROUND_ENABLED = False
    web.S01_REQUIRE_CONFIGURED_STARTUP = False
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=Path(os.environ["TASK4_S01_TEST_STATE_PATH"]),
        recovery_verifier=None,
        worker_identity="t01-queue-unavailable-worker",
    )
    principal = S01CommandPrincipal(
        subject=os.environ.get("TASK4_S01_DEMO_SUBJECT", "c-demo-test-user"),
        role="integrator",
        scope="C-DEMO",
        source_id="t01-queue-unavailable-intake",
    )
    admission = service.submit_demo(
        scenario_id=SCENARIO,
        idempotency_key="t01-queue-unavailable-admission",
        principal=principal,
    )
    assert admission.disposition is AdmissionDisposition.ACCEPTED
    driver = ControlledScenarioTestDriver(service)
    failed = driver.process_next_job(
        worker_id="t01-queue-unavailable-worker",
        now=10,
        operation_fault="checker_incompatible",
    )
    assert failed.status == "blocked"
    assert failed.recovery_work_id is not None
    service._store.audit_events.append(
        {
            "event_id": "t01-attacker-broken-authority",
            "action": "controlled_admission",
            "result": "accepted",
            "application_id": "app_t01_attacker_shared",
            "scope": "C-DEMO",
            "envelope": "not-a-dict",
        }
    )
    service._store.persist()
    web.S01_SERVICE = service
    web.S01_TEST_DRIVER = None
    return web.app


def create_t01_queue_unexpected_app() -> Any:
    """S07-style test app whose recovery projection faults inside the narrowed
    item-local authority boundary.

    One application is admitted and blocked under a real session principal
    (producing open Recovery Work visible to that session), then an
    unexpected exception is injected inside ``_require_application_state_authority``
    after the shared accepted-admission preflight succeeds.  The queue route
    must surface the minimized bounded 500 for this in-boundary fault rather
    than HTTP 200/empty; if the item-local catch regressed to the old broad
    ``except Exception`` the injected fault would be swallowed and the queue
    would incorrectly return 200.  The issued session token is written to
    ``TASK4_S01_TEST_QUEUE_TOKEN_PATH`` for the test.
    """
    import task4_consistency.web.app as web

    from task4_consistency.controlled.s01 import (
        AdmissionDisposition,
        ControlledScenarioTestDriver,
        S01CommandPrincipal,
    )

    web.S01_BACKGROUND_ENABLED = False
    web.S01_REQUIRE_CONFIGURED_STARTUP = False
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=Path(os.environ["TASK4_S01_TEST_STATE_PATH"]),
        recovery_verifier=None,
        worker_identity="t01-queue-unexpected-worker",
    )
    token_path = Path(os.environ["TASK4_S01_TEST_QUEUE_TOKEN_PATH"])
    token, session_record = service.issue_session(
        now=float(web.S01_SESSION_CLOCK()),
        ttl_seconds=15 * 60,
        subject=os.environ.get("TASK4_S01_DEMO_SUBJECT", "c-demo-test-user"),
        roles=("integrator", "reviewer"),
    )
    token_path.write_text(token, encoding="ascii")
    session_principal = S01CommandPrincipal(
        subject=str(session_record["subject"]),
        role="integrator",
        scope=str(session_record["scope"]),
        source_id="t01-queue-unexpected-session",
    )
    admission = service.submit_demo(
        scenario_id=SCENARIO,
        idempotency_key="t01-queue-unexpected-admission",
        principal=session_principal,
    )
    assert admission.disposition is AdmissionDisposition.ACCEPTED
    driver = ControlledScenarioTestDriver(service)
    failed = driver.process_next_job(
        worker_id="t01-queue-unexpected-worker",
        now=10,
        operation_fault="checker_incompatible",
    )
    assert failed.status == "blocked"
    assert failed.recovery_work_id is not None
    original_require = service._require_application_state_authority

    def _injected_require(app: dict[str, Any] | None) -> None:
        original_require(app)
        raise _InjectedQueueProjectionFault(
            "injected unexpected fault inside the item-local authority"
        )

    service._require_application_state_authority = _injected_require
    service._store.persist()
    web.S01_SERVICE = service
    web.S01_TEST_DRIVER = None
    return web.app


def create_t01_media_counter_app() -> Any:
    """S07-style test app that counts real VerifyRecovery service calls.

    Reuses the S07 test app seam (admission + ``_FailFirstS07Driver``) and
    wraps the service's ``verify_recovery`` with a counting wrapper that
    persists the running call count to ``TASK4_S01_TEST_VERIFY_COUNT_PATH``
    after every invocation.  The real-socket media-type regression reads the
    counter between requests to prove rejected media types never reach the
    service while a valid media type does (even when it deterministically
    returns stale 409).  No production endpoint or service hook is added.
    """
    import task4_consistency.web.app as web

    from tests.test_s07_http import create_s07_test_app

    app = create_s07_test_app()
    service = web.S01_SERVICE
    counter_path = Path(os.environ["TASK4_S01_TEST_VERIFY_COUNT_PATH"])
    counter_path.write_text("0", encoding="ascii")
    original = service.verify_recovery

    def counting_verify_recovery(**kwargs: Any) -> dict[str, Any]:
        count = int(counter_path.read_text(encoding="ascii"))
        counter_path.write_text(str(count + 1), encoding="ascii")
        return original(**kwargs)

    service.verify_recovery = counting_verify_recovery  # type: ignore[method-assign]
    return app


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

    request_body = verify_operation.get("requestBody")
    assert request_body is not None
    assert request_body.get("required") is True
    request_schema = resolve(
        request_body["content"]["application/json"]["schema"]
    )
    assert request_schema.get("additionalProperties") is False
    assert set(request_schema["required"]) == {
        "expected_lifecycle_revision",
        "expected_criterion_digest",
        "idempotency_key",
    }
    assert request_schema["properties"]["expected_lifecycle_revision"].get(
        "minimum"
    ) == 1
    digest_schema = request_schema["properties"]["expected_criterion_digest"]
    assert digest_schema.get("minLength") == 64
    assert digest_schema.get("maxLength") == 64
    assert digest_schema.get("pattern") == "^[0-9a-f]{64}$"
    key_schema = request_schema["properties"]["idempotency_key"]
    assert key_schema.get("minLength") == 1
    assert key_schema.get("maxLength") == 200
    for status in ("404", "409", "413", "503"):
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

    verify_422_schema = (
        verify_operation["responses"]
        .get("422", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema")
    )
    assert verify_422_schema is not None
    verify_422_model = resolve(verify_422_schema)
    assert verify_422_model.get("additionalProperties") is not True
    assert set(verify_422_model["required"]) == {"detail"}
    verify_422_detail = verify_422_model["properties"]["detail"]
    assert "anyOf" in verify_422_detail, verify_422_detail
    one_of_refs = [
        member.get("$ref") or member.get("type") for member in verify_422_detail["anyOf"]
    ]
    assert "#/components/schemas/S01ErrorDetail" in one_of_refs
    assert "array" in one_of_refs
    validation_schema = components["S01ValidationErrorItem"]
    assert validation_schema.get("additionalProperties") is not True
    assert {"loc", "msg", "type"} <= set(validation_schema["required"])
    assert validation_schema["properties"]["loc"]["type"] == "array"
    # Sanitized 422 items never reflect rejected input or context back to
    # the client, so the schema must not declare them either.
    assert "input" not in validation_schema["properties"]
    assert "ctx" not in validation_schema["properties"]

    recovery_read_operation = next(
        operation
        for operation in spec["paths"][recovery_path].values()
        if isinstance(operation, dict) and "responses" in operation
    )
    recovery_404 = (
        recovery_read_operation["responses"]
        .get("404", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema")
    )
    assert recovery_404 is not None
    assert resolve(recovery_404) is components["S01ErrorResponse"] or (
        resolve(recovery_404).get("additionalProperties") is not True
    )

    current_route_path = (
        "/controlled/s01/api/queries/applications/{application_id}/current-route"
    )
    current_route_operation = next(
        operation
        for operation in spec["paths"][current_route_path].values()
        if isinstance(operation, dict) and "responses" in operation
    )
    current_route_404 = (
        current_route_operation["responses"]
        .get("404", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema")
    )
    assert current_route_404 is not None
    assert resolve(current_route_404).get("additionalProperties") is not True

    queue_operation = next(
        operation
        for operation in spec["paths"][queue_path].values()
        if isinstance(operation, dict) and "responses" in operation
    )
    queue_200 = queue_operation["responses"]["200"]
    assert "X-S01-Access-Ended" in queue_200.get("headers", {})
    access_ended = queue_schema["properties"].get("access_ended")
    assert access_ended is not None
    assert "boolean" in [
        member.get("type") for member in access_ended.get("anyOf", [])
    ]

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


def test_verify_validation_errors_return_the_real_detail_list_shape(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "t01-validation.sqlite3"
    with UvicornLoopback(
        _environment(state_path, "verified"),
        app_target=APP_FACTORY,
        app_factory=True,
    ) as server:
        blocked = _admit_and_block(server, "t01-validation-a", now=10)

        missing_field = server.request(
            "POST",
            _verify_path(blocked["recovery_work_id"]),
            body={
                "expected_lifecycle_revision": 1,
                "idempotency_key": "t01-validation-missing-digest",
            },
            headers=operator_auth_headers(),
            use_session=False,
        )
        assert missing_field.status == 422
        payload = missing_field.json()
        assert isinstance(payload["detail"], list)
        assert all(
            {"loc", "msg", "type"} <= set(item)
            and isinstance(item["loc"], list)
            for item in payload["detail"]
        )
        assert any(
            "expected_criterion_digest" in item["loc"] for item in payload["detail"]
        )

        bad_digest = server.request(
            "POST",
            _verify_path(blocked["recovery_work_id"]),
            body={
                "expected_lifecycle_revision": 1,
                "expected_criterion_digest": "not-a-hex-digest",
                "idempotency_key": "t01-validation-bad-digest",
            },
            headers=operator_auth_headers(),
            use_session=False,
        )
        assert bad_digest.status == 422
        assert isinstance(bad_digest.json()["detail"], list)

        extra_field = server.request(
            "POST",
            _verify_path(blocked["recovery_work_id"]),
            body={
                "expected_lifecycle_revision": 1,
                "expected_criterion_digest": "a" * 64,
                "idempotency_key": "t01-validation-extra",
                "target": "attacker-injected",
            },
            headers=operator_auth_headers(),
            use_session=False,
        )
        assert extra_field.status == 422
        assert isinstance(extra_field.json()["detail"], list)
        assert blocked["recovery_work_id"] not in extra_field.text
        assert all(
            value not in extra_field.text for value in _restricted_strings()
        )


def test_verify_contract_rejection_is_a_typed_error_response(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "t01-contract.sqlite3"
    with UvicornLoopback(
        _environment(state_path, "verified"),
        app_target=APP_FACTORY,
        app_factory=True,
    ) as server:
        blocked = _admit_and_block(server, "t01-contract-a", now=10)
        padded = server.request(
            "POST",
            _verify_path(blocked["recovery_work_id"]),
            body={
                "expected_lifecycle_revision": 1,
                "expected_criterion_digest": "a" * 64,
                "idempotency_key": "  padded-key  ",
            },
            headers=operator_auth_headers(),
            use_session=False,
        )
        assert padded.status == 422
        assert padded.json() == {
            "detail": {
                "error": "S07_INVALID_COMMAND",
                "message": "VerifyRecovery command does not match the contract",
            }
        }


def test_unauthenticated_recovery_reads_and_commands_are_hidden_typed_404(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "t01-hidden.sqlite3"
    with UvicornLoopback(
        _environment(state_path, "verified"),
        app_target=APP_FACTORY,
        app_factory=True,
    ) as server:
        blocked = _admit_and_block(server, "t01-hidden-a", now=10)
        hidden_read = server.request(
            "GET",
            _recovery_path(blocked["recovery_work_id"]),
            use_session=False,
        )
        assert hidden_read.status == 404
        assert hidden_read.json() == {"detail": {"error": "S07_NOT_FOUND"}}
        assert blocked["recovery_work_id"] not in hidden_read.text

        hidden_command = server.request(
            "POST",
            _verify_path(blocked["recovery_work_id"]),
            body={
                "expected_lifecycle_revision": 1,
                "expected_criterion_digest": "a" * 64,
                "idempotency_key": "t01-hidden-command",
            },
            use_session=False,
        )
        assert hidden_command.status == 404
        assert hidden_command.json() == {"detail": {"error": "S07_NOT_FOUND"}}
        assert blocked["recovery_work_id"] not in hidden_command.text


def test_react_shell_falls_back_to_legacy_for_partial_builds(tmp_path: Path) -> None:
    state_path = tmp_path / "t01-partial.sqlite3"
    legacy_marker = "一致性审核工作台 · C-DEMO"
    outside_asset = tmp_path / "outside.css"
    outside_asset.write_text("/* outside */", encoding="utf-8")
    cases = {
        "missing-js": (
            '<html><head></head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/index-MISSING-JS.js"></script>'
            "</body></html>"
        ),
        "missing-css": (
            '<html><head>'
            '<link rel="stylesheet" href="/static/react/assets/index-MISSING-CSS.css">'
            '</head><body><div id="root"></div></body></html>'
        ),
        "no-assets": (
            '<html><head></head><body><div id="root"></div></body></html>'
        ),
        "css-only": (
            '<html><head>'
            '<link rel="stylesheet" href="/static/react/assets/index.css">'
            '</head><body><div id="root"></div></body></html>'
        ),
        "script-src-css": (
            '<html><head></head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/index.css"></script>'
            "</body></html>"
        ),
        "traversal": (
            '<html><head></head><body><div id="root"></div>'
            '<script type="module" src="/static/react/../outside.css"></script>'
            "</body></html>"
        ),
        "query-ambiguity": (
            '<html><head></head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/index.css?v=1"></script>'
            "</body></html>"
        ),
        "fragment-ambiguity": (
            '<html><head></head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/index.css#entry"></script>'
            "</body></html>"
        ),
        "non-executable-module": (
            '<html><head></head><body><div id="root"></div>'
            '<script type="application/json" src="/static/react/assets/index.js"></script>'
            "</body></html>"
        ),
        "reversed-stylesheet-attr": (
            '<html><head>'
            '<link href="/static/react/assets/index-MISSING-CSS.css" rel="stylesheet">'
            '</head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/index.js"></script>'
            "</body></html>"
        ),
        "valid-module-stylesheet-traversal": (
            '<html><head>'
            '<link rel="stylesheet" href="/static/react/../outside.css">'
            '</head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/index.js"></script>'
            "</body></html>"
        ),
        "valid-module-stylesheet-query": (
            '<html><head>'
            '<link rel="stylesheet" href="/static/react/assets/index.css?v=1">'
            '</head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/index.js"></script>'
            "</body></html>"
        ),
        "valid-module-stylesheet-fragment": (
            '<html><head>'
            '<link rel="stylesheet" href="/static/react/assets/index.css#entry">'
            '</head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/index.js"></script>'
            "</body></html>"
        ),
        "valid-module-stylesheet-missing": (
            '<html><head>'
            '<link rel="stylesheet" href="/static/react/assets/index-MISSING-CSS.css">'
            '</head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/index.js"></script>'
            "</body></html>"
        ),
        "duplicate-script-src": (
            '<html><head></head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/missing.js" '
            'src="/static/react/assets/existing.js"></script>'
            "</body></html>"
        ),
        "duplicate-link-href": (
            '<html><head>'
            '<link rel="stylesheet" href="/static/react/assets/missing.css" '
            'href="/static/react/assets/existing.css">'
            '</head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/index.js"></script>'
            "</body></html>"
        ),
        "duplicate-type": (
            '<html><head></head><body><div id="root"></div>'
            '<script type="module" type="module" src="/static/react/assets/index.js"></script>'
            "</body></html>"
        ),
        "duplicate-rel": (
            '<html><head>'
            '<link rel="stylesheet" rel="stylesheet" href="/static/react/assets/index.css">'
            '</head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/index.js"></script>'
            "</body></html>"
        ),
        "uppercase-stylesheet": (
            '<html><head>'
            '<link rel="STYLESHEET" href="/static/react/assets/index-MISSING-CSS.css">'
            '</head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/index.js"></script>'
            "</body></html>"
        ),
        "absent-type-entry": (
            '<html><head></head><body><div id="root"></div>'
            '<script src="/static/react/assets/index.js"></script>'
            "</body></html>"
        ),
        "classic-entry": (
            '<html><head></head><body><div id="root"></div>'
            '<script type="text/javascript" src="/static/react/assets/index.js"></script>'
            "</body></html>"
        ),
        "valid-module-script-traversal": (
            '<html><head></head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/index.js"></script>'
            '<script src="/static/react/../outside.js"></script>'
            "</body></html>"
        ),
        "valid-module-script-query": (
            '<html><head></head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/index.js"></script>'
            '<script src="/static/react/assets/index.js?v=1"></script>'
            "</body></html>"
        ),
        "valid-module-script-fragment": (
            '<html><head></head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/index.js"></script>'
            '<script src="/static/react/assets/index.js#entry"></script>'
            "</body></html>"
        ),
        "valid-module-script-missing": (
            '<html><head></head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/index.js"></script>'
            '<script src="/static/react/assets/index-MISSING-JS.js"></script>'
            "</body></html>"
        ),
        "external-script-url": (
            '<html><head></head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/index.js"></script>'
            '<script src="https://evil.example/index.js"></script>'
            "</body></html>"
        ),
        "external-stylesheet-url": (
            '<html><head>'
            '<link rel="stylesheet" href="https://evil.example/index.css">'
            '</head><body><div id="root"></div>'
            '<script type="module" src="/static/react/assets/index.js"></script>'
            "</body></html>"
        ),
    }
    for case_name, index_html in cases.items():
        react_dir = tmp_path / f"partial-{case_name}"
        react_dir.mkdir()
        assets_dir = react_dir / "assets"
        assets_dir.mkdir()
        (assets_dir / "index.css").write_text("/* present */", encoding="utf-8")
        (assets_dir / "index.js").write_text("/* present */", encoding="utf-8")
        (assets_dir / "existing.css").write_text("/* present */", encoding="utf-8")
        (assets_dir / "existing.js").write_text("/* present */", encoding="utf-8")
        outside_js = tmp_path / "outside.js"
        outside_js.write_text("/* outside */", encoding="utf-8")
        (react_dir / "index.html").write_text(index_html, encoding="utf-8")
        env = _create_t01_app_environment(
            state_path, "verified", str(react_dir)
        )
        with UvicornLoopback(
            env,
            app_target="tests.test_t01_http:create_t01_test_app",
            app_factory=True,
        ) as server:
            shell = server.request(
                "GET",
                "/controlled/s01/react",
                headers=demo_auth_headers(),
                use_session=False,
            )
            assert shell.status == 200, (case_name, shell.text)
            assert legacy_marker in shell.text, case_name
            assert "index-MISSING" not in shell.text, case_name
            assert "outside.css" not in shell.text, case_name
            assert shell.headers["cache-control"] == "no-store"
            assert "set-cookie" in shell.headers


def test_queue_shared_authority_failure_returns_minimized_unavailable(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "t01-queue-unavailable.sqlite3"
    with UvicornLoopback(
        _environment(state_path, "verified"),
        app_target="tests.test_t01_http:create_t01_queue_unavailable_app",
        app_factory=True,
    ) as server:
        issued = server.request(
            "POST",
            "/controlled/s01/api/session",
            body={},
            headers=demo_auth_headers(),
            use_session=False,
        )
        assert issued.status == 204, issued.text
        queue = server.request(
            "GET",
            "/controlled/s01/api/queries/queue",
            headers=headers("reviewer"),
        )
        assert queue.status == 503
        assert queue.json() == {
            "detail": {
                "error": "S01_QUEUE_UNAVAILABLE",
                "reason_code": "recovery.authority_unavailable",
            }
        }
        assert queue.headers["cache-control"] == "no-store"
        assert "app_t01_attacker_shared" not in queue.text
        assert "recovery_work" not in queue.text
        assert all(
            value not in queue.text for value in _restricted_strings()
        )


def test_verify_auth_happens_before_validation_and_422_is_sanitized(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "t01-authfirst.sqlite3"
    with UvicornLoopback(
        _environment(state_path, "verified"),
        app_target=APP_FACTORY,
        app_factory=True,
    ) as server:
        blocked = _admit_and_block(server, "t01-authfirst-a", now=10)
        work_id = blocked["recovery_work_id"]
        sentinel = "t01-authfirst-SENTINEL-9f8e7d6c5b4a"
        giant = "x" * 65536
        bodies = {
            "missing": {
                "expected_lifecycle_revision": 1,
                "idempotency_key": sentinel,
            },
            "malformed": {
                "expected_lifecycle_revision": "not-an-int",
                "expected_criterion_digest": "z" * 64,
                "idempotency_key": sentinel,
            },
            "extra": {
                "expected_lifecycle_revision": 1,
                "expected_criterion_digest": "a" * 64,
                "idempotency_key": sentinel,
                "target": "injected-extra-field",
            },
            "oversized": {
                "expected_lifecycle_revision": 1,
                "expected_criterion_digest": "a" * 64,
                "idempotency_key": giant,
            },
        }
        for case, body in bodies.items():
            for label, request_headers in (
                ("anonymous", {}),
                ("reviewer", headers("reviewer")),
            ):
                response = server.request(
                    "POST",
                    _verify_path(work_id),
                    body=body,
                    headers=request_headers,
                    use_session=False,
                )
                assert response.status == 404, (case, label, response.text)
                assert response.json() == {"detail": {"error": "S07_NOT_FOUND"}}, (
                    case,
                    label,
                    response.text,
                )
                assert response.headers["cache-control"] == "no-store", (
                    case,
                    label,
                )
                assert sentinel not in response.text, (case, label)
                assert work_id not in response.text, (case, label)
                assert "injected-extra-field" not in response.text, (case, label)
                assert giant not in response.text, (case, label)

            operator_response = server.request(
                "POST",
                _verify_path(work_id),
                body=body,
                headers=operator_auth_headers(),
                use_session=False,
            )
            assert operator_response.status == 422, (case, operator_response.text)
            payload = operator_response.json()
            assert isinstance(payload["detail"], list), (case, payload)
            assert all(
                {"loc", "msg", "type"} <= set(item) for item in payload["detail"]
            ), (case, payload)
            assert '"input"' not in operator_response.text, (case, "input echoed")
            assert '"ctx"' not in operator_response.text, (case, "ctx echoed")
            assert sentinel not in operator_response.text, (case, "sentinel echoed")
            assert giant not in operator_response.text, (case, "oversized echoed")
            assert work_id not in operator_response.text, (case, "work id echoed")
            assert "injected-extra-field" not in operator_response.text, (
                case,
                "extra echoed",
            )
            assert all(
                value not in operator_response.text
                for value in _restricted_strings()
            ), (case, "restricted echoed")


def test_verify_auth_hides_before_raw_body_parsing(tmp_path: Path) -> None:
    """Raw wire bodies never reach body parsing for anonymous or Reviewer
    callers: the existence-hiding Operator gate is solved before any request
    byte is read.  An authorized Operator still receives a truthful, sanitized
    422 (or a bounded 413) with no rejected input, context, credential,
    idempotency value, work ID, or oversized content echoed."""
    from task4_consistency.web.app import S02_MAX_COMMAND_BYTES

    state_path = tmp_path / "t01-raw-body.sqlite3"
    with UvicornLoopback(
        _environment(state_path, "verified"),
        app_target=APP_FACTORY,
        app_factory=True,
    ) as server:
        blocked = _admit_and_block(server, "t01-raw-body-a", now=10)
        work_id = blocked["recovery_work_id"]
        sentinel = "t01-raw-SENTINEL-a1b2c3d4e5"
        giant = "x" * 65536
        digest64 = "a" * 64
        valid_body = (
            b'{"expected_lifecycle_revision": 1, '
            b'"expected_criterion_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", '
            b'"idempotency_key": "t01-raw-body-key"}'
        )
        wire_cases = {
            "malformed-json": (
                b'{"broken": "t01-raw-SENTINEL-a1b2c3d4e5"',
                "application/json",
            ),
            "empty-body": (b"", "application/json"),
            "wrong-content-type": (valid_body, "text/plain"),
            "schema-invalid-json": (
                (
                    b'{"expected_lifecycle_revision": "not-an-int", '
                    b'"expected_criterion_digest": "zzzz", '
                    b'"idempotency_key": "t01-raw-SENTINEL-a1b2c3d4e5"}'
                ),
                "application/json",
            ),
            "extra-fields": (
                (
                    b'{"expected_lifecycle_revision": 1, '
                    b'"expected_criterion_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", '
                    b'"idempotency_key": "t01-raw-SENTINEL-a1b2c3d4e5", '
                    b'"target": "injected-raw-extra-field"}'
                ),
                "application/json",
            ),
            "oversized-field": (
                json.dumps(
                    {
                        "expected_lifecycle_revision": 1,
                        "expected_criterion_digest": digest64,
                        "idempotency_key": giant,
                    }
                ).encode("utf-8"),
                "application/json",
            ),
        }
        for case, (raw, content_type) in wire_cases.items():
            for label, auth_headers in (
                ("anonymous", {}),
                ("reviewer", headers("reviewer")),
            ):
                response = server.raw_request(
                    "POST",
                    _verify_path(work_id),
                    body=raw,
                    content_type=content_type,
                    headers=auth_headers,
                    use_session=False,
                )
                assert response.status == 404, (case, label, response.text)
                assert response.json() == {"detail": {"error": "S07_NOT_FOUND"}}, (
                    case,
                    label,
                    response.text,
                )
                assert response.headers["cache-control"] == "no-store", (
                    case,
                    label,
                )
                assert sentinel not in response.text, (case, label)
                assert work_id not in response.text, (case, label)
                assert "injected-raw-extra-field" not in response.text, (case, label)
                assert giant not in response.text, (case, label)

            operator_response = server.raw_request(
                "POST",
                _verify_path(work_id),
                body=raw,
                content_type=content_type,
                headers=operator_auth_headers(),
                use_session=False,
            )
            assert operator_response.status == 422, (case, operator_response.text)
            payload = operator_response.json()
            assert isinstance(payload["detail"], list), (case, payload)
            assert all(
                {"loc", "msg", "type"} <= set(item) for item in payload["detail"]
            ), (case, payload)
            assert '"input"' not in operator_response.text, (case, "input echoed")
            assert '"ctx"' not in operator_response.text, (case, "ctx echoed")
            assert sentinel not in operator_response.text, (case, "sentinel echoed")
            assert giant not in operator_response.text, (case, "oversized echoed")
            assert work_id not in operator_response.text, (case, "work id echoed")
            assert "injected-raw-extra-field" not in operator_response.text, (
                case,
                "extra echoed",
            )
            assert all(
                value not in operator_response.text
                for value in _restricted_strings()
            ), (case, "restricted echoed")

        huge_body = b'{"expected_lifecycle_revision": 1, "idempotency_key": "' + (
            b"x" * S02_MAX_COMMAND_BYTES
        ) + b'"}'
        for label, auth_headers in (
            ("anonymous", {}),
            ("reviewer", headers("reviewer")),
        ):
            hidden = server.raw_request(
                "POST",
                _verify_path(work_id),
                body=huge_body,
                headers=auth_headers,
                use_session=False,
            )
            assert hidden.status == 404, (label, hidden.text)
            assert hidden.json() == {"detail": {"error": "S07_NOT_FOUND"}}, label
        oversized = server.raw_request(
            "POST",
            _verify_path(work_id),
            body=huge_body,
            headers=operator_auth_headers(),
            use_session=False,
        )
        assert oversized.status == 413, oversized.text
        assert oversized.json() == {
            "detail": {
                "error": "S07_INVALID_COMMAND",
                "message": "VerifyRecovery command exceeds the allowed size",
            }
        }
        assert work_id not in oversized.text
        assert all(
            value not in oversized.text for value in _restricted_strings()
        )

        # No raw wire attempt — anonymous, Reviewer, or authorized Operator —
        # may commit a VerifyRecovery service effect: the work must still be
        # open with zero recovery facts.
        work_view = server.request(
            "GET",
            _recovery_path(work_id),
            headers=operator_auth_headers(),
            use_session=False,
        )
        assert work_view.status == 200, work_view.text
        view_payload = work_view.json()
        assert view_payload["status"] == "open"
        assert view_payload["recovery_fact_count"] == 0


def test_verify_rejects_non_json_media_types_before_service(tmp_path: Path) -> None:
    """The VerifyRecovery media-type gate accepts only the exact
    ``application/json`` essence (case-normalized, valid parameters) and
    rejects missing, disguised, lookalike, and malformed-parameter content
    types before any service call.

    A real service-call counter (wrapped in the test app factory seam) proves
    every rejected media type -- Operator and anonymous/Reviewer alike --
    leaves ``verify_recovery`` untouched, while valid media types increment
    it (even when they deterministically return stale 409)."""
    state_path = tmp_path / "t01-media-type.sqlite3"
    counter_path = tmp_path / "t01-media-type-verify-count.txt"
    env = _environment(state_path, "verified")
    env["TASK4_S01_TEST_VERIFY_COUNT_PATH"] = str(counter_path)
    with UvicornLoopback(
        env,
        app_target="tests.test_t01_http:create_t01_media_counter_app",
        app_factory=True,
    ) as server:
        blocked = _admit_and_block(server, "t01-media-type-a", now=10)
        work_id = blocked["recovery_work_id"]
        sentinel = "t01-media-SENTINEL-0f1e2d3c4b5a"

        def verify_call_count() -> int:
            return int(counter_path.read_text(encoding="ascii"))

        valid_body = (
            b'{"expected_lifecycle_revision": 1, '
            b'"expected_criterion_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", '
            b'"idempotency_key": "t01-media-type-key"}'
        )
        media_type_cases = {
            "missing-content-type": (valid_body, None),
            "missing-equals": (valid_body, "application/json; charset"),
            "empty-param-name": (valid_body, "application/json; =oops"),
            "empty-param-value": (valid_body, "application/json; charset="),
            "unterminated-quote": (valid_body, 'application/json; charset="unterminated'),
            "bad-param-name": (valid_body, "application/json; a b=c"),
            "mime-comment": (valid_body, "application/json; foo=(comment)bar"),
            "non-ascii-param-name": (valid_body, "application/json; föö=bar"),
            "non-ascii-token-value": (valid_body, "application/json; foo=bär"),
            "empty-param-segment": (valid_body, "application/json;; charset=utf-8"),
            "trailing-empty-param": (valid_body, "application/json; charset=utf-8;"),
            "ows-before-equals": (valid_body, "application/json; charset =utf-8"),
            "ows-after-equals": (valid_body, "application/json; charset= utf-8"),
            "disguised-param": (valid_body, "text/plain; note=application/json"),
            "lookalike-patch": (valid_body, "application/json-patch+json"),
            "lookalike-jsonx": (valid_body, "application/jsonx"),
            "plain-text": (valid_body, "text/plain"),
        }
        assert verify_call_count() == 0
        for case, (raw, content_type) in media_type_cases.items():
            for label, auth_headers in (
                ("anonymous", {}),
                ("reviewer", headers("reviewer")),
            ):
                response = server.raw_request(
                    "POST",
                    _verify_path(work_id),
                    body=raw,
                    content_type=content_type,
                    headers=auth_headers,
                    use_session=False,
                )
                assert response.status == 404, (case, label, response.text)
                assert response.json() == {"detail": {"error": "S07_NOT_FOUND"}}, (
                    case,
                    label,
                    response.text,
                )
                assert response.headers["cache-control"] == "no-store", (
                    case,
                    label,
                )
                assert work_id not in response.text, (case, label)
                assert verify_call_count() == 0, (
                    case,
                    label,
                    "service called for a hidden request",
                )

            operator_response = server.raw_request(
                "POST",
                _verify_path(work_id),
                body=raw,
                content_type=content_type,
                headers=operator_auth_headers(),
                use_session=False,
            )
            assert operator_response.status == 422, (case, operator_response.text)
            payload = operator_response.json()
            assert isinstance(payload["detail"], list), (case, payload)
            assert payload["detail"] == [
                {
                    "loc": ["body"],
                    "msg": "expected application/json request content type",
                    "type": "content_type",
                }
            ], (case, payload)
            assert '"input"' not in operator_response.text, (case, "input echoed")
            assert '"ctx"' not in operator_response.text, (case, "ctx echoed")
            assert work_id not in operator_response.text, (case, "work id echoed")
            assert all(
                value not in operator_response.text
                for value in _restricted_strings()
            ), (case, "restricted echoed")
            assert verify_call_count() == 0, (
                case,
                "service called for a rejected media type",
            )

        # A case-normalized JSON essence with valid parameters IS accepted,
        # including valid quoted-string values with a space or an escaped
        # quote: each request reaches business handling (a deterministic stale
        # 409, which commits no effect) instead of a media-type 422, and the
        # call counter increments -- proving the counter is live and
        # discriminating.
        work_view = server.request(
            "GET",
            _recovery_path(work_id),
            headers=operator_auth_headers(),
            use_session=False,
        ).json()
        real_revision = work_view["lifecycle_revision"]
        for accepted_type in (
            "Application/JSON",
            "application/json; charset=utf-8",
            'application/json; note="hello world"',
            'application/json; note="a\\"b"',
            "application/json \t;\tcharset=utf-8",
            'application/json; note="(comment) text"',
            'application/json; note="bär"',
        ):
            accepted = server.raw_request(
                "POST",
                _verify_path(work_id),
                body=json.dumps(
                    {
                        "expected_lifecycle_revision": real_revision + 100,
                        "expected_criterion_digest": "a" * 64,
                        "idempotency_key": sentinel,
                    }
                ).encode("utf-8"),
                content_type=accepted_type,
                headers=operator_auth_headers(),
                use_session=False,
            )
            assert accepted.status == 409, (accepted_type, accepted.text)
            assert "S07_STALE" in accepted.text, (accepted_type, accepted.text)
        assert verify_call_count() == 7, (
            "valid media types must reach verify_recovery",
        )

        # Complementary zero-persisted-effect proof: no attempt committed a
        # VerifyRecovery fact (rejected media types never call the service;
        # the valid-but-stale calls intentionally return 409 with no effect).
        final_view = server.request(
            "GET",
            _recovery_path(work_id),
            headers=operator_auth_headers(),
            use_session=False,
        ).json()
        assert final_view["status"] == "open"
        assert final_view["recovery_fact_count"] == 0


def test_queue_unexpected_failure_is_bounded_and_not_authoritative_empty(
    tmp_path: Path,
) -> None:
    """An unexpected fault inside the item-local authority boundary (after the
    shared accepted-admission preflight succeeds) reaches the minimized
    bounded 500 rather than HTTP 200/empty.  A broadened per-item
    ``except Exception`` would swallow the injected fault and incorrectly
    return 200, so this regression pins the narrowed catch boundary."""
    state_path = tmp_path / "t01-unexpected.sqlite3"
    token_path = tmp_path / "t01-unexpected-token.txt"
    env = _environment(state_path, "verified")
    env["TASK4_S01_TEST_QUEUE_TOKEN_PATH"] = str(token_path)
    with UvicornLoopback(
        env,
        app_target="tests.test_t01_http:create_t01_queue_unexpected_app",
        app_factory=True,
    ) as server:
        token = token_path.read_text(encoding="ascii").strip()
        assert token
        queue = server.request(
            "GET",
            "/controlled/s01/api/queries/queue",
            headers={**headers("reviewer"), "Cookie": f"s01_session={token}"},
            use_session=False,
        )
        assert queue.status == 500
        payload = queue.json()
        assert payload == {
            "detail": {
                "error": "S01_INTERNAL_ERROR",
                "message": "Controlled S01 request failed",
            }
        }
        assert queue.headers["cache-control"] == "no-store"
        assert "recovery_items" not in queue.text
        assert all(value not in queue.text for value in _restricted_strings())
