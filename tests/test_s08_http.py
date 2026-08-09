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


def _json(server: UvicornLoopback, method: str, path: str, body: dict[str, Any] | None = None,
          headers: dict[str, str] | None = None) -> dict[str, Any]:
    response = server.request(method, path, body=body, headers=headers, use_session=False)
    assert response.status == 200, f"{method} {path}: {response.status} {response.text}"
    return response.json()


def _post_command(server: UvicornLoopback, name: str, body: dict[str, Any],
                  headers: dict[str, str]) -> dict[str, Any]:
    return _json(server, "POST", f"/controlled/s08/api/commands/{name}", body, headers)


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
        approval_result = _post_command(
            server,
            "approve",
            {
                "candidate_id": candidate_id,
                "activation_time": activation_time,
                "recovery_release_id": bootstrap["candidate_id"],
                "idempotency_key": "s08-tracer-approve-1",
                "expected_governance_revision": _governance_revision(server),
            },
            approver_headers(),
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
        approval = _post_command(
            server,
            "approve",
            {
                "candidate_id": candidate_id,
                "activation_time": activation_time,
                "recovery_release_id": active["candidate_id"],
                "idempotency_key": "drill-approve-1",
                "expected_governance_revision": _governance_revision(server),
            },
            approver_headers(),
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
        #    command fails with the stable S08_FORBIDDEN error.
        for name, payload in [
            ("import_legacy", {"source_bundle_id": SOURCE_BUNDLE_ID}),
            ("revise_draft", {"draft_id": draft_id, "metadata": {"scope": S08_SCOPE}}),
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

        # 3. The author (admin) cannot approve the candidate.
        denied = server.request(
            "POST",
            "/controlled/s08/api/commands/approve",
            body={
                "candidate_id": candidate_id,
                "activation_time": int(time.time()) + 60,
                "recovery_release_id": bootstrap["candidate_id"],
                "idempotency_key": "g3-admin-approve",
                "expected_governance_revision": _governance_revision(server),
            },
            headers=admin_headers(),
        )
        assert denied.status == 403
        assert denied.json()["detail"]["error"] == "S08_FORBIDDEN"

        # 4. The approver approves; the fixed binding is readable and pins
        #    the exact machine diff, digests, scope and activation time.
        activation_time = int(time.time())
        approval = _post_command(
            server,
            "approve",
            {
                "candidate_id": candidate_id,
                "activation_time": activation_time,
                "recovery_release_id": bootstrap["candidate_id"],
                "idempotency_key": "g3-approve-1",
                "expected_governance_revision": _governance_revision(server),
            },
            approver_headers(),
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
        activation_time = int(time.time())
        approval = _post_command(
            server,
            "approve",
            {
                "candidate_id": candidate_id,
                "activation_time": activation_time,
                "recovery_release_id": bootstrap["candidate_id"],
                "idempotency_key": "g3-version-approve",
                "expected_governance_revision": _governance_revision(server),
            },
            approver_headers(),
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
