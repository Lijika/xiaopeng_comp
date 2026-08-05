from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from task4_consistency.controlled.s01 import (
    ControlledScenarioService,
    ControlledScenarioTestDriver,
)
from tests.test_s01_http import (
    UvicornLoopback,
    demo_auth_headers,
    headers,
    operator_auth_headers,
    submit,
)


ROOT = Path(__file__).resolve().parents[1]


class _FailFirstS07Driver(ControlledScenarioTestDriver):
    def __init__(self, service: ControlledScenarioService) -> None:
        super().__init__(service)
        self._failed = False

    def process_next_job(self, **kwargs: Any) -> Any:
        operation_fault = None if self._failed else "checker_incompatible"
        self._failed = True
        return super().process_next_job(
            operation_fault=operation_fault,
            **kwargs,
        )


def create_s07_test_app() -> Any:
    import task4_consistency.web.app as web

    state_path = Path(os.environ["TASK4_S01_TEST_STATE_PATH"])
    verifier_succeeds = os.environ.get("TASK4_S07_TEST_VERIFIER") == "verified"

    def verify(work: dict[str, Any]) -> dict[str, Any]:
        return {
            "verification_id": "s07-http-checker-probe-fact-1",
            "observed_at": int(work["opened_at"]) + 1,
            "evidence_kind": work["criterion"]["evidence_kind"],
            "scope": work["visibility_scope"],
            "recovery_work_id": work["recovery_work_id"],
            "criterion_digest": work["criterion"]["digest"],
            "conditions": [
                {
                    "condition_id": condition["condition_id"],
                    "verified": verifier_succeeds,
                    "evidence_digest": "a" * 64,
                }
                for condition in work["conditions"]
            ],
        }

    web.S01_BACKGROUND_ENABLED = False
    web.S01_REQUIRE_CONFIGURED_STARTUP = False
    web.S01_SERVICE = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
        recovery_verifier=verify,
        worker_identity="s07-http-worker",
    )
    web.S01_TEST_DRIVER = _FailFirstS07Driver(web.S01_SERVICE)
    return web.app


def _environment(state_path: Path, verifier: str) -> dict[str, str]:
    return {
        "TASK4_S01_STATE_PATH": str(state_path),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
        "TASK4_S01_TEST_BACKGROUND_ENABLED": "0",
        "TASK4_S07_TEST_VERIFIER": verifier,
    }


def _recovery_path(work_id: str) -> str:
    return f"/controlled/s01/api/queries/recovery-work-items/{work_id}"


def _verify_path(work_id: str) -> str:
    return f"/controlled/s01/api/commands/recovery-work-items/{work_id}/verify"


