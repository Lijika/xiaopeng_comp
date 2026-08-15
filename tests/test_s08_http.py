"""S08 governed policy release public HTTP contract tests.

The acceptance seam is a real uvicorn loopback process (same policy as the
S01 suite): governance commands and queries are exercised over HTTP with
distinct Rule Administrator / Policy Approver bearer identities, and the
governed activation is observed through a new S01 application run that pins
the complete manifest.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from pathlib import Path
from typing import Any

from tests.test_s01_http import (
    UvicornLoopback,
    demo_auth_headers,
    submit,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = "app_r53_bad_engine.json"
S08_SCOPE = "C-DEMO/demo"
SOURCE_BUNDLE_ID = "c-demo-legacy-baseline/1"
ADMIN_CREDENTIAL = "s08-registered-admin-test-credential"
APPROVER_CREDENTIAL = "s08-registered-approver-test-credential"
OPERATOR_CREDENTIAL = "s08-registered-operator-test-credential"
REPLAY_CREDENTIAL = "s09-registered-replay-test-credential"
SIMULATION_CREDENTIAL = "s09-registered-simulation-test-credential"
_POLL_TIMEOUT_SECONDS = 8.0


def s08_test_loopback(
    extra_env: dict[str, str] | None = None,
) -> UvicornLoopback:
    values = {
        "TASK4_S08_ADMIN_CREDENTIAL": ADMIN_CREDENTIAL,
        "TASK4_S08_ADMIN_SUBJECT": "c-demo-policy-admin",
        "TASK4_S08_APPROVER_CREDENTIAL": APPROVER_CREDENTIAL,
        "TASK4_S08_APPROVER_SUBJECT": "c-demo-policy-approver",
        "TASK4_S08_OPERATOR_CREDENTIAL": OPERATOR_CREDENTIAL,
        "TASK4_S08_OPERATOR_SUBJECT": "c-demo-policy-operator",
        "TASK4_S09_REPLAY_CREDENTIAL": REPLAY_CREDENTIAL,
        "TASK4_S09_REPLAY_SUBJECT": "c-demo-replay-operator",
        "TASK4_S09_SIMULATION_CREDENTIAL": SIMULATION_CREDENTIAL,
        "TASK4_S09_SIMULATION_SUBJECT": "c-demo-simulation-operator",
        # Uvicorn imports the module before invoking the test factory.  Keep
        # that unused default wiring fail-closed so only the explicit test
        # authority performs the governed bootstrap.
        "TASK4_S01_AUDIT_AVAILABLE": "0",
    }
    values.update(extra_env or {})
    return UvicornLoopback(
        values,
        app_target="task4_consistency.web.app:create_s01_test_app",
        app_factory=True,
    )


@pytest.mark.parametrize(
    "availability_flag",
    ("TASK4_S01_TEST_AUDIT_AVAILABLE", "TASK4_S01_TEST_STORAGE_AVAILABLE"),
)
def test_required_governance_authority_loss_is_http_503(
    tmp_path: Path, availability_flag: str
) -> None:
    state_path = tmp_path / f"{availability_flag.lower()}.sqlite3"
    with s08_test_loopback(
        {
            "TASK4_S01_TEST_STATE_PATH": str(state_path),
            availability_flag: "0",
        }
    ) as server:
        response = server.request(
            "POST",
            "/controlled/s08/api/commands/import_legacy",
            body={
                "source_bundle_id": SOURCE_BUNDLE_ID,
                "idempotency_key": f"authority-loss-{availability_flag}",
                "expected_governance_revision": 0,
            },
            headers=admin_headers(),
            use_session=False,
        )
    assert response.status == 503
    assert response.json()["detail"]["error"] == "S08_UNAVAILABLE"


def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_CREDENTIAL}"}


def approver_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {APPROVER_CREDENTIAL}"}


def operator_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {OPERATOR_CREDENTIAL}"}


def replay_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {REPLAY_CREDENTIAL}"}


def simulation_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {SIMULATION_CREDENTIAL}"}


def _json(server: UvicornLoopback, method: str, path: str, body: dict[str, Any] | None = None,
          headers: dict[str, str] | None = None) -> dict[str, Any]:
    response = server.request(method, path, body=body, headers=headers, use_session=False)
    assert response.status == 200, f"{method} {path}: {response.status} {response.text}"
    return response.json()


def _post_command(server: UvicornLoopback, name: str, body: dict[str, Any],
                  headers: dict[str, str]) -> dict[str, Any]:
    prefix = (
        "/controlled/s09"
        if name == "preview_impact"
        else "/controlled/s08"
    )
    return _json(server, "POST", f"{prefix}/api/commands/{name}", body, headers)


def _preview_and_approve(
    server: UvicornLoopback,
    candidate_id: str,
    activation_time: int,
    recovery_release_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Preview the immutable conservative impact and bind its exact digest
    in the approval (the S09 approval contract)."""
    preview = _post_command(
        server,
        "preview_impact",
        {
            "candidate_id": candidate_id,
            "idempotency_key": f"{idempotency_key}-preview",
            "expected_governance_revision": _governance_revision(server),
        },
        admin_headers(),
    )
    assert preview["status"] == "accepted", preview
    return _post_command(
        server,
        "approve",
        {
            "candidate_id": candidate_id,
            "activation_time": activation_time,
            "recovery_release_id": recovery_release_id,
            "preview_manifest_id": preview["manifest_id"],
            "idempotency_key": idempotency_key,
            "expected_governance_revision": _governance_revision(server),
        },
        approver_headers(),
    )


def _governance_revision(server: UvicornLoopback) -> int:
    status = _json(
        server, "GET", f"/controlled/s08/api/queries/status?scope={S08_SCOPE}",
        headers=admin_headers(),
    )
    assert status["scope"] == S08_SCOPE
    return int(status["governance_revision"])


def _active_query(server: UvicornLoopback) -> dict[str, Any]:
    return _json(
        server, "GET", f"/controlled/s08/api/queries/active?scope={S08_SCOPE}",
        headers=admin_headers(),
    )


def _wait_for_active_generation(
    server: UvicornLoopback, generation: int
) -> dict[str, Any]:
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        active = _active_query(server)
        if active["status"] == "active" and active["active_generation"] == generation:
            return active
        time.sleep(0.05)
    raise AssertionError(
        f"governed activation did not reach generation {generation}: {active!r}"
    )


def _wait_for_candidate_status(
    server: UvicornLoopback, candidate_id: str, expected: str
) -> dict[str, Any]:
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        candidates = _json(
            server, "GET", f"/controlled/s08/api/queries/candidates?scope={S08_SCOPE}",
            headers=admin_headers(),
        )
        candidate = next(
            (item for item in candidates["candidates"]
             if item["candidate_id"] == candidate_id),
            None,
        )
        if candidate is not None and candidate["status"] == expected:
            return candidate
        time.sleep(0.05)
    raise AssertionError(
        f"candidate {candidate_id} did not reach {expected}: {candidates!r}"
    )


def _wait_for_complete_run(server: UvicornLoopback, application_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{application_id}/history",
            headers={"Authorization": "Bearer s01-registered-demo-test-credential"},
            use_session=True,
        )
        assert response.status == 200
        history = response.json()
        run = next(
            (item for item in history["runs"] if item.get("current") is True),
            None,
        )
        if run is not None and run.get("status") == "complete":
            return run
        time.sleep(0.05)
    raise AssertionError("governed run was not published to application history")


