"""S05 business-exception acceptance over a real HTTP connection."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_s01_http import (
    DEMO_CREDENTIAL,
    UvicornLoopback,
    headers,
    operator_auth_headers,
    wait_for_projected_queue_item,
)


APPROVER_CREDENTIAL = "s05-http-approver-credential"
APPROVER_SUBJECT = "s05-http-approver"


def _environment(state_path: Path) -> dict[str, str]:
    return {
        "TASK4_S01_STATE_PATH": str(state_path),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
        "TASK4_S01_TEST_SCENARIO_ID": "app_bad_brand.json",
        "TASK4_S05_EXCEPTION_APPROVER_CREDENTIAL": APPROVER_CREDENTIAL,
        "TASK4_S05_EXCEPTION_APPROVER_SUBJECT": APPROVER_SUBJECT,
    }


def _approver_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {APPROVER_CREDENTIAL}"}


def _ready_request(server: UvicornLoopback, key: str) -> tuple[str, dict, dict]:
    server.open_s01_session()
    admitted = server.request(
        "POST",
        "/controlled/s01/api/commands/submit",
        body={"scenario_id": "app_bad_brand.json", "idempotency_key": f"{key}-intake"},
        headers=headers("integrator"),
    )
    assert admitted.status == 200, admitted.text
    application_id = admitted.json()["application_id"]
    queue_item = wait_for_projected_queue_item(server, application_id)
    work_item_id = queue_item["work_item_id"]
    work = server.request(
        "GET",
        f"/controlled/s01/api/queries/review-work-items/{work_item_id}",
        headers=headers("reviewer"),
    )
    assert work.status == 200, work.text
    work_body = work.json()
    finding = next(
        item
        for item in work_body["automatic_findings"]
        if item["rule_id"] == "R_BRAND_CROSS"
    )
    claimed = server.request(
        "POST",
        f"/controlled/s01/api/commands/review-work-items/{work_item_id}/claim",
        body={"expected_context": work_body["command_context"]},
        headers=headers("reviewer"),
    )
    assert claimed.status == 200, claimed.text
    request = server.request(
        "POST",
        f"/controlled/s01/api/commands/review-work-items/{work_item_id}/business-exceptions",
        body={
            "finding_id": finding["finding_id"],
            "reason_code": "DOCUMENTED_BRAND_VARIANCE",
            "expected_fence": claimed.json()["claim_fence"],
            "expected_context": work_body["command_context"],
            "idempotency_key": f"{key}-request",
        },
        headers=headers("reviewer"),
    )
    assert request.status == 200, request.text
    return application_id, finding, request.json()


def _claim_exception(server: UvicornLoopback, request: dict) -> tuple[dict, dict]:
    view = server.request(
        "GET",
        f"/controlled/s01/api/queries/business-exceptions/{request['request_id']}",
        headers=_approver_headers(),
        use_session=False,
    )
    assert view.status == 200, view.text
    claimed = server.request(
        "POST",
        f"/controlled/s01/api/commands/exception-work-items/{request['work_item_id']}/claim",
        body={"expected_context": view.json()["command_context"]},
        headers=_approver_headers(),
        use_session=False,
    )
    assert claimed.status == 200, claimed.text
    return view.json(), claimed.json()


def test_request_independent_approve_route_and_restart_over_http(tmp_path: Path) -> None:
    state_path = tmp_path / "target.sqlite3"
    environment = _environment(state_path)
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s01_test_app",
        app_factory=True,
    ) as server:
        application_id, finding, request = _ready_request(server, "s05-http-happy")
        reviewer_cookie = server._session_cookie
        anonymous = server.request(
            "GET",
            f"/controlled/s01/api/queries/business-exceptions/{request['request_id']}",
            use_session=False,
        )
        view, claim = _claim_exception(server, request)
        decided = server.request(
            "POST",
            f"/controlled/s01/api/commands/business-exceptions/{request['request_id']}/decide",
            body={
                "work_item_id": request["work_item_id"],
                "decision": "approved",
                "reason_code": "DOCUMENTED_VARIANCE_ACCEPTED",
                "expected_fence": claim["claim_fence"],
                "expected_context": view["command_context"],
                "idempotency_key": "s05-http-approve",
            },
            headers=_approver_headers(),
            use_session=False,
        )
        assert decided.status == 200, decided.text
        routed = server.request(
            "POST",
            f"/controlled/s01/api/commands/business-exceptions/{request['request_id']}/route",
            body={
                "expected_context": decided.json()["routing_context"],
                "idempotency_key": "s05-http-route",
            },
            headers=operator_auth_headers(),
            use_session=False,
        )
        route = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/current-route",
            headers=headers("reviewer"),
        )
        history = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/history",
            headers=headers("reviewer"),
        )

    assert anonymous.status == 404
    assert view["finding"]["verdict"] == "inconsistent"
    assert view["scope"] == "one_application_cycle_run_finding"
    assert view["actions"] == ["claim"]
    assert routed.status == route.status == history.status == 200
    assert routed.json()["completion_basis"] == "business_exception"
    assert route.json()["route"] == "human_complete"
    assert request["request_id"] in history.json()["runs"][0]["exception_ids"]
    public = json.dumps([view, routed.json(), route.json(), history.json()], ensure_ascii=False)
    fixture = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures/applications/app_bad_brand.json").read_text(
            encoding="utf-8"
        )
    )
    raw_values = [
        str(field["raw"])
        for document in fixture["documents"]
        for field in document["fields"].values()
    ]
    assert all(raw not in public for raw in raw_values)
    assert finding["finding_id"] in public

    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s01_test_app",
        app_factory=True,
    ) as restarted:
        restarted._session_cookie = reviewer_cookie
        rebuilt = restarted.request(
            "GET",
            f"/controlled/s01/api/queries/business-exceptions/{request['request_id']}",
            headers=_approver_headers(),
            use_session=False,
        )
        rebuilt_route = restarted.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/current-route",
            headers=headers("reviewer"),
        )
    assert rebuilt.status == rebuilt_route.status == 200
    assert rebuilt.json()["status"] == "approved"
    assert rebuilt_route.json()["route"] == "human_complete"


def test_reject_returns_fresh_review_and_forbidden_payloads_write_nothing(
    tmp_path: Path,
) -> None:
    with UvicornLoopback(
        _environment(tmp_path / "target.sqlite3"),
        app_target="task4_consistency.web.app:create_s01_test_app",
        app_factory=True,
    ) as server:
        application_id, _, request = _ready_request(server, "s05-http-reject")
        view, claim = _claim_exception(server, request)
        unknown = server.request(
            "POST",
            f"/controlled/s01/api/commands/business-exceptions/{request['request_id']}/decide",
            body={
                "work_item_id": request["work_item_id"],
                "decision": "rejected",
                "reason_code": "DOCUMENTED_VARIANCE_REJECTED",
                "expected_fence": claim["claim_fence"],
                "expected_context": view["command_context"],
                "idempotency_key": "s05-http-invalid",
                "unexpected": "not-accepted",
            },
            headers=_approver_headers(),
            use_session=False,
        )
        oversized = server.request(
            "POST",
            f"/controlled/s01/api/commands/business-exceptions/{request['request_id']}/decide",
            body={"unexpected": "x" * 300_000},
            headers=_approver_headers(),
            use_session=False,
        )
        no_batch = server.request(
            "POST",
            "/controlled/s01/api/commands/business-exceptions/batch",
            body={"items": [{"request_id": request["request_id"]}]},
            headers=_approver_headers(),
            use_session=False,
        )
        rejected = server.request(
            "POST",
            f"/controlled/s01/api/commands/business-exceptions/{request['request_id']}/decide",
            body={
                "work_item_id": request["work_item_id"],
                "decision": "rejected",
                "reason_code": "DOCUMENTED_VARIANCE_REJECTED",
                "expected_fence": claim["claim_fence"],
                "expected_context": view["command_context"],
                "idempotency_key": "s05-http-reject-decision",
            },
            headers=_approver_headers(),
            use_session=False,
        )
        fresh = wait_for_projected_queue_item(server, application_id)

    assert unknown.status == 422
    assert oversized.status == 413
    assert no_batch.status == 404
    assert rejected.status == 200
    assert rejected.json()["phase"] == "Manual Review"
    assert fresh["work_item_id"] == rejected.json()["successor_work_item_id"]


def create_s05_clock_test_app():
    import os
    import task4_consistency.web.app as web

    clock_path = Path(os.environ["TASK4_S05_TEST_CLOCK_PATH"])
    web.S01_SESSION_CLOCK = lambda: float(clock_path.read_text(encoding="ascii"))
    web.S01_SESSION_TTL_SECONDS = 5_000
    return web.create_s01_test_app()


def test_trusted_time_expiry_at_equality_over_http(tmp_path: Path) -> None:
    clock_path = tmp_path / "clock.txt"
    clock_path.write_text("1000", encoding="ascii")
    environment = {
        **_environment(tmp_path / "target.sqlite3"),
        "TASK4_S05_TEST_CLOCK_PATH": str(clock_path),
    }
    with UvicornLoopback(
        environment,
        app_target="tests.test_s05_http:create_s05_clock_test_app",
        app_factory=True,
    ) as server:
        application_id, _, request = _ready_request(server, "s05-http-expiry")
        view, _ = _claim_exception(server, request)
        clock_path.write_text(str(request["expires_at"]), encoding="ascii")
        expired = server.request(
            "POST",
            f"/controlled/s01/api/commands/business-exceptions/{request['request_id']}/expire",
            body={
                "expected_context": view["command_context"],
                "idempotency_key": "s05-http-expire",
            },
            headers=operator_auth_headers(),
            use_session=False,
        )
        fresh = wait_for_projected_queue_item(server, application_id)
        final = server.request(
            "GET",
            f"/controlled/s01/api/queries/business-exceptions/{request['request_id']}",
            headers=_approver_headers(),
            use_session=False,
        )

    assert expired.status == 200, expired.text
    assert expired.json()["phase"] == "Manual Review"
    assert fresh["work_item_id"] == expired.json()["successor_work_item_id"]
    assert final.json()["status"] == "expired"
    assert final.json()["current"] is False


def test_same_subject_cannot_approve_through_another_identity_channel(
    tmp_path: Path,
) -> None:
    environment = {
        **_environment(tmp_path / "target.sqlite3"),
        "TASK4_S05_EXCEPTION_APPROVER_SUBJECT": "c-demo-test-user",
    }
    with UvicornLoopback(
        environment,
        app_target="task4_consistency.web.app:create_s01_test_app",
        app_factory=True,
    ) as server:
        _, _, request = _ready_request(server, "s05-http-sod")
        view, claim = _claim_exception(server, request)
        denied = server.request(
            "POST",
            f"/controlled/s01/api/commands/business-exceptions/{request['request_id']}/decide",
            body={
                "work_item_id": request["work_item_id"],
                "decision": "approved",
                "reason_code": "DOCUMENTED_VARIANCE_ACCEPTED",
                "expected_fence": claim["claim_fence"],
                "expected_context": view["command_context"],
                "idempotency_key": "s05-http-self-approval",
            },
            headers=_approver_headers(),
            use_session=False,
        )
        final = server.request(
            "GET",
            f"/controlled/s01/api/queries/business-exceptions/{request['request_id']}",
            headers=_approver_headers(),
            use_session=False,
        )

    assert denied.status == 409
    assert denied.json()["detail"]["reason_code"] == "SEPARATION_OF_DUTIES_REQUIRED"
    assert final.json()["status"] == "pending"


def test_operator_close_drain_and_resume_over_http(tmp_path: Path) -> None:
    with UvicornLoopback(
        _environment(tmp_path / "target.sqlite3"),
        app_target="task4_consistency.web.app:create_s01_test_app",
        app_factory=True,
    ) as server:
        _, _, request = _ready_request(server, "s05-http-rollback")
        view, claim = _claim_exception(server, request)
        unauthorized = server.request(
            "POST",
            "/controlled/s01/api/commands/business-exception-operations/close",
            body={"idempotency_key": "s05-http-unauthorized-close"},
            use_session=False,
        )
        closed = server.request(
            "POST",
            "/controlled/s01/api/commands/business-exception-operations/close",
            body={"idempotency_key": "s05-http-close"},
            headers=operator_auth_headers(),
            use_session=False,
        )
        status = server.request(
            "GET",
            "/controlled/s01/api/queries/business-exception-operations",
            headers=operator_auth_headers(),
            use_session=False,
        )
        blocked = server.request(
            "POST",
            f"/controlled/s01/api/commands/business-exceptions/{request['request_id']}/decide",
            body={
                "work_item_id": request["work_item_id"],
                "decision": "approved",
                "reason_code": "DOCUMENTED_VARIANCE_ACCEPTED",
                "expected_fence": claim["claim_fence"],
                "expected_context": view["command_context"],
                "idempotency_key": "s05-http-closed-decision",
            },
            headers=_approver_headers(),
            use_session=False,
        )
        premature_resume = server.request(
            "POST",
            "/controlled/s01/api/commands/business-exception-operations/resume",
            body={"idempotency_key": "s05-http-premature-resume"},
            headers=operator_auth_headers(),
            use_session=False,
        )
        invalidated = server.request(
            "POST",
            f"/controlled/s01/api/commands/business-exceptions/{request['request_id']}/invalidate",
            body={
                "reason_code": "BUSINESS_EXCEPTION_OPERATIONS_CLOSED",
                "expected_context": view["command_context"],
                "idempotency_key": "s05-http-rollback-invalidation",
            },
            headers=operator_auth_headers(),
            use_session=False,
        )
        resumed = server.request(
            "POST",
            "/controlled/s01/api/commands/business-exception-operations/resume",
            body={"idempotency_key": "s05-http-resume"},
            headers=operator_auth_headers(),
            use_session=False,
        )
        final_status = server.request(
            "GET",
            "/controlled/s01/api/queries/business-exception-operations",
            headers=operator_auth_headers(),
            use_session=False,
        )

    assert unauthorized.status == 403
    assert closed.status == status.status == 200
    assert closed.json()["operations"] == status.json()["operations"] == "closed"
    assert blocked.status == 503
    assert blocked.json()["detail"]["reason_code"] == (
        "BUSINESS_EXCEPTION_OPERATIONS_CLOSED"
    )
    assert premature_resume.status == 503
    assert premature_resume.json()["detail"]["reason_code"] == (
        "BUSINESS_EXCEPTION_DRAIN_INCOMPLETE"
    )
    assert invalidated.status == resumed.status == final_status.status == 200
    assert invalidated.json()["phase"] == "Manual Review"
    assert resumed.json()["operations"] == final_status.json()["operations"] == "open"