def test_reviewer_and_operator_recover_through_the_public_http_gate(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "target.sqlite3"
    app_factory = "tests.test_s07_http:create_s07_test_app"

    with UvicornLoopback(
        _environment(state_path, "false"),
        app_target=app_factory,
        app_factory=True,
    ) as server:
        admission = submit(server, "s07-http-admission").json()
        failed_response = server.request(
            "POST",
            "/controlled/s01/api/_test/commands/process",
            body={"worker_id": "s07-http-failure-worker", "now": 10},
            use_session=False,
        )
        assert failed_response.status == 200, failed_response.text
        failed = failed_response.json()
        assert failed["status"] == "blocked"
        work_id = failed["recovery_work_id"]
        assert isinstance(work_id, str) and work_id

        reviewer_response = server.request(
            "GET",
            _recovery_path(work_id),
            headers=headers("reviewer"),
        )
        assert reviewer_response.status == 200, reviewer_response.text
        reviewer_view = reviewer_response.json()
        reviewer_cookie = server._session_cookie
        assert reviewer_cookie is not None
        assert reviewer_view == {
            **reviewer_view,
            "schema_version": "recovery-work-view/1",
            "status": "open",
            "application_id": admission["application_id"],
            "phase": "Unprocessable",
            "primary_reason_code": "configuration.checker_unavailable",
            "related_reason_codes": [],
            "operation": "execute_check_run",
            "dependency": "c-demo-target-checker",
            "responsible_party": "policy_owner",
            "recovery_action": "restore_exact_release_or_activate_compatible_successor",
            "recovery_target": "Evidence Ready",
            "protected_business_revision": 0,
            "current_run_id": None,
            "recovery_fact_count": 0,
            "resolution_count": 0,
            "can_verify": False,
        }
        assert reviewer_view["attempts"] == [
            {
                "attempt": 1,
                "classification": "terminal",
                "status": "blocked",
                "started_at": 10,
                "retry_not_before": None,
            }
        ]
        assert reviewer_view["criterion"]["id"] == "s07-checker-compatibility/1"
        assert len(reviewer_view["criterion"]["digest"]) == 64
        assert isinstance(reviewer_view["projection_watermark"], int)

        public = json.dumps(reviewer_view, ensure_ascii=False, sort_keys=True)
        fixture = json.loads(
            (ROOT / "fixtures" / "applications" / "app_r53_bad_engine.json").read_text(
                encoding="utf-8"
            )
        )
        raw_strings = {
            value
            for document in fixture["documents"]
            for field in document["fields"].values()
            if isinstance((value := field.get("raw")), str) and len(value) > 1
        }
        assert fixture["application_id"] not in public
        assert all(value not in public for value in raw_strings)
        assert all(
            forbidden not in public
            for forbidden in ('"raw"', "run_spec", "evidence_snapshot", "object_ref")
        )

        operator_response = server.request(
            "GET",
            _recovery_path(work_id),
            headers=operator_auth_headers(),
            use_session=False,
        )
        assert operator_response.status == 200, operator_response.text
        assert operator_response.json() == {**reviewer_view, "can_verify": True}

        anonymous = server.request(
            "GET",
            _recovery_path(work_id),
            use_session=False,
        )
        second_session = server.request(
            "POST",
            "/controlled/s01/api/session",
            body={},
            headers=demo_auth_headers(),
            use_session=False,
        )
        assert second_session.status == 204
        cross_scope_cookie = server._session_cookie
        assert cross_scope_cookie is not None and cross_scope_cookie != reviewer_cookie
        cross_scope = server.request(
            "GET",
            _recovery_path(work_id),
            headers={**headers("reviewer"), "Cookie": cross_scope_cookie},
            use_session=False,
        )
        server._session_cookie = reviewer_cookie
        wrong_role = server.request(
            "POST",
            _verify_path(work_id),
            body={
                "expected_lifecycle_revision": reviewer_view["lifecycle_revision"],
                "expected_criterion_digest": reviewer_view["criterion"]["digest"],
                "idempotency_key": "s07-http-reviewer-cannot-recover",
            },
            headers=headers("reviewer"),
        )
        extra_field = server.request(
            "POST",
            _verify_path(work_id),
            body={
                "expected_lifecycle_revision": reviewer_view["lifecycle_revision"],
                "expected_criterion_digest": reviewer_view["criterion"]["digest"],
                "idempotency_key": "s07-http-extra-field",
                "recovered": True,
            },
            headers=operator_auth_headers(),
            use_session=False,
        )
        false_recovery = server.request(
            "POST",
            _verify_path(work_id),
            body={
                "expected_lifecycle_revision": reviewer_view["lifecycle_revision"],
                "expected_criterion_digest": reviewer_view["criterion"]["digest"],
                "idempotency_key": "s07-http-false-recovery",
            },
            headers=operator_auth_headers(),
            use_session=False,
        )

        assert anonymous.status == cross_scope.status == wrong_role.status == 404
        assert work_id not in anonymous.text
        assert work_id not in cross_scope.text
        assert work_id not in wrong_role.text
        assert extra_field.status == 422
        assert false_recovery.status == 409
        assert false_recovery.json()["detail"] == {
            "error": "S07_REJECTED",
            "reason_code": "recovery.criterion_not_satisfied",
        }
        unchanged = server.request(
            "GET",
            _recovery_path(work_id),
            headers=headers("reviewer"),
        ).json()
        assert unchanged == reviewer_view

    with UvicornLoopback(
        _environment(state_path, "verified"),
        app_target=app_factory,
        app_factory=True,
    ) as restarted:
        restarted._session_cookie = reviewer_cookie
        before = restarted.request(
            "GET",
            _recovery_path(work_id),
            headers=headers("reviewer"),
        ).json()
        assert before == reviewer_view

        stale = restarted.request(
            "POST",
            _verify_path(work_id),
            body={
                "expected_lifecycle_revision": before["lifecycle_revision"] - 1,
                "expected_criterion_digest": before["criterion"]["digest"],
                "idempotency_key": "s07-http-stale-browser",
            },
            headers=operator_auth_headers(),
            use_session=False,
        )
        assert stale.status == 409
        assert stale.json()["detail"] == {
            "error": "S07_STALE",
            "reason_code": "recovery.context_changed",
        }
        assert restarted.request(
            "GET",
            _recovery_path(work_id),
            headers=headers("reviewer"),
        ).json() == before

        accepted = restarted.request(
            "POST",
            _verify_path(work_id),
            body={
                "expected_lifecycle_revision": before["lifecycle_revision"],
                "expected_criterion_digest": before["criterion"]["digest"],
                "idempotency_key": "s07-http-verified-recovery",
            },
            headers=operator_auth_headers(),
            use_session=False,
        )
        assert accepted.status == 200, accepted.text
        assert accepted.json()["status"] == "accepted"
        assert accepted.json()["phase"] == "Evidence Ready"
        assert accepted.json()["successor_job_id"] != failed["job_id"]
        assert accepted.json()["successor_fence"] > failed["fence"]

        resolved = restarted.request(
            "GET",
            _recovery_path(work_id),
            headers=headers("reviewer"),
        ).json()
        assert resolved["status"] == "resolved"
        assert resolved["phase"] == "Evidence Ready"
        assert resolved["lifecycle_revision"] == before["lifecycle_revision"] + 1
        assert resolved["recovery_fact_count"] == 1
        assert resolved["resolution_count"] == 1
        assert resolved["current_run_id"] is None
        current = restarted.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{admission['application_id']}/current-route",
            headers=headers("reviewer"),
        )
        assert current.status == 200, current.text
        assert current.json()["phase"] == "Evidence Ready"
        assert current.json()["route"] == "pending_check"
        assert current.json()["current_run_id"] is None