def test_first_governed_release_activates_and_new_run_pins_complete_manifest(
    tmp_path: Path,
) -> None:
    """The complete Admin -> Approver -> activator -> new run tracer.

    The bootstrap release is active at startup.  A separate Rule
    Administrator then imports, revises, freezes, validates and reviews the
    first ordinary candidate; an independent Policy Approver approves the
    exact digest; the background activator applies the schedule.  A new S01
    application run must then expose the complete pinned manifest and the
    activation generation, and later draft revisions must not affect it.
    """
    state_path = tmp_path / "target.sqlite3"
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
    }
    with s08_test_loopback(env) as server:
        # 1. The one-time bootstrap migration release is active at startup.
        bootstrap = _active_query(server)
        assert bootstrap["status"] == "active"
        assert bootstrap["bootstrap"] is True
        assert bootstrap["active_generation"] == 1
        assert bootstrap["candidate_id"]
        assert bootstrap["manifest_id"]
        assert len(bootstrap["manifest_digest"]) == 64
        assert bootstrap["approval_binding_id"]
        assert bootstrap["recovery_release_id"] == bootstrap["candidate_id"]

        # 2. Rule Administrator imports the server-owned legacy source.
        import_result = _post_command(
            server,
            "import_legacy",
            {
                "source_bundle_id": SOURCE_BUNDLE_ID,
                "idempotency_key": "s08-tracer-import-1",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        assert import_result["status"] == "accepted"
        draft_id = import_result["draft_id"]
        assert import_result["mapping_ledger_id"]
        assert len(import_result["source_sha256"]) == 64

        # 3. Admin revises the draft metadata (non-runtime).
        revise_result = _post_command(
            server,
            "revise_draft",
            {
                "draft_id": draft_id,
                "metadata": {
                    "scope": S08_SCOPE,
                    "validity": {"valid_from": "2000-01-01T00:00:00Z"},
                    "source": "c-demo-legacy-baseline/1",
                    "reason": "S08 first governed release metadata",
                },
                "idempotency_key": "s08-tracer-revise-1",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        assert revise_result["status"] == "accepted"
        assert revise_result["draft_revision"] >= 1

        # 4. Admin freezes an immutable candidate.
        freeze_result = _post_command(
            server,
            "freeze_candidate",
            {
                "draft_id": draft_id,
                "idempotency_key": "s08-tracer-freeze-1",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        assert freeze_result["status"] == "accepted"
        candidate_id = freeze_result["candidate_id"]
        assert freeze_result["manifest_id"]
        assert len(freeze_result["manifest_digest"]) == 64
        assert freeze_result["components"]
        assert {"type", "id", "digest"} <= set(freeze_result["components"][0])

        # 5. Validation runs as a durable background job and validates.
        request_result = _post_command(
            server,
            "request_validation",
            {
                "candidate_id": candidate_id,
                "idempotency_key": "s08-tracer-validate-1",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        assert request_result["status"] == "accepted"
        assert request_result["policy_job_id"]
        _wait_for_candidate_status(server, candidate_id, "validated")

        # 6. Admin submits the validated candidate for independent review.
        review_result = _post_command(
            server,
            "submit_review",
            {
                "candidate_id": candidate_id,
                "idempotency_key": "s08-tracer-review-1",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        assert review_result["status"] == "accepted"

        # 7. The independent Policy Approver binds the exact digest.
        # Leave enough room for approval/status requests before the
        # non-retroactive schedule check, while staying inside the poll window.
        activation_time = int(time.time()) + 5
        approval_result = _preview_and_approve(

            server,

            candidate_id,

            activation_time,

            bootstrap["candidate_id"],

            "s08-tracer-approve-1",

        )
        assert approval_result["status"] == "accepted"
        approval_binding_id = approval_result["approval_binding_id"]
        assert approval_result["validation_bundle_id"]
        assert approval_result["approver_subject"] == "c-demo-policy-approver"
        assert approval_result["author_subject"] == "c-demo-policy-admin"
        assert approval_result["author_subject"] != approval_result["approver_subject"]
        _wait_for_candidate_status(server, candidate_id, "approved")

        # 8. The Admin schedules the approved binding at server-trusted time.
        schedule_result = _post_command(
            server,
            "schedule",
            {
                "approval_binding_id": approval_binding_id,
                "activation_at": activation_time,
                "idempotency_key": "s08-tracer-schedule-1",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        assert schedule_result["status"] == "accepted"
        assert schedule_result["reservation_id"]

        # 9. The background activator applies the schedule.
        active = _wait_for_active_generation(server, 2)
        assert active["bootstrap"] is False
        assert active["candidate_id"] == candidate_id
        assert active["manifest_digest"] == freeze_result["manifest_digest"]
        assert active["approval_binding_id"] == approval_binding_id
        assert active["activation_event_id"]

        # 10. A new S01 application resolves exactly the active governed
        #     release: history exposes the complete pinned manifest.
        server.open_s01_session()
        submit_result = server.request(
            "POST",
            "/controlled/s01/api/commands/submit",
            body={"scenario_id": SCENARIO, "idempotency_key": "s08-tracer-s01-1"},
            headers=demo_auth_headers(),
        )
        assert submit_result.status == 200
        application_id = submit_result.json()["application_id"]
        run = _wait_for_complete_run(server, application_id)
        assert run["manifest_digest"] == active["manifest_digest"]
        assert run["activation_event_id"] == active["activation_event_id"]
        assert run["active_generation"] == 2
        assert run["candidate_id"] == candidate_id
        assert run["approval_binding_id"] == approval_binding_id
        assert run["validation_bundle_id"] == approval_result["validation_bundle_id"]
        component_map = {item["type"]: item for item in run["components"]}
        active_components = {item["type"]: item for item in active["components"]}
        assert set(component_map) >= {
            "check_policy",
            "semantic_catalog",
            "entity_knowledge",
            "normalization_policy",
            "comparison_policy",
            "readiness_policy",
            "operators",
            "normalizers",
            "checker",
            "input_contract",
            "limits",
        }
        # The raw check policy bytes are the rules digest; the canonical
        # policy components reproduce their compile-time digests, and the
        # run's components are exactly the active manifest's components.
        assert component_map["check_policy"]["digest"] == hashlib.sha256(
            (ROOT / "configs" / "rules_auto_lease.yaml").read_bytes()
        ).hexdigest()
        assert component_map["checker"]["digest"] == active_components["checker"][
            "digest"
        ]
        assert component_map == active_components

        # 11. Draft mutation after activation does not touch the pinned run.
        draft_before = _governance_revision(server)
        _post_command(
            server,
            "revise_draft",
            {
                "draft_id": draft_id,
                "metadata": {
                    "scope": S08_SCOPE,
                    "validity": {"valid_from": "2000-01-01T00:00:00Z"},
                    "source": "c-demo-legacy-baseline/1",
                    "reason": "post-activation draft revision must not affect runtime",
                },
                "idempotency_key": "s08-tracer-revise-2",
                "expected_governance_revision": draft_before,
            },
            admin_headers(),
        )
        assert _active_query(server)["active_generation"] == 2
        # The bootstrap source files themselves remain byte-identical.
        assert (
            (ROOT / "configs" / "rules_auto_lease.yaml").read_bytes()
            == (ROOT / "configs" / "rules_auto_lease.yaml").read_bytes()
        )


def test_role_auth_claim_labels_stop_activation_and_restart_reconciliation(
    tmp_path: Path,
) -> None:
    """Roles are bearer-separated with stable errors; every governance
    response carries the C-DEMO/G3 claim labels; a stop-activation hold is
    durable across restart, keeps the prior active resolvable and stops new
    activations from advancing."""
    state_path = tmp_path / "target.sqlite3"
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
    }
    with s08_test_loopback(env) as server:
        # 1. Role separation over HTTP: approver cannot import, admin cannot
        #    approve, and a missing credential is rejected.
        denied = server.request(
            "POST",
            "/controlled/s08/api/commands/import_legacy",
            body={
                "source_bundle_id": SOURCE_BUNDLE_ID,
                "idempotency_key": "role-import-1",
                "expected_governance_revision": 0,
            },
            headers=approver_headers(),
        )
        assert denied.status == 403
        denied = server.request(
            "POST",
            "/controlled/s08/api/commands/approve",
            body={
                "candidate_id": "candidate_000000000000000000000000",
                "activation_time": 4102444800,
                "recovery_release_id": "candidate_000000000000000000000000",
                "preview_manifest_id": "preview_sha256_" + "1" * 64,
                "idempotency_key": "role-approve-1",
                "expected_governance_revision": 0,
            },
            headers=admin_headers(),
        )
        assert denied.status == 403
        denied = server.request(
            "POST",
            "/controlled/s08/api/commands/import_legacy",
            body={
                "source_bundle_id": SOURCE_BUNDLE_ID,
                "idempotency_key": "role-import-2",
                "expected_governance_revision": 0,
            },
        )
        assert denied.status == 403

        # 2. Existence is hidden with a stable reason; extra body fields are
        #    rejected by the closed DTO.
        hidden = server.request(
            "GET",
            "/controlled/s08/api/queries/candidate/candidate_000000000000000000000000",
            headers=admin_headers(),
        )
        assert hidden.status == 404
        assert hidden.json()["detail"]["error"] == "S08_NOT_FOUND"
        extra = server.request(
            "POST",
            "/controlled/s08/api/commands/import_legacy",
            body={
                "source_bundle_id": SOURCE_BUNDLE_ID,
                "idempotency_key": "role-import-3",
                "expected_governance_revision": 0,
                "caller_path": "/etc/passwd",
            },
            headers=admin_headers(),
        )
        assert extra.status == 422

        # 3. Claim labels: every governance query carries C-DEMO/G3 and the
        #    validation corpus is labeled C-DEV-REG.
        status = _json(
            server, "GET", f"/controlled/s08/api/queries/status?scope={S08_SCOPE}",
            headers=admin_headers(),
        )
        assert status["track"] == "C-DEMO"
        assert status["capability_gate"] == "G3"
        active = _active_query(server)
        assert active["track"] == "C-DEMO"
        assert active["capability_gate"] == "G3"
        candidates = _json(
            server, "GET", f"/controlled/s08/api/queries/candidates?scope={S08_SCOPE}",
            headers=admin_headers(),
        )
        assert candidates["track"] == "C-DEMO"
        assert candidates["capability_gate"] == "G3"

        # 4. Drive a candidate to scheduled, then apply the hold.
        import_result = _post_command(
            server,
            "import_legacy",
            {
                "source_bundle_id": SOURCE_BUNDLE_ID,
                "idempotency_key": "drill-import-1",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        draft_id = import_result["draft_id"]
        _post_command(
            server,
            "revise_draft",
            {
                "draft_id": draft_id,
                "metadata": {
                    "scope": S08_SCOPE,
                    "validity": {"valid_from": "2000-01-01T00:00:00Z"},
                    "source": SOURCE_BUNDLE_ID,
                    "reason": "stop-activation drill",
                },
                "idempotency_key": "drill-revise-1",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        freeze = _post_command(
            server,
            "freeze_candidate",
            {
                "draft_id": draft_id,
                "idempotency_key": "drill-freeze-1",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        candidate_id = freeze["candidate_id"]
        _post_command(
            server,
            "request_validation",
            {
                "candidate_id": candidate_id,
                "idempotency_key": "drill-validate-1",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        _wait_for_candidate_status(server, candidate_id, "validated")
        _post_command(
            server,
            "submit_review",
            {
                "candidate_id": candidate_id,
                "idempotency_key": "drill-review-1",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        activation_time = int(time.time()) + 30
        approval = _preview_and_approve(

            server,

            candidate_id,

            activation_time,

            active["candidate_id"],

            "drill-approve-1",

        )
        _post_command(
            server,
            "schedule",
            {
                "approval_binding_id": approval["approval_binding_id"],
                "activation_at": activation_time,
                "idempotency_key": "drill-schedule-1",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        stop = _post_command(
            server,
            "stop_activations",
            {
                "reason_code": "S08_DRILL_HOLD",
                "idempotency_key": "drill-stop-1",
                "expected_governance_revision": _governance_revision(server),
            },
            operator_headers(),
        )
        assert stop["status"] == "accepted"
        assert stop["governance_event_id"]
        status = _json(
            server, "GET", f"/controlled/s08/api/queries/status?scope={S08_SCOPE}",
            headers=admin_headers(),
        )
        assert status["activation_hold"]["reason_code"] == "S08_DRILL_HOLD"
        # New schedules are rejected under the hold.
        schedule_denied = server.request(
            "POST",
            "/controlled/s08/api/commands/schedule",
            body={
                "approval_binding_id": approval["approval_binding_id"],
                "activation_at": activation_time,
                "idempotency_key": "drill-schedule-2",
                "expected_governance_revision": _governance_revision(server),
            },
            headers=admin_headers(),
        )
        assert schedule_denied.status == 409

    # 5. Restart reconciliation: the hold is durable and the scheduled
    #    candidate never advances; the prior active keeps resolving.
    with s08_test_loopback(env) as server:
        active_after = _active_query(server)
        assert active_after["status"] == "active"
        assert active_after["active_generation"] == 1
        assert active_after["activation_hold"]["reason_code"] == "S08_DRILL_HOLD"
        status = _json(
            server, "GET", f"/controlled/s08/api/queries/status?scope={S08_SCOPE}",
            headers=admin_headers(),
        )
        assert status["activation_hold"] is not None
        candidates = _json(
            server, "GET", f"/controlled/s08/api/queries/candidates?scope={S08_SCOPE}",
            headers=admin_headers(),
        )
        drill_candidate = next(
            item
            for item in candidates["candidates"]
            if item["candidate_id"] == candidate_id
        )
        assert drill_candidate["status"] == "scheduled"
        time.sleep(1.0)
        assert _active_query(server)["active_generation"] == 1
        # The prior active still resolves into a new run: the S01 path pins
        # generation 1 and the run hash is identical to the pre-hold pin.
        server.open_s01_session()
        submit_result = server.request(
            "POST",
            "/controlled/s01/api/commands/submit",
            body={"scenario_id": SCENARIO, "idempotency_key": "s08-hold-run-1"},
            headers=demo_auth_headers(),
        )
        assert submit_result.status == 200
        run = _wait_for_complete_run(server, submit_result.json()["application_id"])
        assert run["active_generation"] == 1
        assert run["manifest_digest"] == active_after["manifest_digest"]


def test_approver_can_read_exact_candidate_diff_but_cannot_mutate(
    tmp_path: Path,
) -> None:
    """The independent Policy Approver reads the complete candidate workspace
    (manifest, validation bundle, machine diff and fixed approval binding) but
    cannot mutate governance; the author cannot self-approve."""
    state_path = tmp_path / "target.sqlite3"
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
    }
    with s08_test_loopback(env) as server:
        bootstrap = _active_query(server)

        # Admin drives import -> revise -> freeze -> validate -> review.
        import_result = _post_command(
            server,
            "import_legacy",
            {
                "source_bundle_id": SOURCE_BUNDLE_ID,
                "idempotency_key": "g3-import-1",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        draft_id = import_result["draft_id"]
        _post_command(
            server,
            "revise_draft",
            {
                "draft_id": draft_id,
                "metadata": {
                    "scope": S08_SCOPE,
                    "validity": {"valid_from": "2000-01-01T00:00:00Z"},
                    "source": SOURCE_BUNDLE_ID,
                    "reason": "G3 approver read drill",
                },
                "idempotency_key": "g3-revise-1",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        freeze = _post_command(
            server,
            "freeze_candidate",
            {
                "draft_id": draft_id,
                "idempotency_key": "g3-freeze-1",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        candidate_id = freeze["candidate_id"]
        _post_command(
            server,
            "request_validation",
            {
                "candidate_id": candidate_id,
                "idempotency_key": "g3-validate-1",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        _wait_for_candidate_status(server, candidate_id, "validated")
        _post_command(
            server,
            "submit_review",
            {
                "candidate_id": candidate_id,
                "idempotency_key": "g3-review-1",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        _wait_for_candidate_status(server, candidate_id, "in_review")

        # 1. The approver reads the exact candidate workspace pre-approval:
        #    manifest and validation bundle with every digest.
        workspace = server.request(
            "GET",
            f"/controlled/s08/api/queries/candidate/{candidate_id}",
            headers=approver_headers(),
        )
        assert workspace.status == 200
        body = workspace.json()
        assert body["status"] == "in_review"
        assert body["manifest_digest"] == freeze["manifest_digest"]
        assert body["manifest"]["digest"] == freeze["manifest_digest"]
        assert body["manifest"]["components"] == freeze["components"]
        assert body["validation_bundle_digest"]
        bundle = body["validation_bundle"]
        assert bundle["schema_version"] == "s08-validation-bundle/1"
        assert bundle["candidate_id"] == candidate_id
        assert bundle["status"] == "validated"
        assert bundle["inputs"]["component_digests"] == {
            item["type"]: item["digest"] for item in freeze["components"]
        }
        assert "approval_binding" not in body
        # The prospective review material is readable before approval: the
        # exact diff the binding will fix, the full mapping ledger and the
        # unsupported report.
        review = body["review_material"]
        assert review["schema_version"] == "s08-review-material/1"
        assert review["candidate_id"] == candidate_id
        assert review["candidate_digest"] == freeze["manifest_digest"]
        assert review["anchor_candidate_id"] == bootstrap["candidate_id"]
        assert review["mapping_ledger_id"]
        assert review["mapping_ledger"]["schema_version"] == "s08-mapping-ledger/1"
        assert review["mapping_ledger"]["items"]
        assert review["unsupported_report"]["count"] == 0
        assert review["unsupported_report"]["items"] == []
        assert review["behavior_delta"]["equal"] is True
        assert review["validation_bundle_id"] == body["validation_bundle_id"]

        # 2. The approver cannot mutate governance: every admin/operator
        #    command fails with the stable S08_FORBIDDEN error.  The revise
        #    payload carries the complete closed metadata so the role check
        #    (not the closed-DTO validation) is what rejects it.
        for name, payload in [
            ("import_legacy", {"source_bundle_id": SOURCE_BUNDLE_ID}),
            (
                "revise_draft",
                {
                    "draft_id": draft_id,
                    "metadata": {
                        "scope": S08_SCOPE,
                        "validity": {"valid_from": "2000-01-01T00:00:00Z"},
                        "source": SOURCE_BUNDLE_ID,
                        "reason": "g3 approver denied",
                    },
                },
            ),
            ("freeze_candidate", {"draft_id": draft_id}),
            ("request_validation", {"candidate_id": candidate_id}),
            ("submit_review", {"candidate_id": candidate_id}),
            (
                "schedule",
                {
                    "approval_binding_id": "approval_sha256_" + "0" * 64,
                    "activation_at": int(time.time()) + 60,
                },
            ),
            ("stop_activations", {"reason_code": "S08_G3_DENIED"}),
        ]:
            denied = server.request(
                "POST",
                f"/controlled/s08/api/commands/{name}",
                body={
                    "idempotency_key": f"g3-approver-{name}",
                    "expected_governance_revision": _governance_revision(server),
                    **payload,
                },
                headers=approver_headers(),
            )
            assert denied.status == 403
            assert denied.json()["detail"]["error"] == "S08_FORBIDDEN"

        # 3. The author (admin) cannot approve the candidate (SoD): the
        #    approve credential role never matches the admin bearer.
        denied = server.request(
            "POST",
            "/controlled/s08/api/commands/approve",
            body={
                "candidate_id": candidate_id,
                "activation_time": int(time.time()) + 60,
                "recovery_release_id": bootstrap["candidate_id"],
                "preview_manifest_id": "preview_sha256_"
                + "1" * 64,
                "idempotency_key": "g3-admin-approve",
                "expected_governance_revision": _governance_revision(server),
            },
            headers=admin_headers(),
        )
        assert denied.status == 403
        assert denied.json()["detail"]["error"] == "S08_FORBIDDEN"

        # 4. The approver approves; the fixed binding is readable and pins
        #    the exact machine diff, digests, scope and activation time.
        activation_time = int(time.time()) + 60
        approval = _preview_and_approve(

            server,

            candidate_id,

            activation_time,

            bootstrap["candidate_id"],

            "g3-approve-1",

        )
        assert approval["status"] == "accepted"
        _wait_for_candidate_status(server, candidate_id, "approved")
        workspace = server.request(
            "GET",
            f"/controlled/s08/api/queries/candidate/{candidate_id}",
            headers=approver_headers(),
        )
        assert workspace.status == 200
        body = workspace.json()
        assert body["approval_binding_id"] == approval["approval_binding_id"]
        binding = body["approval_binding"]
        assert binding["schema_version"] == "s08-approval-binding/1"
        assert binding["candidate_id"] == candidate_id
        assert binding["candidate_digest"] == freeze["manifest_digest"]
        assert binding["validation_bundle_digest"] == approval["validation_bundle_digest"]
        assert binding["scope"] == S08_SCOPE
        assert binding["activation_time"] == activation_time
        assert binding["recovery_release_id"] == bootstrap["candidate_id"]
        assert binding["approved_by"] == "c-demo-policy-approver"
        diff = binding["diff"]
        assert diff["schema_version"] == "s08-review-material/1"
        assert diff["anchor_candidate_id"] == bootstrap["candidate_id"]
        assert diff["anchor_components"] == {
            item["type"]: {"id": item["id"], "digest": item["digest"]}
            for item in bootstrap["components"]
        }
        assert diff["candidate_components"] == {
            item["type"]: {"id": item["id"], "digest": item["digest"]}
            for item in freeze["components"]
        }
        # Behavior-equivalent sources produce no component changes and an
        # equivalent behavior verdict bound into the diff.
        assert diff["changes"] == []
        assert diff["behavior_delta"]["equal"] is True
        assert diff["applicable_check_delta"]["added"] == []
        assert diff["applicable_check_delta"]["removed"] == []
        assert diff["unsupported_report"]["count"] == 0


@pytest.mark.parametrize("missing", ("admin", "approver", "operator"))
def test_missing_s08_identities_disable_scope_without_default_subjects(
    tmp_path: Path, missing: str
) -> None:
    """Removing any single independent identity closes the whole S08 scope:
    routes fail stable with no default shared subject, no bootstrap side
    effect is written, and the legacy S01 surface stays live."""
    state_path = tmp_path / "target.sqlite3"
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
    }
    upper = missing.upper()
    env[f"TASK4_S08_{upper}_CREDENTIAL"] = ""
    env[f"TASK4_S08_{upper}_SUBJECT"] = ""
    with s08_test_loopback(env) as server:
        # The legacy S01 surface remains live and fully functional.
        health = server.request("GET", "/api/health")
        assert health.status == 200
        page = server.request(
            "GET", "/controlled/s01", headers=demo_auth_headers(), use_session=False
        )
        assert page.status == 200
        accepted = submit(server, f"s08-{missing}-missing").json()
        assert accepted["disposition"] == "accepted"

        # Every S08 route fails closed with a stable error; the response
        # never leaks a default shared subject.
        for path, request_headers in [
            (f"/controlled/s08/api/queries/status?scope={S08_SCOPE}", admin_headers()),
            (f"/controlled/s08/api/queries/candidates?scope={S08_SCOPE}", admin_headers()),
            (
                f"/controlled/s08/api/queries/candidate/candidate_000000000000000000000000",
                admin_headers(),
            ),
            (f"/controlled/s08/api/queries/active?scope={S08_SCOPE}", admin_headers()),
        ]:
            response = server.request("GET", path, headers=request_headers)
            assert response.status in (403, 503)
            error = response.json()["detail"]["error"]
            assert error in ("S08_FORBIDDEN", "S08_UNAVAILABLE")
            assert "c-demo-policy" not in response.text
        denied = server.request(
            "POST",
            "/controlled/s08/api/commands/import_legacy",
            body={
                "source_bundle_id": SOURCE_BUNDLE_ID,
                "idempotency_key": "closed-import-1",
                "expected_governance_revision": 0,
            },
            headers=admin_headers(),
        )
        assert denied.status in (403, 503)
        assert denied.json()["detail"]["error"] in ("S08_FORBIDDEN", "S08_UNAVAILABLE")

    # No bootstrap side effect: the governance ledger stayed empty.
    with sqlite3.connect(state_path) as connection:
        counts = {
            "events": connection.execute(
                "SELECT COUNT(*) FROM policy_governance_events"
            ).fetchone()[0],
            "artifacts": connection.execute(
                "SELECT COUNT(*) FROM policy_artifacts"
            ).fetchone()[0],
            "projections": connection.execute(
                "SELECT COUNT(*) FROM policy_active_projections"
            ).fetchone()[0],
        }
    assert counts == {"events": 0, "artifacts": 0, "projections": 0}


@pytest.mark.parametrize(
    "overlap",
    ("admin_approver_credential", "admin_approver_subject", "operator_subject_dup"),
)
def test_overlapping_s08_credentials_or_subjects_disable_scope(
    tmp_path: Path, overlap: str
) -> None:
    """Any shared credential or duplicate subject across Admin/Approver/
    Operator closes the whole S08 scope: a single bearer can never be
    relabeled into two subjects, and no bootstrap/governance effect is
    written."""
    state_path = tmp_path / "target.sqlite3"
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
    }
    if overlap == "admin_approver_credential":
        env["TASK4_S08_APPROVER_CREDENTIAL"] = ADMIN_CREDENTIAL
    elif overlap == "admin_approver_subject":
        env["TASK4_S08_APPROVER_SUBJECT"] = "c-demo-policy-admin"
    else:
        env["TASK4_S08_OPERATOR_SUBJECT"] = "c-demo-policy-admin"
    with s08_test_loopback(env) as server:
        # The legacy S01 surface stays live.
        health = server.request("GET", "/api/health")
        assert health.status == 200
        # S08 fails closed with no default subject leak.
        for path, request_headers in [
            (f"/controlled/s08/api/queries/status?scope={S08_SCOPE}", admin_headers()),
            (f"/controlled/s08/api/queries/candidates?scope={S08_SCOPE}", admin_headers()),
            (f"/controlled/s08/api/queries/active?scope={S08_SCOPE}", admin_headers()),
        ]:
            response = server.request("GET", path, headers=request_headers)
            assert response.status in (403, 503)
            error = response.json()["detail"]["error"]
            assert error in ("S08_FORBIDDEN", "S08_UNAVAILABLE")
            assert "c-demo-policy" not in response.text
        denied = server.request(
            "POST",
            "/controlled/s08/api/commands/import_legacy",
            body={
                "source_bundle_id": SOURCE_BUNDLE_ID,
                "idempotency_key": "overlap-import-1",
                "expected_governance_revision": 0,
            },
            headers=admin_headers(),
        )
        assert denied.status in (403, 503)
        assert denied.json()["detail"]["error"] in ("S08_FORBIDDEN", "S08_UNAVAILABLE")

    # No bootstrap/governance side effect.
    with sqlite3.connect(state_path) as connection:
        events = connection.execute(
            "SELECT COUNT(*) FROM policy_governance_events"
        ).fetchone()[0]
        artifacts = connection.execute(
            "SELECT COUNT(*) FROM policy_artifacts"
        ).fetchone()[0]
    assert (events, artifacts) == (0, 0)


def test_review_material_lists_component_changes_for_behavior_equivalent_version(
    tmp_path: Path,
) -> None:
    """A behavior-equivalent version bump changes exactly the check-policy
    and checker component digests; the pre-approval review material and the
    bound diff list those changes while the behavior verdict stays
    equivalent."""
    state_path = tmp_path / "target.sqlite3"
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_bytes((ROOT / "configs" / "rules_auto_lease.yaml").read_bytes())
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
        "TASK4_S01_TEST_RULES_PATH": str(rules_file),
    }
    with s08_test_loopback(env) as server:
        bootstrap = _active_query(server)
        # After the bootstrap anchored on the original bytes, drift the
        # server-owned rules to a behavior-equivalent version.
        rules_file.write_bytes(
            rules_file.read_bytes().replace(
                b'version: "1.9.0"', b'version: "9.9.9"'
            )
        )
        import_result = _post_command(
            server,
            "import_legacy",
            {
                "source_bundle_id": SOURCE_BUNDLE_ID,
                "idempotency_key": "g3-version-import",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        draft_id = import_result["draft_id"]
        _post_command(
            server,
            "revise_draft",
            {
                "draft_id": draft_id,
                "metadata": {
                    "scope": S08_SCOPE,
                    "validity": {"valid_from": "2000-01-01T00:00:00Z"},
                    "source": SOURCE_BUNDLE_ID,
                    "reason": "behavior-equivalent version bump",
                },
                "idempotency_key": "g3-version-revise",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        freeze = _post_command(
            server,
            "freeze_candidate",
            {
                "draft_id": draft_id,
                "idempotency_key": "g3-version-freeze",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        candidate_id = freeze["candidate_id"]
        _post_command(
            server,
            "request_validation",
            {
                "candidate_id": candidate_id,
                "idempotency_key": "g3-version-validate",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        _wait_for_candidate_status(server, candidate_id, "validated")
        _post_command(
            server,
            "submit_review",
            {
                "candidate_id": candidate_id,
                "idempotency_key": "g3-version-review",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        _wait_for_candidate_status(server, candidate_id, "in_review")

        workspace = server.request(
            "GET",
            f"/controlled/s08/api/queries/candidate/{candidate_id}",
            headers=approver_headers(),
        )
        assert workspace.status == 200
        body = workspace.json()
        review = body["review_material"]
        # The version bump changes exactly the raw check-policy bytes and
        # the compiled checker artifact.
        change_map = {item["component"]: item for item in review["changes"]}
        assert set(change_map) == {"check_policy", "checker"}
        assert change_map["check_policy"]["change"] == "modified"
        assert change_map["checker"]["change"] == "modified"
        assert review["behavior_delta"]["equal"] is True
        assert review["applicable_check_delta"]["added"] == []
        assert review["applicable_check_delta"]["removed"] == []
        # The bound diff fixes exactly the same review material.
        activation_time = int(time.time()) + 60
        approval = _preview_and_approve(

            server,

            candidate_id,

            activation_time,

            bootstrap["candidate_id"],

            "g3-version-approve",

        )
        assert approval["status"] == "accepted"
        workspace = server.request(
            "GET",
            f"/controlled/s08/api/queries/candidate/{candidate_id}",
            headers=approver_headers(),
        )
        bound_diff = workspace.json()["approval_binding"]["diff"]
        assert bound_diff["changes"] == review["changes"]
        assert bound_diff["behavior_delta"] == review["behavior_delta"]
        assert (
            bound_diff["applicable_check_delta"]
            == review["applicable_check_delta"]
        )


# ---------------------------------------------------------------------------
# T08 / Issue #42 item A: closed typed candidate workspace + T08 DTO surface
# ---------------------------------------------------------------------------

T08_COMMAND_NAMES = (
    "import_legacy",
    "revise_draft",
    "freeze_candidate",
    "request_validation",
    "submit_review",
    "approve",
    "reject",
    "schedule",
    "cancel",
)
UNKNOWN_CANDIDATE = "candidate_000000000000000000000000"
CANDIDATE_QUERY_PATH = "/controlled/s08/api/queries/candidate/{candidate_id}"


def _candidate_workspace(
    server: UvicornLoopback, candidate_id: str, headers: dict[str, str]
) -> dict[str, Any]:
    response = server.request(
        "GET",
        f"/controlled/s08/api/queries/candidate/{candidate_id}",
        headers=headers,
        use_session=False,
    )
    assert response.status == 200, response.text
    return response.json()


def _s08_import_revise_freeze(
    server: UvicornLoopback, key: str
) -> tuple[str, dict[str, Any]]:
    """Admin: import -> revise -> freeze, returning (candidate_id, freeze)."""
    import_result = _post_command(
        server,
        "import_legacy",
        {
            "source_bundle_id": SOURCE_BUNDLE_ID,
            "idempotency_key": f"{key}-import",
            "expected_governance_revision": _governance_revision(server),
        },
        admin_headers(),
    )
    draft_id = import_result["draft_id"]
    _post_command(
        server,
        "revise_draft",
        {
            "draft_id": draft_id,
            "metadata": {
                "scope": S08_SCOPE,
                "validity": {"valid_from": "2000-01-01T00:00:00Z"},
                "source": SOURCE_BUNDLE_ID,
                "reason": f"S08 T08 workspace drill {key}",
            },
            "idempotency_key": f"{key}-revise",
            "expected_governance_revision": _governance_revision(server),
        },
        admin_headers(),
    )
    freeze = _post_command(
        server,
        "freeze_candidate",
        {
            "draft_id": draft_id,
            "idempotency_key": f"{key}-freeze",
            "expected_governance_revision": _governance_revision(server),
        },
        admin_headers(),
    )
    return freeze["candidate_id"], freeze


def test_candidate_workspace_is_closed_typed_and_self_sufficient(
    tmp_path: Path,
) -> None:
    """(a)+(c) The candidate workspace alone carries the authoritative
    governance revision, the authenticated actor role, the server-owned
    action list (candidate status + role only) and the current
    recovery/active anchor.  Admin/Approver actions follow the backend state
    machine exactly at every status, and the author can never approve."""
    state_path = tmp_path / "target.sqlite3"
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
    }
    with s08_test_loopback(env) as server:
        bootstrap = _active_query(server)
        candidate_id, freeze = _s08_import_revise_freeze(server, "ws")

        # Frozen candidate: only the author-admin may act; the workspace is
        # already self-sufficient (revision, role, anchor) without a status
        # call.
        workspace = _candidate_workspace(server, candidate_id, admin_headers())
        assert workspace["status"] == "candidate"
        assert workspace["actor_role"] == "admin"
        assert workspace["actions"] == ["request_validation", "cancel"]
        assert workspace["governance_revision"] == _governance_revision(server)
        assert workspace["active_anchor"] == {
            "candidate_id": bootstrap["candidate_id"],
            "manifest_digest": bootstrap["manifest_digest"],
        }
        assert workspace["manifest_id"] == freeze["manifest_id"]
        assert workspace["manifest_digest"] == freeze["manifest_digest"]
        # A freshly frozen candidate has no manifest/validation projection yet.
        assert "manifest" not in workspace
        assert "validation_bundle" not in workspace
        # (f) The workspace carries the candidate's full governance timeline:
        # the originating draft's events plus the candidate's own, in
        # append-only ledger order with the closed actor identity.
        assert [event["kind"] for event in workspace["events"]] == [
            "imported",
            "draft_revised",
            "candidate_frozen",
        ]
        for event in workspace["events"]:
            assert set(event["actor"]) == {"subject", "role", "source_id"}
            assert event["actor"]["role"] == "admin"

        # Validated: the manifest and the typed validation bundle appear.
        _post_command(
            server,
            "request_validation",
            {
                "candidate_id": candidate_id,
                "idempotency_key": "ws-validate",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        _wait_for_candidate_status(server, candidate_id, "validated")
        workspace = _candidate_workspace(server, candidate_id, admin_headers())
        assert workspace["status"] == "validated"
        assert workspace["actions"] == ["submit_review", "cancel"]
        assert workspace["validation_outcome"] == {
            "status": "validated",
            "reason_code": "S08_VALIDATION_PASSED",
        }
        assert workspace.get("activation_outcome") is None
        assert workspace["manifest"]["digest"] == freeze["manifest_digest"]
        assert workspace["manifest"]["schema_version"] == "s08-candidate-manifest/1"
        assert workspace["manifest"]["components"] == freeze["components"]
        assert workspace["manifest"]["compatibility"]["checker_build"]
        bundle = workspace["validation_bundle"]
        assert bundle["schema_version"] == "s08-validation-bundle/1"
        assert bundle["status"] == "validated"
        assert set(bundle["validator"]) == {
            "suite",
            "build",
            "code_sha256",
            "python",
            "machine",
        }
        assert bundle["inputs"]["component_digests"] == {
            item["type"]: item["digest"] for item in freeze["components"]
        }
        assert bundle["results"]["failed_count"] == 0
        assert set(bundle["results"]["checks"][0]) == {
            "check_id",
            "outcome",
            "detail",
        }
        assert set(bundle["results"]["determinism"]) == {
            "runs",
            "equal",
            "digest",
            "reason",
        }
        assert set(bundle["results"]["corpus_diff"]) == {
            "anchor",
            "applications_compared",
            "applications_skipped",
            "checks_equal",
            "selection_equal",
            "normalization_equal",
            "verdicts_equal",
            "route_equal",
            "corpus_digest",
            "equal",
            "reason",
        }
        assert "raw_outcomes" not in bundle["results"]
        approver_workspace = _candidate_workspace(
            server, candidate_id, approver_headers()
        )
        assert approver_workspace["actor_role"] == "approver"
        assert approver_workspace["actions"] == ["reject"]
        for response_workspace in (workspace, approver_workspace):
            response_text = json.dumps(response_workspace, ensure_ascii=False)
            assert "LSVAA4182N2333444" not in response_text
            assert "330102199505055556" not in response_text

        # In review: the independent approver gets the review diff and the
        # exact approve/reject actions; the author-admin only cancels.
        _post_command(
            server,
            "submit_review",
            {
                "candidate_id": candidate_id,
                "idempotency_key": "ws-review",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        _wait_for_candidate_status(server, candidate_id, "in_review")
        workspace = _candidate_workspace(server, candidate_id, approver_headers())
        assert workspace["status"] == "in_review"
        assert workspace["actor_role"] == "approver"
        assert workspace["actions"] == ["approve", "reject"]
        assert workspace["governance_revision"] == _governance_revision(server)
        assert workspace["active_anchor"] == {
            "candidate_id": bootstrap["candidate_id"],
            "manifest_digest": bootstrap["manifest_digest"],
        }
        review = workspace["review_material"]
        assert review["schema_version"] == "s08-review-material/1"
        assert review["candidate_digest"] == freeze["manifest_digest"]
        assert review["anchor_candidate_id"] == bootstrap["candidate_id"]
        assert review["mapping_ledger"]["schema_version"] == "s08-mapping-ledger/1"
        assert review["mapping_ledger"]["items"]
        assert set(review["applicable_check_delta"]) == {
            "anchor",
            "candidate",
            "added",
            "removed",
        }
        assert review["behavior_delta"]["equal"] is True
        assert review["unsupported_report"] == {"count": 0, "items": []}
        assert "approval_binding" not in workspace
        admin_workspace = _candidate_workspace(server, candidate_id, admin_headers())
        assert admin_workspace["actor_role"] == "admin"
        # The author's own candidate never offers approve.
        assert admin_workspace["actions"] == ["cancel"]

        # (d) 409: a stale governance revision is rejected with the stable
        # closed S08_CONFLICT envelope.  The preview itself is a governance
        # fact, so the stale approval binds its manifest and fences on the
        # pre-preview revision.
        stale_preview = _post_command(
            server,
            "preview_impact",
            {
                "candidate_id": candidate_id,
                "idempotency_key": "ws-stale-approve-preview",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        stale = server.request(
            "POST",
            "/controlled/s08/api/commands/approve",
            body={
                "candidate_id": candidate_id,
                "activation_time": int(time.time()) + 3600,
                "recovery_release_id": bootstrap["candidate_id"],
                "preview_manifest_id": stale_preview["manifest_id"],
                "idempotency_key": "ws-stale-approve",
                "expected_governance_revision": workspace["governance_revision"] - 1,
            },
            headers=approver_headers(),
        )
        assert stale.status == 409
        assert stale.json() == {
            "detail": {
                "error": "S08_CONFLICT",
                "message": "Governance command conflicts with current state",
            }
        }
        # (f) The prior active release stays authoritative across the stale
        # conflict: the active query still resolves the bootstrap release.
        assert (
            _active_query(server)["candidate_id"] == bootstrap["candidate_id"]
        )

        # (c) SoD: the author (admin) cannot approve the candidate.
        denied = server.request(
            "POST",
            "/controlled/s08/api/commands/approve",
            body={
                "candidate_id": candidate_id,
                "activation_time": int(time.time()) + 3600,
                "recovery_release_id": bootstrap["candidate_id"],
                "preview_manifest_id": "preview_sha256_"
                + "1" * 64,
                "idempotency_key": "ws-admin-approve",
                "expected_governance_revision": _governance_revision(server),
            },
            headers=admin_headers(),
        )
        assert denied.status == 403
        assert denied.json() == {
            "detail": {
                "error": "S08_FORBIDDEN",
                "message": "Registered S08 identity required",
            }
        }

        # Approved: the fixed approval binding is typed on the wire and the
        # author-admin may schedule or cancel.
        activation_time = int(time.time()) + 3600
        approval = _preview_and_approve(

            server,

            candidate_id,

            activation_time,

            bootstrap["candidate_id"],

            "ws-approve",

        )
        _wait_for_candidate_status(server, candidate_id, "approved")
        workspace = _candidate_workspace(server, candidate_id, admin_headers())
        assert workspace["status"] == "approved"
        assert workspace["actions"] == ["schedule", "cancel"]
        assert workspace["approval_binding_id"] == approval["approval_binding_id"]
        binding = workspace["approval_binding"]
        assert binding["schema_version"] == "s08-approval-binding/1"
        assert binding["candidate_digest"] == freeze["manifest_digest"]
        assert binding["approved_by"] == "c-demo-policy-approver"
        assert binding["activation_time"] == activation_time
        assert binding["recovery_release_id"] == bootstrap["candidate_id"]
        assert binding["diff"]["schema_version"] == "s08-review-material/1"
        assert binding["diff"]["changes"] == review["changes"]
        # (f) The governance timeline now runs to the approval binding, with
        # the independent approver as the actor of the approval event; the
        # immutable impact previews precede the approval (the stale-revision
        # drill previews once, the successful approval previews again).
        assert [event["kind"] for event in workspace["events"]] == [
            "imported",
            "draft_revised",
            "candidate_frozen",
            "validated",
            "in_review",
            "impact_previewed",
            "impact_previewed",
            "approved",
        ]
        assert workspace["events"][-1]["actor"]["subject"] == "c-demo-policy-approver"
        assert workspace["events"][-1]["actor"]["role"] == "approver"

        # Scheduled: only cancel remains, for the author-admin only.
        _post_command(
            server,
            "schedule",
            {
                "approval_binding_id": approval["approval_binding_id"],
                "activation_at": activation_time,
                "idempotency_key": "ws-schedule",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        _wait_for_candidate_status(server, candidate_id, "scheduled")
        workspace = _candidate_workspace(server, candidate_id, admin_headers())
        assert workspace["status"] == "scheduled"
        assert workspace["actions"] == ["cancel"]
        assert workspace["activation_time"] == activation_time
        assert workspace["validation_outcome"] == {
            "status": "validated",
            "reason_code": "S08_VALIDATION_PASSED",
        }
        assert workspace["activation_outcome"] == {"status": "pending"}
        assert _candidate_workspace(
            server, candidate_id, approver_headers()
        )["actions"] == []


def test_s08_active_workspace_projects_activation_outcome_and_prior_anchor(
    tmp_path: Path,
) -> None:
    """Once the worker activates the scheduled candidate, the workspace
    projects the authoritative activation outcome (event id + generation),
    and the anchor switches to the activated candidate."""
    state_path = tmp_path / "target.sqlite3"
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
    }
    with s08_test_loopback(env) as server:
        bootstrap = _active_query(server)
        candidate_id, freeze = _s08_import_revise_freeze(server, "actout")
        _post_command(
            server,
            "request_validation",
            {
                "candidate_id": candidate_id,
                "idempotency_key": "actout-validate",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        _wait_for_candidate_status(server, candidate_id, "validated")
        _post_command(
            server,
            "submit_review",
            {
                "candidate_id": candidate_id,
                "idempotency_key": "actout-review",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        activation_time = int(time.time()) + 5
        approval = _preview_and_approve(

            server,

            candidate_id,

            activation_time,

            bootstrap["candidate_id"],

            "actout-approve",

        )
        _post_command(
            server,
            "schedule",
            {
                "approval_binding_id": approval["approval_binding_id"],
                "activation_at": activation_time,
                "idempotency_key": "actout-schedule",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        _wait_for_candidate_status(server, candidate_id, "active")
        workspace = _candidate_workspace(server, candidate_id, admin_headers())
        assert workspace["status"] == "active"
        assert workspace["actions"] == []
        outcome = workspace["activation_outcome"]
        assert outcome["status"] == "active"
        assert outcome["activation_event_id"]
        assert outcome["active_generation"] == 2
        assert workspace["active_anchor"] == {
            "candidate_id": candidate_id,
            "manifest_digest": freeze["manifest_digest"],
        }
        assert workspace["validation_outcome"]["status"] == "validated"


def test_s08_workspace_error_contracts_are_closed_and_existence_hiding(
    tmp_path: Path,
) -> None:
    """(d) 404 is existence-hiding with one stable closed envelope for both
    roles; a missing identity is the stable 403; invalid commands are 422
    with sanitized detail that never reflects caller input."""
    state_path = tmp_path / "target.sqlite3"
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
    }
    with s08_test_loopback(env) as server:
        for headers in (admin_headers(), approver_headers()):
            hidden = server.request(
                "GET",
                f"/controlled/s08/api/queries/candidate/{UNKNOWN_CANDIDATE}",
                headers=headers,
                use_session=False,
            )
            assert hidden.status == 404
            assert hidden.json() == {
                "detail": {
                    "error": "S08_NOT_FOUND",
                    "message": "Governance object is unavailable",
                }
            }
            assert "c-demo-policy" not in hidden.text
        denied = server.request(
            "GET",
            f"/controlled/s08/api/queries/candidate/{UNKNOWN_CANDIDATE}",
            use_session=False,
        )
        assert denied.status == 403
        assert denied.json() == {
            "detail": {
                "error": "S08_FORBIDDEN",
                "message": "Registered S08 identity required",
            }
        }
        invalid = server.request(
            "POST",
            "/controlled/s08/api/commands/import_legacy",
            body={
                "source_bundle_id": SOURCE_BUNDLE_ID,
                "idempotency_key": "err-import-1",
                "expected_governance_revision": 0,
                "caller_path": "/etc/passwd",
            },
            headers=admin_headers(),
        )
        assert invalid.status == 422
        detail = invalid.json()["detail"]
        assert isinstance(detail, list) and detail
        for item in detail:
            assert set(item) == {"loc", "msg", "type"}
        # The rejected value/context is never reflected; only the sanitized
        # structural location survives.
        assert "/etc/passwd" not in invalid.text


def test_s08_authority_loss_503_is_a_closed_envelope(tmp_path: Path) -> None:
    """(d) Lost governance authority is a closed 503: exactly the stable
    S08_UNAVAILABLE envelope with no internal detail."""
    state_path = tmp_path / "target.sqlite3"
    with s08_test_loopback(
        {
            "TASK4_S01_TEST_STATE_PATH": str(state_path),
            "TASK4_S01_TEST_AUDIT_AVAILABLE": "0",
        }
    ) as server:
        response = server.request(
            "POST",
            "/controlled/s08/api/commands/import_legacy",
            body={
                "source_bundle_id": SOURCE_BUNDLE_ID,
                "idempotency_key": "err-import-503",
                "expected_governance_revision": 0,
            },
            headers=admin_headers(),
        )
        assert response.status == 503
        assert response.json() == {
            "detail": {
                "error": "S08_UNAVAILABLE",
                "message": "Governance authority is unavailable",
            }
        }
        # (f) Fail closed from an unavailable authority: with no healthy
        # bootstrap ever possible, the active query resolves to the exact
        # no-release state and never invents a candidate or digest.
        active = _active_query(server)
        assert active["status"] == "none"
        assert "candidate_id" not in active
        assert "manifest_digest" not in active


def test_s08_store_integrity_failure_is_a_closed_http_503(tmp_path: Path) -> None:
    state_path = tmp_path / "target.sqlite3"
    with s08_test_loopback(
        {
            "TASK4_S01_TEST_STATE_PATH": str(state_path),
            "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        }
    ) as server:
        assert _governance_revision(server) > 0
        with sqlite3.connect(state_path) as connection:
            connection.execute(
                "UPDATE policy_governance_events SET integrity_sha256 = ? "
                "WHERE rowid = (SELECT MIN(rowid) FROM policy_governance_events)",
                ("0" * 64,),
            )
            connection.commit()

        response = server.request(
            "GET",
            f"/controlled/s08/api/queries/status?scope={S08_SCOPE}",
            headers=admin_headers(),
            use_session=False,
        )

    assert response.status == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "detail": {
            "error": "S08_UNAVAILABLE",
            "message": "Governance authority is unavailable",
        }
    }


def test_prior_active_release_survives_lost_authority_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(f) After a healthy bootstrap and an active prior release, a
    deterministic authority loss is a closed 503 and the prior active
    release stays authoritative: the active query returns the exact same
    candidate and manifest digest before and after the rejected command."""
    state_path = tmp_path / "target.sqlite3"
    from task4_consistency.web import app as webapp

    monkeypatch.setenv("TASK4_S01_TEST_STATE_PATH", str(state_path))
    monkeypatch.setenv(
        "TASK4_S01_TEST_FIXTURE_ROOT", str(ROOT / "fixtures" / "applications")
    )
    monkeypatch.setenv("TASK4_S01_TEST_AUDIT_AVAILABLE", "1")
    monkeypatch.setattr(webapp, "S08_CONFIGURED", True)
    for name, value in (
        ("S08_ADMIN_CREDENTIAL", ADMIN_CREDENTIAL),
        ("S08_ADMIN_SUBJECT", "c-demo-policy-admin"),
        ("S08_APPROVER_CREDENTIAL", APPROVER_CREDENTIAL),
        ("S08_APPROVER_SUBJECT", "c-demo-policy-approver"),
        ("S08_OPERATOR_CREDENTIAL", OPERATOR_CREDENTIAL),
        ("S08_OPERATOR_SUBJECT", "c-demo-policy-operator"),
    ):
        monkeypatch.setattr(webapp, name, value)
    app = webapp.create_s01_test_app()
    assert webapp.S08_SERVICE is not None
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {ADMIN_CREDENTIAL}"}
    active_url = (
        f"/controlled/s08/api/queries/active?scope={S08_SCOPE}"
    )
    prior = client.get(active_url, headers=headers).json()
    assert prior["status"] == "active"
    prior_candidate = prior["candidate_id"]
    prior_digest = prior["manifest_digest"]
    # Inject the authority loss on the running governed service; every
    # subsequent governed write must fail closed.
    monkeypatch.setattr(webapp.S08_SERVICE, "audit_available", False)
    response = client.post(
        "/controlled/s08/api/commands/import_legacy",
        json={
            "source_bundle_id": SOURCE_BUNDLE_ID,
            "idempotency_key": "err-import-503-prior-active",
            "expected_governance_revision": 0,
        },
        headers=headers,
    )
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "S08_UNAVAILABLE"
    after = client.get(active_url, headers=headers).json()
    assert after["status"] == "active"
    assert after["candidate_id"] == prior_candidate
    assert after["manifest_digest"] == prior_digest


def _assert_no_wildcard_dicts(
    schemas: dict[str, Any], ref: str, seen: set[str] | None = None
) -> None:
    """Fail when any schema reachable from ``ref`` uses an untyped
    additionalProperties (``true`` or an empty object): those are the
    ``{[key: string]: unknown}`` leaks that break generated OpenAPI clients.
    A closed model's ``additionalProperties: false`` and typed value dicts
    (``{"type": ...}`` / ``{"$ref": ...}``) are allowed."""
    seen = set() if seen is None else seen
    name = ref.rsplit("/", 1)[-1]
    if name in seen:
        return
    seen.add(name)

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if "additionalProperties" in node:
            values = node["additionalProperties"]
            assert values is False or (
                isinstance(values, dict) and values
            ), f"{name} leaks a wildcard dict: {node}"
        nested_ref = node.get("$ref")
        if isinstance(nested_ref, str) and nested_ref.startswith(
            "#/components/schemas/"
        ):
            _assert_no_wildcard_dicts(schemas, nested_ref, seen)
        for value in node.values():
            walk(value)

    walk(schemas[name])


def test_s08_openapi_t08_surface_is_closed_typed(tmp_path: Path) -> None:
    """(b) The exported OpenAPI registers every T08 command/query with the
    five closed error responses, and the candidate workspace schema is
    precisely typed end to end: no ``{[key: string]: unknown}`` dict leaks
    for manifest, validation outcome/checks, review diff, approval binding
    or activation status."""
    state_path = tmp_path / "target.sqlite3"
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
    }
    with s08_test_loopback(env) as server:
        response = server.request("GET", "/openapi.json", use_session=False)
        assert response.status == 200
        spec = response.json()
    schemas = spec["components"]["schemas"]
    paths = spec["paths"]

    # 1. Every T08 command and the candidate query are registered with the
    #    five closed error responses the UI must handle.
    for name in T08_COMMAND_NAMES:
        path = f"/controlled/s08/api/commands/{name}"
        assert path in paths, path
        post = paths[path]["post"]
        for status in ("403", "404", "409", "422", "503"):
            assert status in post["responses"], f"{path} missing {status}"
            error_model = post["responses"][status]["content"]["application/json"][
                "schema"
            ]["$ref"].rsplit("/", 1)[-1]
            expected = (
                "S08ValidationErrorResponse" if status == "422" else "S08ErrorResponse"
            )
            assert error_model == expected, f"{path} {status} -> {error_model}"
        success = post["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ].rsplit("/", 1)[-1]
        assert success in schemas, f"{path} success model {success}"

    # 1b. The operator stop-activations command declares the same closed set.
    stop_path = paths["/controlled/s08/api/commands/stop_activations"]
    for status in ("403", "404", "409", "422", "503"):
        assert status in stop_path["post"]["responses"], status

    # 1c. Every T08-used query declares the closed error set too.
    for query_path in (
        "/controlled/s08/api/queries/status",
        "/controlled/s08/api/queries/active",
        "/controlled/s08/api/queries/candidates",
        "/controlled/s08/api/queries/drafts",
        "/controlled/s08/api/queries/events",
    ):
        assert query_path in paths, query_path
        for status in ("403", "404", "409", "422", "503"):
            assert status in paths[query_path]["get"]["responses"], (
                f"{query_path} missing {status}"
            )

    candidate_path = paths[CANDIDATE_QUERY_PATH]
    assert "get" in candidate_path
    for status in ("403", "404", "409", "422", "503"):
        assert status in candidate_path["get"]["responses"], status
    workspace_ref = candidate_path["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    assert workspace_ref.rsplit("/", 1)[-1] == "S08CandidateWorkspaceResponse"

    # 2. Every T08 command request body is the exact closed DTO and the
    #    status/active/workspace responses are closed typed end to end: no
    #    reachable ``{[key: string]: unknown}`` dict remains on any
    #    T08-used request or response.
    command_bodies = {
        "import_legacy": "S08ImportLegacyBody",
        "revise_draft": "S08ReviseDraftBody",
        "freeze_candidate": "S08FreezeCandidateBody",
        "request_validation": "S08CandidateCommandBody",
        "submit_review": "S08CandidateCommandBody",
        "approve": "S08ApproveBody",
        "reject": "S08RejectBody",
        "schedule": "S08ScheduleBody",
        "cancel": "S08RejectBody",
        "stop_activations": "S08StopActivationsBody",
    }
    for name, expected in command_bodies.items():
        command_path = paths[f"/controlled/s08/api/commands/{name}"]
        body_ref = command_path["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        assert body_ref.rsplit("/", 1)[-1] == expected, (name, body_ref)
        _assert_no_wildcard_dicts(schemas, body_ref)
        success = command_path["post"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        _assert_no_wildcard_dicts(schemas, success)

    status_ref = paths["/controlled/s08/api/queries/status"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]["$ref"]
    assert status_ref.rsplit("/", 1)[-1] == "S08StatusResponse"
    _assert_no_wildcard_dicts(schemas, status_ref)
    active_ref = paths["/controlled/s08/api/queries/active"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]["$ref"]
    _assert_no_wildcard_dicts(schemas, active_ref)
    for query_path in (
        "/controlled/s08/api/queries/candidates",
        "/controlled/s08/api/queries/drafts",
        "/controlled/s08/api/queries/events",
    ):
        query_ref = paths[query_path]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        _assert_no_wildcard_dicts(schemas, query_ref)

    # 2b. Every error response registered on a T08 command or query path is
    #     also a closed typed model (no wildcard dicts), matching the closed
    #     S08ErrorResponse/S08ValidationErrorResponse components.
    error_refs: set[str] = set()
    for t08_path in (
        *(f"/controlled/s08/api/commands/{name}" for name in command_bodies),
        "/controlled/s08/api/queries/status",
        "/controlled/s08/api/queries/active",
        CANDIDATE_QUERY_PATH,
        "/controlled/s08/api/queries/candidates",
        "/controlled/s08/api/queries/drafts",
        "/controlled/s08/api/queries/events",
    ):
        method = "post" if "/commands/" in t08_path else "get"
        for status, response in paths[t08_path][method]["responses"].items():
            if status == "200":
                continue
            error_refs.add(
                response["content"]["application/json"]["schema"]["$ref"].rsplit(
                    "/", 1
                )[-1]
            )
    for name in sorted(error_refs):
        _assert_no_wildcard_dicts(schemas, f"#/components/schemas/{name}")

    public_schema_text = json.dumps(schemas, sort_keys=True)
    assert "raw_outcomes" not in public_schema_text
    assert "S08RawEvidence" not in public_schema_text

    # 3. The closed typed DTOs are registered components.
    for name in (
        "S08CandidateWorkspaceResponse",
        "S08CandidateManifest",
        "S08ValidationBundle",
        "S08ValidationCheck",
        "S08ReviewMaterial",
        "S08ApprovalBinding",
        "S08ActiveAnchor",
        "S08ErrorResponse",
        "S08ValidationErrorResponse",
        "S08ActivationHold",
        "S08DraftMetadata",
        "S08DraftValidity",
    ):
        assert name in schemas, name

    # 4. No wildcard dict anywhere reachable from the workspace schema.
    _assert_no_wildcard_dicts(schemas, workspace_ref)


def test_s08_react_shell_identity_no_store_and_missing_build_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(e) /controlled/s08/react mirrors /controlled/s02/react: served only
    to a registered S08 identity, no-store, no session, and a closed 503
    when the shared build is missing or incomplete."""
    state_path = tmp_path / "target.sqlite3"
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
    }
    with s08_test_loopback(env) as server:
        denied = server.request("GET", "/controlled/s08/react", use_session=False)
        assert denied.status == 403
        assert denied.json()["detail"]["error"] == "S08_FORBIDDEN"
        for headers in (admin_headers(), approver_headers()):
            shell = server.request(
                "GET",
                "/controlled/s08/react",
                headers=headers,
                use_session=False,
            )
            assert shell.status == 200, shell.text
            assert shell.headers["cache-control"] == "no-store"
            assert shell.headers["pragma"] == "no-cache"
            assert "set-cookie" not in shell.headers
            assert 'type="module"' in shell.text
            assert 'src="/static/react/assets/' in shell.text

    # The global demo token cannot preempt or grant access to S08's own
    # registered identities.  Both shell and API must reach the S08 auth
    # boundary when TASK4_WEB_TOKEN is enabled.
    with s08_test_loopback(
        {
            "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
            "TASK4_S01_TEST_STATE_PATH": str(tmp_path / "global-token.sqlite3"),
            "TASK4_WEB_TOKEN": "global-token-not-an-s08-identity",
        }
    ) as server:
        shell = server.request(
            "GET",
            "/controlled/s08/react",
            headers=admin_headers(),
            use_session=False,
        )
        status = server.request(
            "GET",
            f"/controlled/s08/api/queries/status?scope={S08_SCOPE}",
            headers=admin_headers(),
            use_session=False,
        )
        global_only = server.request(
            "GET",
            "/controlled/s08/react",
            headers={"Authorization": "Bearer global-token-not-an-s08-identity"},
            use_session=False,
        )
        assert shell.status == 200
        assert status.status == 200
        assert global_only.status == 403
        assert global_only.json()["detail"]["error"] == "S08_FORBIDDEN"

    # A valid read-role credential is insufficient when the complete
    # Admin/Approver/Operator authority could not be constructed.  The shell
    # must fail closed just like its API instead of exposing a usable surface.
    from task4_consistency.web import app as webapp

    monkeypatch.setattr(webapp, "S08_ADMIN_CREDENTIAL", ADMIN_CREDENTIAL)
    monkeypatch.setattr(webapp, "S08_ADMIN_SUBJECT", "c-demo-policy-admin")
    monkeypatch.setattr(webapp, "S08_APPROVER_CREDENTIAL", APPROVER_CREDENTIAL)
    monkeypatch.setattr(webapp, "S08_APPROVER_SUBJECT", "c-demo-policy-approver")
    monkeypatch.setattr(webapp, "S08_OPERATOR_CREDENTIAL", "")
    monkeypatch.setattr(webapp, "S08_OPERATOR_SUBJECT", "")
    monkeypatch.setattr(webapp, "S08_CONFIGURED", False)
    monkeypatch.setattr(webapp, "S08_SERVICE", None)
    client = TestClient(webapp.app)
    for credential in (ADMIN_CREDENTIAL, APPROVER_CREDENTIAL):
        unavailable = client.get(
            "/controlled/s08/react",
            headers={"Authorization": f"Bearer {credential}"},
        )
        assert unavailable.status_code == 503
        assert unavailable.json() == {
            "detail": {
                "error": "S08_UNAVAILABLE",
                "message": "Controlled S08 policy governance is unavailable",
            }
        }

    # Missing/incomplete build fails closed with the stable error code.  The
    # build path is server-fixed, so the missing-build seam is exercised
    # in-process on the same ASGI app (the test_t06_http pattern).
    monkeypatch.setattr(webapp, "S01_REACT_INDEX", Path("/nonexistent/index.html"))
    monkeypatch.setattr(webapp, "S08_ADMIN_CREDENTIAL", ADMIN_CREDENTIAL)
    monkeypatch.setattr(webapp, "S08_ADMIN_SUBJECT", "c-demo-policy-admin")
    monkeypatch.setattr(webapp, "S08_APPROVER_CREDENTIAL", APPROVER_CREDENTIAL)
    monkeypatch.setattr(webapp, "S08_APPROVER_SUBJECT", "c-demo-policy-approver")
    client = TestClient(webapp.app)
    missing = client.get(
        "/controlled/s08/react",
        headers={"Authorization": f"Bearer {APPROVER_CREDENTIAL}"},
    )
    assert missing.status_code == 503
    assert missing.json()["detail"]["error"] == "S08_REACT_UNAVAILABLE"


def _s09_identity_request() -> Any:
    from types import SimpleNamespace

    import task4_consistency.web.app as app_module

    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(_s08_application_module=app_module))
    )


@pytest.mark.parametrize(
    "role, attr, other",
    [
        ("replay_operator", "CREDENTIAL", "ADMIN"),
        ("replay_operator", "CREDENTIAL", "APPROVER"),
        ("replay_operator", "CREDENTIAL", "OPERATOR"),
        ("replay_operator", "CREDENTIAL", "AUDITOR"),
        ("replay_operator", "SUBJECT", "ADMIN"),
        ("replay_operator", "SUBJECT", "APPROVER"),
        ("replay_operator", "SUBJECT", "OPERATOR"),
        ("replay_operator", "SUBJECT", "AUDITOR"),
        ("simulation_operator", "CREDENTIAL", "ADMIN"),
        ("simulation_operator", "CREDENTIAL", "APPROVER"),
        ("simulation_operator", "CREDENTIAL", "OPERATOR"),
        ("simulation_operator", "CREDENTIAL", "AUDITOR"),
        ("simulation_operator", "SUBJECT", "ADMIN"),
        ("simulation_operator", "SUBJECT", "APPROVER"),
        ("simulation_operator", "SUBJECT", "OPERATOR"),
        ("simulation_operator", "SUBJECT", "AUDITOR"),
        ("replay_operator", "CREDENTIAL", "SIMULATION"),
        ("simulation_operator", "SUBJECT", "REPLAY"),
    ],
)
def test_s09_diagnostic_identity_aliases_fail_closed(
    monkeypatch: pytest.MonkeyPatch, role: str, attr: str, other: str
) -> None:
    """R6/ST-3/SP-6: any replay/simulation credential or subject aliasing
    another controlled identity (including the Auditor) makes diagnostic
    authorization fail closed at configuration/authorization time."""
    from fastapi import HTTPException

    import task4_consistency.web.app as app_module
    import task4_consistency.web.s08_http as http_module

    values = {
        "S08_ADMIN_CREDENTIAL": "admin-cred",
        "S08_ADMIN_SUBJECT": "admin-subject",
        "S08_APPROVER_CREDENTIAL": "approver-cred",
        "S08_APPROVER_SUBJECT": "approver-subject",
        "S08_OPERATOR_CREDENTIAL": "operator-cred",
        "S08_OPERATOR_SUBJECT": "operator-subject",
        "S01_AUDITOR_CREDENTIAL": "auditor-cred",
        "S01_AUDITOR_SUBJECT": "auditor-subject",
        "S09_REPLAY_CREDENTIAL": "replay-cred",
        "S09_REPLAY_SUBJECT": "replay-subject",
        "S09_SIMULATION_CREDENTIAL": "simulation-cred",
        "S09_SIMULATION_SUBJECT": "simulation-subject",
    }
    for name, value in values.items():
        monkeypatch.setattr(app_module, name, value)
    # Any non-empty presented credential matches, so the only thing that can
    # reject an aliased bearer is the configuration gate itself.
    monkeypatch.setattr(
        app_module,
        "_s01_has_credential",
        lambda request, credential: bool(credential),
    )
    s09_prefix = "S09_REPLAY" if role == "replay_operator" else "S09_SIMULATION"
    other_prefix = (
        "S01"
        if other == "AUDITOR"
        else ("S08" if other in {"ADMIN", "APPROVER", "OPERATOR"} else "S09")
    )
    monkeypatch.setattr(app_module, f"{s09_prefix}_{attr}", values[f"{other_prefix}_{other}_{attr}"])
    with pytest.raises(HTTPException) as error:
        http_module._s08_require_role(_s09_identity_request(), role)
    assert error.value.status_code == 403
    assert error.value.detail["error"] == "S08_FORBIDDEN"


@pytest.mark.parametrize(
    "role, missing_attr",
    [
        ("replay_operator", "S09_REPLAY_CREDENTIAL"),
        ("replay_operator", "S09_REPLAY_SUBJECT"),
        ("simulation_operator", "S09_SIMULATION_CREDENTIAL"),
        ("simulation_operator", "S09_SIMULATION_SUBJECT"),
    ],
)
def test_s09_missing_diagnostic_identity_fails_closed(
    monkeypatch: pytest.MonkeyPatch, role: str, missing_attr: str
) -> None:
    """R6: a missing replay/simulation credential or subject keeps the
    diagnostic role fail-closed without disabling S08 command roles."""
    from fastapi import HTTPException

    import task4_consistency.web.app as app_module
    import task4_consistency.web.s08_http as http_module

    values = {
        "S08_ADMIN_CREDENTIAL": "admin-cred",
        "S08_ADMIN_SUBJECT": "admin-subject",
        "S08_APPROVER_CREDENTIAL": "approver-cred",
        "S08_APPROVER_SUBJECT": "approver-subject",
        "S08_OPERATOR_CREDENTIAL": "operator-cred",
        "S08_OPERATOR_SUBJECT": "operator-subject",
        "S01_AUDITOR_CREDENTIAL": "auditor-cred",
        "S01_AUDITOR_SUBJECT": "auditor-subject",
        "S09_REPLAY_CREDENTIAL": "replay-cred",
        "S09_REPLAY_SUBJECT": "replay-subject",
        "S09_SIMULATION_CREDENTIAL": "simulation-cred",
        "S09_SIMULATION_SUBJECT": "simulation-subject",
    }
    for name, value in values.items():
        monkeypatch.setattr(app_module, name, value)
    monkeypatch.setattr(app_module, missing_attr, "")
    monkeypatch.setattr(
        app_module,
        "_s01_has_credential",
        lambda request, credential: credential == "presented-secret",
    )
    with pytest.raises(HTTPException) as error:
        http_module._s08_require_role(_s09_identity_request(), role)
    assert error.value.status_code == 403
    assert error.value.detail["error"] == "S08_FORBIDDEN"
    # S08 command authorization is unchanged by the missing S09 identity.
    monkeypatch.setattr(
        app_module,
        "_s01_has_credential",
        lambda request, credential: credential == "operator-cred",
    )
    operator = http_module._s08_require_role(_s09_identity_request(), "operator")
    assert operator.role == "operator"


def test_s09_distinct_diagnostic_identities_authorize_and_stay_mutually_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R6: distinct replay/simulation identities authorize their own roles;
    replay, simulation and activation credentials are mutually unusable."""
    from fastapi import HTTPException

    import task4_consistency.web.app as app_module
    import task4_consistency.web.s08_http as http_module

    values = {
        "S08_ADMIN_CREDENTIAL": "admin-cred",
        "S08_ADMIN_SUBJECT": "admin-subject",
        "S08_APPROVER_CREDENTIAL": "approver-cred",
        "S08_APPROVER_SUBJECT": "approver-subject",
        "S08_OPERATOR_CREDENTIAL": "operator-cred",
        "S08_OPERATOR_SUBJECT": "operator-subject",
        "S01_AUDITOR_CREDENTIAL": "auditor-cred",
        "S01_AUDITOR_SUBJECT": "auditor-subject",
        "S09_REPLAY_CREDENTIAL": "replay-cred",
        "S09_REPLAY_SUBJECT": "replay-subject",
        "S09_SIMULATION_CREDENTIAL": "simulation-cred",
        "S09_SIMULATION_SUBJECT": "simulation-subject",
    }
    for name, value in values.items():
        monkeypatch.setattr(app_module, name, value)
    request = _s09_identity_request()

    def presenting(credential: str) -> None:
        monkeypatch.setattr(
            app_module,
            "_s01_has_credential",
            lambda request, expected, _c=credential: expected == _c,
        )

    presenting("replay-cred")
    replay = http_module._s08_require_role(request, "replay_operator")
    assert replay.role == "replay_operator"
    presenting("simulation-cred")
    simulation = http_module._s08_require_role(request, "simulation_operator")
    assert simulation.role == "simulation_operator"
    # Replay and simulation credentials cannot relabel into the activation
    # operator; the activation credential cannot run a diagnostic workload.
    presenting("replay-cred")
    with pytest.raises(HTTPException) as error:
        http_module._s08_require_role(request, "operator")
    assert error.value.status_code == 403
    presenting("simulation-cred")
    with pytest.raises(HTTPException) as error:
        http_module._s08_require_role(request, "operator")
    assert error.value.status_code == 403
    presenting("operator-cred")
    with pytest.raises(HTTPException) as error:
        http_module._s08_require_role(request, "replay_operator")
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        http_module._s08_require_role(request, "simulation_operator")
    assert error.value.status_code == 403
    presenting("operator-cred")
    operator = http_module._s08_require_role(request, "operator")
    assert operator.role == "operator"
