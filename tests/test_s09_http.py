"""S09 policy impact / safety hold / rollback public HTTP contract tests.

The acceptance seam is a real uvicorn loopback process (same policy as the
S01/S08 suites): governance impact preview, envelope-bound approval,
activation-time final impact, Lifecycle per-application disposition and
Operational Re-evaluation are exercised over HTTP with distinct identities,
and currentness is observed through the S01 application query seam.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from tests.test_s01_http import (
    UvicornLoopback,
    auditor_auth_headers,
    demo_auth_headers,
)
from tests.test_s08_http import (
    ADMIN_CREDENTIAL,
    APPROVER_CREDENTIAL,
    OPERATOR_CREDENTIAL,
    S08_SCOPE,
    SOURCE_BUNDLE_ID,
    _governance_revision,
    _post_command,
    _wait_for_active_generation,
    _wait_for_candidate_status,
    _wait_for_complete_run,
    admin_headers,
    approver_headers,
    operator_headers,
    replay_headers,
    s08_test_loopback,
    simulation_headers,
)

ROOT = Path(__file__).resolve().parents[1]
_POLL_TIMEOUT_SECONDS = 8.0


def _json(
    server: UvicornLoopback,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    use_session: bool = False,
) -> tuple[int, dict[str, Any]]:
    response = server.request(
        method, path, body=body, headers=headers, use_session=use_session
    )
    return response.status, response.json()


def _command(
    server: UvicornLoopback,
    name: str,
    body: dict[str, Any],
    headers: dict[str, str],
) -> tuple[int, dict[str, Any]]:
    """S09 commands live on their own router; the S08 approve command keeps
    its original path."""
    prefix = (
        "/controlled/s08"
        if name in {"approve", "schedule"}
        else "/controlled/s09"
    )
    return _json(
        server, "POST", f"{prefix}/api/commands/{name}", body, headers
    )


def _submit_application(server: UvicornLoopback, key: str) -> str:
    server.open_s01_session()
    response = server.request(
        "POST",
        "/controlled/s01/api/commands/submit",
        body={"scenario_id": "app_r53_bad_engine.json", "idempotency_key": key},
        headers=demo_auth_headers(),
    )
    assert response.status == 200, f"submit failed: {response.status} {response.text}"
    return response.json()["application_id"]


def _new_candidate_in_review(server: UvicornLoopback) -> str:
    """Drive the ordinary S08 pipeline to a candidate awaiting independent
    review (import -> revise -> freeze -> validated -> in_review)."""
    import_result = _post_command(
        server,
        "import_legacy",
        {
            "source_bundle_id": SOURCE_BUNDLE_ID,
            "idempotency_key": "s09-rg1-import",
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
                "reason": "S09 changed governed release",
            },
            "idempotency_key": "s09-rg1-revise",
            "expected_governance_revision": _governance_revision(server),
        },
        admin_headers(),
    )
    freeze_result = _post_command(
        server,
        "freeze_candidate",
        {
            "draft_id": draft_id,
            "idempotency_key": "s09-rg1-freeze",
            "expected_governance_revision": _governance_revision(server),
        },
        admin_headers(),
    )
    candidate_id = freeze_result["candidate_id"]
    _post_command(
        server,
        "request_validation",
        {
            "candidate_id": candidate_id,
            "idempotency_key": "s09-rg1-validate",
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
            "idempotency_key": "s09-rg1-review",
            "expected_governance_revision": _governance_revision(server),
        },
        admin_headers(),
    )
    _wait_for_candidate_status(server, candidate_id, "in_review")
    return candidate_id


def _impact_dispositions(
    server: UvicornLoopback, final_impact_digest: str
) -> dict[str, Any]:
    """The authorized audit/reconciliation route: per-member receipts.  The
    ordinary Reviewer route exposes only the aggregate summary."""
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status, result = _json(
            server,
            "GET",
            "/controlled/s01/api/queries/impact-dispositions/reconciliation?"
            f"final_impact_digest={final_impact_digest}",
            headers=auditor_auth_headers(),
        )
        if status == 200:
            return result
        time.sleep(0.05)
    raise AssertionError(f"impact-dispositions never became available: {status} {result}")


def _wait_for_impact_disposition(
    server: UvicornLoopback,
    final_impact_digest: str,
    application_id: str,
    expected: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        result = _impact_dispositions(server, final_impact_digest)
        member = next(
            (
                item
                for item in result["members"]
                if item["application_id"] == application_id
            ),
            None,
        )
        if member is not None and member["disposition"] == expected:
            return member
        time.sleep(0.05)
    raise AssertionError(
        f"member {application_id} did not reach disposition {expected}: {result}"
    )


def test_rg1_changed_release_stales_affected_current_run_and_creates_one_reevaluation(
    tmp_path: Path,
) -> None:
    """The minimal S09 vertical slice over the real public seams.

    A governed release change goes through impact preview, independent
    envelope-bound approval, schedule and activation-time final impact; the
    new activation generation must immediately derive the existing current
    run of an open-cycle application as non-current, and Lifecycle must
    consume the final-impact fact into exactly one applied disposition and
    one Operational Re-evaluation job.
    """
    state_path = tmp_path / "target.sqlite3"
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
    }
    with s08_test_loopback(env) as server:
        # 0. The one-time bootstrap migration release is active at startup.
        active = _json(
            server,
            "GET",
            f"/controlled/s08/api/queries/active?scope={S08_SCOPE}",
            headers=admin_headers(),
        )[1]
        assert active["status"] == "active"
        assert active["bootstrap"] is True
        assert active["active_generation"] == 1
        bootstrap_candidate_id = active["candidate_id"]

        # 1. An open-cycle application completes a current run under
        #    generation 1 of the governed release.
        application_id = _submit_application(server, "s09-rg1-submit-1")
        run = _wait_for_complete_run(server, application_id)
        assert run["status"] == "complete"
        assert run["current"] is True
        assert run["active_generation"] == 1

        # 2. A changed governed release reaches independent review.
        candidate_id = _new_candidate_in_review(server)

        # 3. Preview the conservative impact of the changed release.
        status, preview = _command(
            server,
            "preview_impact",
            {
                "candidate_id": candidate_id,
                "idempotency_key": "s09-rg1-preview",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        assert status == 200, f"preview_impact: {status} {preview}"
        assert preview["status"] == "accepted"
        preview_manifest_id = preview["manifest_id"]
        preview_digest = preview["digest"]
        assert len(preview_digest) == 64
        assert preview["phase"] == "preview"
        assert preview["member_count"] >= 1
        assert preview["zero_hit_proof"] is False
        # The read-only preview is also available to the independent Policy
        # Approver, who binds its exact digest at approval time.
        status, approver_preview = _command(
            server,
            "preview_impact",
            {
                "candidate_id": candidate_id,
                "idempotency_key": "s09-rg1-preview-approver",
                "expected_governance_revision": _governance_revision(server),
            },
            approver_headers(),
        )
        assert status == 200, approver_preview
        assert approver_preview["manifest_id"]
        assert len(approver_preview["digest"]) == 64
        bound_preview_manifest_id = approver_preview["manifest_id"]
        bound_preview_digest = approver_preview["digest"]

        # 4. The independent Policy Approver binds the preview envelope.
        activation_time = int(time.time()) + 5
        status, approval = _command(
            server,
            "approve",
            {
                "candidate_id": candidate_id,
                "activation_time": activation_time,
                "recovery_release_id": bootstrap_candidate_id,
                "preview_manifest_id": bound_preview_manifest_id,
                "idempotency_key": "s09-rg1-approve",
                "expected_governance_revision": _governance_revision(server),
            },
            approver_headers(),
        )
        assert status == 200, f"approve: {status} {approval}"
        assert approval["status"] == "accepted"
        assert approval["preview_manifest_id"] == bound_preview_manifest_id
        assert approval["preview_manifest_digest"] == bound_preview_digest
        envelope = approval["impact_envelope"]
        assert envelope is not None, "approval must bind the impact envelope"
        assert envelope["preview_digest"] == bound_preview_digest
        assert envelope["candidate"]["candidate_id"] == candidate_id
        approval_binding_id = approval["approval_binding_id"]
        _wait_for_candidate_status(server, candidate_id, "approved")

        # 5. The Admin schedules the envelope-bound approval.
        schedule_result = _post_command(
            server,
            "schedule",
            {
                "approval_binding_id": approval_binding_id,
                "activation_at": activation_time,
                "idempotency_key": "s09-rg1-schedule",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        assert schedule_result["status"] == "accepted"

        # 6. Activation commits generation 2 with a final impact manifest.
        active = _wait_for_active_generation(server, 2)
        assert active["candidate_id"] == candidate_id
        final_impact_digest = active["final_impact_digest"]
        assert len(final_impact_digest) == 64
        assert active["final_impact_member_count"] >= 1

        # 7. Lifecycle consumes the final-impact fact: the old generation
        #    run is immediately non-current.
        deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
        history = None
        while time.monotonic() < deadline:
            status, history = _json(
                server,
                "GET",
                f"/controlled/s01/api/queries/applications/{application_id}/history",
                headers=demo_auth_headers(),
                use_session=True,
            )
            runs = history.get("runs", [])
            if runs and not any(run.get("current") for run in runs):
                break
            time.sleep(0.05)
        assert history is not None
        runs = history["runs"]
        assert runs, "application history is empty"
        assert not any(run.get("current") for run in runs), (
            "an old-generation run must never remain current after activation"
        )

        # 8. Exactly one applied disposition with one Operational
        #    Re-evaluation job; a second consumption adds nothing.
        member = _wait_for_impact_disposition(
            server, final_impact_digest, application_id, "applied"
        )
        assert member["partition"] == "open_cycle"
        assert member["target_generation"] == 2
        reevaluation_job_id = member["reevaluation_job_id"]
        assert reevaluation_job_id, "applied disposition must carry one reevaluation job"
        assert member["reevaluation_job_count"] == 1
        result = _impact_dispositions(server, final_impact_digest)
        assert result["unconsumed_count"] == 0
        assert result["members"][0]["reevaluation_job_count"] == 1, (
            "duplicate consumption must not create a second reevaluation job"
        )

        # P-6: the ordinary Reviewer route exposes only the aggregate
        # summary -- never per-member application/job receipts.
        status, summary = _json(
            server,
            "GET",
            "/controlled/s01/api/queries/impact-dispositions?"
            f"final_impact_digest={final_impact_digest}",
            headers=demo_auth_headers(),
            use_session=True,
        )
        assert status == 200, summary
        assert "members" not in summary
        assert summary["member_count"] >= 1
        assert summary["unconsumed_count"] == 0
        # No session and a cross-scope reviewer are the same sanitized 404.
        status, no_session = _json(
            server,
            "GET",
            "/controlled/s01/api/queries/impact-dispositions?"
            f"final_impact_digest={final_impact_digest}",
        )
        assert status == 404
        assert no_session["detail"]["error"] == "S03_NOT_FOUND"


def test_s09_hold_recovery_and_role_denials_over_http(tmp_path: Path) -> None:
    """The scoped hold, the separate recovery command and the role/SoD
    denials are exercised over the real HTTP seam: wrong roles get 403,
    the hold actor's own release gets a conflict, the hold blocks new
    schedules, and explicit recovery releases it."""
    state_path = tmp_path / "target.sqlite3"
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
    }
    with s08_test_loopback(env) as server:
        application_id = _submit_application(server, "s09-http-hold-1")
        run = _wait_for_complete_run(server, application_id)
        assert run["current"] is True

        # Wrong-role denials: only the operator may impose a hold.
        status, denied = _command(
            server,
            "impose_hold",
            {
                "reason_code": "S09_TEST_HOLD",
                "hold_scope": "open_cycle",
                "idempotency_key": "s09-http-hold-wrong-role",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        assert status == 403
        assert denied["detail"]["error"] == "S08_FORBIDDEN"

        # The operator imposes the scoped hold.
        status, hold = _command(
            server,
            "impose_hold",
            {
                "reason_code": "S09_TEST_HOLD",
                "hold_scope": "open_cycle",
                "idempotency_key": "s09-http-hold-1",
                "expected_governance_revision": _governance_revision(server),
            },
            operator_headers(),
        )
        assert status == 200, hold
        assert hold["status"] == "accepted"
        hold_id = hold["hold_id"]
        status, active = _json(
            server,
            "GET",
            f"/controlled/s08/api/queries/active?scope={S08_SCOPE}",
            headers=admin_headers(),
        )
        assert status == 200
        assert [item["hold_id"] for item in active["holds"]] == [hold_id]

        # The hold blocks new schedules for ordinary candidates.
        candidate_id = _new_candidate_in_review(server)
        status, preview = _command(
            server,
            "preview_impact",
            {
                "candidate_id": candidate_id,
                "idempotency_key": "s09-http-hold-preview",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        assert status == 200, preview
        active = _json(
            server,
            "GET",
            f"/controlled/s08/api/queries/active?scope={S08_SCOPE}",
            headers=admin_headers(),
        )[1]
        activation_at = int(time.time()) + 5
        status, approval = _command(
            server,
            "approve",
            {
                "candidate_id": candidate_id,
                "activation_time": activation_at,
                "recovery_release_id": active["candidate_id"],
                "preview_manifest_id": preview["manifest_id"],
                "idempotency_key": "s09-http-hold-approve",
                "expected_governance_revision": _governance_revision(server),
            },
            approver_headers(),
        )
        assert status == 200, approval
        status, scheduled = _command(
            server,
            "schedule",
            {
                "approval_binding_id": approval["approval_binding_id"],
                "activation_at": activation_at,
                "idempotency_key": "s09-http-hold-schedule",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        assert status == 409, f"schedule under hold must fail closed: {scheduled}"

        # The hold actor (activation operator) cannot confirm the release:
        # recovery is approver-only at the HTTP seam (SoD by role).
        status, self_release = _command(
            server,
            "recover_hold",
            {
                "hold_id": hold_id,
                "recovery_generation": 1,
                "idempotency_key": "s09-http-hold-self",
                "expected_governance_revision": _governance_revision(server),
            },
            operator_headers(),
        )
        assert status == 403
        assert self_release["detail"]["error"] == "S08_FORBIDDEN"

        # R5: recovery requires the hold to have reached the covered
        # application -- wait for Lifecycle to consume the imposed hold (the
        # old current run becomes non-operable and the hold is in force).
        deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
        hold_consumed = False
        while time.monotonic() < deadline:
            status, history = _json(
                server,
                "GET",
                f"/controlled/s01/api/queries/applications/{application_id}/history",
                headers=demo_auth_headers(),
                use_session=True,
            )
            if status == 200 and not any(
                run.get("current") for run in history.get("runs", [])
            ):
                hold_consumed = True
                break
            time.sleep(0.05)
        assert hold_consumed, "the imposed hold must be consumed before recovery"

        # The independent approver confirms the exact recovery generation.
        status, recovered = _command(
            server,
            "recover_hold",
            {
                "hold_id": hold_id,
                "recovery_generation": 1,
                "idempotency_key": "s09-http-hold-recover",
                "expected_governance_revision": _governance_revision(server),
            },
            approver_headers(),
        )
        assert status == 200, recovered
        assert recovered["status"] == "accepted"
        assert recovered["hold_released_event_id"]
        status, active = _json(
            server,
            "GET",
            f"/controlled/s08/api/queries/active?scope={S08_SCOPE}",
            headers=admin_headers(),
        )
        assert status == 200
        assert active["holds"] == []


def test_s09_diagnostic_identities_are_isolated_over_http(tmp_path: Path) -> None:
    """P-5 over the real HTTP seam: replay and simulation use separate
    least-privilege credentials; the activation operator is denied; an
    omitted application identity is a closed 422; and each dedicated
    identity runs exactly its own namespace with response DTOs that
    validate through the public contract."""
    state_path = tmp_path / "target.sqlite3"
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
    }
    with s08_test_loopback(env) as server:
        application_id = _submit_application(server, "s09-http-diag-1")
        run = _wait_for_complete_run(server, application_id)
        assert run["current"] is True
        active = _json(
            server,
            "GET",
            f"/controlled/s08/api/queries/active?scope={S08_SCOPE}",
            headers=admin_headers(),
        )[1]
        release_id = active["candidate_id"]
        revision = _governance_revision(server)

        # The activation operator can never run a diagnostic workload.
        status, denied = _command(
            server,
            "replay",
            {
                "release_candidate_id": release_id,
                "application_id": application_id,
                "idempotency_key": "s09-http-diag-op",
                "expected_governance_revision": revision,
            },
            operator_headers(),
        )
        assert status == 403
        assert denied["detail"]["error"] == "S08_FORBIDDEN"

        # An omitted application identity is a closed 422 (never an
        # enumeration of every run application).
        status, omitted = _json(
            server,
            "POST",
            "/controlled/s09/api/commands/replay",
            {
                "release_candidate_id": release_id,
                "idempotency_key": "s09-http-diag-omit",
                "expected_governance_revision": revision,
            },
            replay_headers(),
        )
        assert status == 422

        # The replay identity runs replay and is denied simulation.
        status, replay = _command(
            server,
            "replay",
            {
                "release_candidate_id": release_id,
                "application_id": application_id,
                "idempotency_key": "s09-http-diag-replay",
                "expected_governance_revision": revision,
            },
            replay_headers(),
        )
        assert status == 200, replay
        assert replay["namespace"] == "s09-replay"
        assert replay["bundles"][0]["outcome"] == "REPRODUCED"
        assert replay["bundles"][0]["bundle_digest"]
        assert replay["bundles"][0]["route"] in {"manual_review", "auto_complete"}
        status, cross = _command(
            server,
            "simulate",
            {
                "release_candidate_id": release_id,
                "application_id": application_id,
                "idempotency_key": "s09-http-diag-cross",
                "expected_governance_revision": revision,
            },
            replay_headers(),
        )
        assert status == 403

        # The simulation identity runs simulation and is denied replay.
        status, simulation = _command(
            server,
            "simulate",
            {
                "release_candidate_id": release_id,
                "application_id": application_id,
                "idempotency_key": "s09-http-diag-sim",
                "expected_governance_revision": revision,
            },
            simulation_headers(),
        )
        assert status == 200, simulation
        assert simulation["namespace"] == "s09-simulation"
        assert simulation["bundles"][0]["outcome"] == "REPRODUCED"
        status, cross2 = _command(
            server,
            "replay",
            {
                "release_candidate_id": release_id,
                "application_id": application_id,
                "idempotency_key": "s09-http-diag-cross2",
                "expected_governance_revision": revision,
            },
            simulation_headers(),
        )
        assert status == 403


def test_t09_workspace_four_role_reads_over_http(tmp_path: Path) -> None:
    """The closed T09 governance workspace over the real HTTP seam: four
    roles read one atomic projection with the same governance revision and
    their own server-owned actions; the active release, the recorded
    known-good recovery anchor, the active hold union and the append-only
    S09 event refs are rendered exactly from the ledger."""
    state_path = tmp_path / "target.sqlite3"
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
    }
    with s08_test_loopback(env) as server:
        bootstrap = _json(
            server,
            "GET",
            f"/controlled/s08/api/queries/active?scope={S08_SCOPE}",
            headers=admin_headers(),
        )[1]
        bootstrap_candidate_id = bootstrap["candidate_id"]

        # Four roles read the same atomic revision with their own actions.
        revision = None
        for headers, role, actions in (
            (admin_headers(), "admin", []),
            (approver_headers(), "approver", []),
            (operator_headers(), "operator", ["impose_hold"]),
            (auditor_auth_headers(), "auditor", []),
        ):
            status, workspace = _json(
                server,
                "GET",
                "/controlled/s09/api/queries/workspace",
                headers=headers,
            )
            assert status == 200, f"{role} workspace: {workspace}"
            assert workspace["track"] == "C-DEMO"
            assert workspace["capability_gate"] == "G3"
            assert workspace["scope"] == S08_SCOPE
            assert workspace["actor_role"] == role
            assert workspace["actions"] == actions
            if revision is None:
                revision = workspace["governance_revision"]
            assert workspace["governance_revision"] == revision
            assert workspace["active_release"]["active_generation"] == 1
            assert workspace["active_release"]["bootstrap"] is True
            # The bootstrap release records itself as its known-good
            # recovery anchor.
            assert workspace["recovery_anchor"] == {
                "release_candidate_id": bootstrap_candidate_id
            }
            assert workspace["holds"] == []
            # The minimized Security Audit refs are Auditor-only (P-3): the
            # bootstrap writes no audit fact, so the initial auditor
            # workspace may still be empty, but no other role ever receives
            # audit detail.
            assert isinstance(workspace["audit_events"], list)
            assert workspace["audit_events"] == [] or role == "auditor"

        # A changed release activates with the bootstrap recorded as the
        # known-good recovery anchor.
        candidate_id = _new_candidate_in_review(server)
        activation_at = int(time.time()) + 5
        status, preview = _command(
            server,
            "preview_impact",
            {
                "candidate_id": candidate_id,
                "idempotency_key": "t09-ws-preview",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        assert status == 200, preview
        status, approval = _command(
            server,
            "approve",
            {
                "candidate_id": candidate_id,
                "activation_time": activation_at,
                "recovery_release_id": bootstrap_candidate_id,
                "preview_manifest_id": preview["manifest_id"],
                "idempotency_key": "t09-ws-approve",
                "expected_governance_revision": _governance_revision(server),
            },
            approver_headers(),
        )
        assert status == 200, approval
        _post_command(
            server,
            "schedule",
            {
                "approval_binding_id": approval["approval_binding_id"],
                "activation_at": activation_at,
                "idempotency_key": "t09-ws-schedule",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        _wait_for_active_generation(server, 2)

        status, workspace = _json(
            server,
            "GET",
            "/controlled/s09/api/queries/workspace",
            headers=admin_headers(),
        )
        assert status == 200, workspace
        active = workspace["active_release"]
        assert active["active_generation"] == 2
        assert active["candidate_id"] == candidate_id
        assert active["recovery_release_id"] == bootstrap_candidate_id
        assert active["final_impact_digest"]
        assert len(active["final_impact_digest"]) == 64
        assert workspace["recovery_anchor"] == {
            "release_candidate_id": bootstrap_candidate_id
        }
        event_kinds = [event["kind"] for event in workspace["events"]]
        for kind in ("impact_previewed", "approved", "activated"):
            assert kind in event_kinds, workspace["events"]
        preview_event = next(
            event
            for event in workspace["events"]
            if event["kind"] == "impact_previewed"
        )
        assert preview_event["manifest_id"] == preview["manifest_id"]
        activated_event = next(
            event
            for event in reversed(workspace["events"])
            if event["kind"] == "activated"
        )
        assert activated_event["active_generation"] == 2
        assert activated_event["activation_event_id"]
        assert [event["revision"] for event in workspace["events"]] == sorted(
            event["revision"] for event in workspace["events"]
        )

        # A scoped hold is rendered with exact scope/reason/actor/criterion
        # and re-derives the role actions; it never auto-expires.
        status, hold = _command(
            server,
            "impose_hold",
            {
                "reason_code": "S09_TEST_HOLD",
                "hold_scope": "open_cycle",
                "idempotency_key": "t09-ws-hold",
                "expected_governance_revision": _governance_revision(server),
            },
            operator_headers(),
        )
        assert status == 200, hold
        hold_id = hold["hold_id"]
        status, operator_ws = _json(
            server,
            "GET",
            "/controlled/s09/api/queries/workspace",
            headers=operator_headers(),
        )
        assert status == 200, operator_ws
        assert operator_ws["actions"] == ["impose_hold", "propose_rollback"]
        assert [item["hold_id"] for item in operator_ws["holds"]] == [hold_id]
        rendered = operator_ws["holds"][0]
        assert rendered["reason_code"] == "S09_TEST_HOLD"
        assert rendered["hold_scope"] == "open_cycle"
        assert rendered["imposed_by"] == "c-demo-policy-operator"
        assert rendered["recovery_criterion_id"]
        assert rendered["recovery_criterion_digest"]
        assert rendered["event_id"]
        assert "expires_at" not in rendered
        assert "stopped_at" not in rendered
        status, approver_ws = _json(
            server,
            "GET",
            "/controlled/s09/api/queries/workspace",
            headers=approver_headers(),
        )
        assert status == 200, approver_ws
        assert approver_ws["actions"] == ["recover_hold"]
        assert (
            approver_ws["governance_revision"]
            == operator_ws["governance_revision"]
        )
        hold_event = next(
            event
            for event in approver_ws["events"]
            if event["kind"] == "hold_imposed"
        )
        assert hold_event["hold_id"] == hold_id


def test_t09_auditor_security_audit_refs_stay_immutable_over_http(
    tmp_path: Path,
) -> None:
    """P-3 over the real HTTP seam: the Auditor reads the exact append-only
    Security Audit facts (actor subject/role, action/result/reason and the
    hold plus recovery references) from the workspace; the other roles
    receive no audit detail, and recovery appends exactly one new audit
    fact while every earlier fact stays byte-equal."""
    state_path = tmp_path / "target.sqlite3"
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
    }
    workspace_path = "/controlled/s09/api/queries/workspace"
    with s08_test_loopback(env) as server:
        bootstrap = _json(
            server,
            "GET",
            f"/controlled/s08/api/queries/active?scope={S08_SCOPE}",
            headers=admin_headers(),
        )[1]
        bootstrap_candidate_id = bootstrap["candidate_id"]
        candidate_id = _new_candidate_in_review(server)
        activation_at = int(time.time()) + 5
        status, preview = _command(
            server,
            "preview_impact",
            {
                "candidate_id": candidate_id,
                "idempotency_key": "t09-audit-http-preview",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        assert status == 200, preview
        status, approval = _command(
            server,
            "approve",
            {
                "candidate_id": candidate_id,
                "activation_time": activation_at,
                "recovery_release_id": bootstrap_candidate_id,
                "preview_manifest_id": preview["manifest_id"],
                "idempotency_key": "t09-audit-http-approve",
                "expected_governance_revision": _governance_revision(server),
            },
            approver_headers(),
        )
        assert status == 200, approval
        _post_command(
            server,
            "schedule",
            {
                "approval_binding_id": approval["approval_binding_id"],
                "activation_at": activation_at,
                "idempotency_key": "t09-audit-http-schedule",
                "expected_governance_revision": _governance_revision(server),
            },
            admin_headers(),
        )
        _wait_for_active_generation(server, 2)
        status, hold = _command(
            server,
            "impose_hold",
            {
                "reason_code": "S09_TEST_HOLD",
                "hold_scope": "open_cycle",
                "idempotency_key": "t09-audit-http-hold",
                "expected_governance_revision": _governance_revision(server),
            },
            operator_headers(),
        )
        assert status == 200, hold
        hold_id = hold["hold_id"]

        status, auditor_ws = _json(
            server, "GET", workspace_path, headers=auditor_auth_headers()
        )
        assert status == 200, auditor_ws
        audit_before = auditor_ws["audit_events"]
        assert audit_before, "auditor workspace must expose security audit refs"
        actions = [record["action"] for record in audit_before]
        assert "s08_approve" in actions
        assert "s08_impose_hold" in actions
        hold_audit = next(
            record
            for record in audit_before
            if record["action"] == "s08_impose_hold"
        )
        assert hold_audit["event_id"].startswith("audit_")
        assert hold_audit["hold_id"] == hold_id
        assert hold_audit["subject"] == "c-demo-policy-operator"
        assert hold_audit["role"] == "operator"
        assert hold_audit["result"] == "accepted"
        assert hold_audit["reason_code"] == "S09_HOLD_IMPOSED"
        assert isinstance(hold_audit["event_time"], int)
        approve_audit = next(
            record
            for record in audit_before
            if record["action"] == "s08_approve"
        )
        assert approve_audit["candidate_id"] == candidate_id
        assert approve_audit["role"] == "approver"

        # The other three roles receive no audit detail.
        for headers in (admin_headers(), approver_headers(), operator_headers()):
            status, ws = _json(server, "GET", workspace_path, headers=headers)
            assert status == 200, ws
            assert ws["audit_events"] == []

        status, release = _command(
            server,
            "recover_hold",
            {
                "hold_id": hold_id,
                "recovery_generation": 2,
                "idempotency_key": "t09-audit-http-recover",
                "expected_governance_revision": _governance_revision(server),
            },
            approver_headers(),
        )
        assert status == 200, release
        status, auditor_after = _json(
            server, "GET", workspace_path, headers=auditor_auth_headers()
        )
        assert status == 200, auditor_after
        audit_after = auditor_after["audit_events"]
        # Every prior audit fact stays byte-equal; recovery appends exactly
        # one new Security Audit fact with the release reference.
        assert audit_after[:-1] == audit_before
        assert len(audit_after) == len(audit_before) + 1
        recovered = audit_after[-1]
        assert recovered["action"] == "s08_recover_hold"
        assert recovered["hold_id"] == hold_id
        assert recovered["recovery_generation"] == 2
        assert recovered["reason_code"] == "S09_HOLD_RELEASED"
        assert recovered["subject"] == "c-demo-policy-approver"
        assert recovered["role"] == "approver"


def test_t09_workspace_identity_boundary_and_closed_schema_over_http(
    tmp_path: Path,
) -> None:
    """T09 identity boundary over the real HTTP seam: an unregistered
    identity gets a stable 403 on the query and the React shell (before any
    build check), a missing governance authority is a closed 503, and the
    OpenAPI document exposes the closed workspace DTOs."""
    state_path = tmp_path / "target.sqlite3"
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
    }
    with s08_test_loopback(env) as server:
        status, denied = _json(
            server,
            "GET",
            "/controlled/s09/api/queries/workspace",
            headers={"Authorization": "Bearer unknown-credential"},
        )
        assert status == 403
        assert denied["detail"]["error"] == "S08_FORBIDDEN"
        response = server.request(
            "GET",
            "/controlled/s09/react",
            headers={"Authorization": "Bearer unknown-credential"},
        )
        assert response.status == 403
        assert response.json()["detail"]["error"] == "S08_FORBIDDEN"
        status, spec = _json(
            server, "GET", "/openapi.json", headers=admin_headers()
        )
        assert status == 200
        workspace_path = spec["paths"]["/controlled/s09/api/queries/workspace"]
        assert "get" in workspace_path
        schema = spec["components"]["schemas"]["S09GovernanceWorkspaceResponse"]
        assert schema["additionalProperties"] is False
        assert {
            "track",
            "capability_gate",
            "scope",
            "governance_revision",
            "actor_role",
            "actions",
        } <= set(schema["required"])
        assert "holds" in schema["properties"]
        assert "events" in schema["properties"]
        ref_schema = spec["components"]["schemas"]["S09WorkspaceEventRef"]
        assert ref_schema["additionalProperties"] is False
    with s08_test_loopback(
        {
            **env,
            # Missing governance authority: without the complete S08
            # identities the scope stays closed, S08_SERVICE is never
            # created, and the T09 workspace fails closed 503 before any
            # read.
            "TASK4_S08_ADMIN_CREDENTIAL": "",
            "TASK4_S08_ADMIN_SUBJECT": "",
            "TASK4_S08_APPROVER_CREDENTIAL": "",
            "TASK4_S08_APPROVER_SUBJECT": "",
            "TASK4_S08_OPERATOR_CREDENTIAL": "",
            "TASK4_S08_OPERATOR_SUBJECT": "",
        }
    ) as server:
        status, unavailable = _json(
            server,
            "GET",
            "/controlled/s09/api/queries/workspace",
            headers=admin_headers(),
        )
        assert status == 503
        assert unavailable["detail"]["error"] == "S08_UNAVAILABLE"


def test_s09_diagnostic_result_schemas_are_separate_and_closed(
    tmp_path: Path,
) -> None:
    """R7/ST-4/SP-7: replay and simulation each expose their own closed
    result schema, and both success (REPRODUCED) and the INVALID /
    UNREPRODUCIBLE failure outcomes validate through the public contract
    without leaking raw values, OCR text, credentials or internal paths."""
    state_path = tmp_path / "target.sqlite3"
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
    }
    with s08_test_loopback(env) as server:
        application_id = _submit_application(server, "s09-r7-http-app")
        _wait_for_complete_run(server, application_id)
        active = _json(
            server,
            "GET",
            f"/controlled/s08/api/queries/active?scope={S08_SCOPE}",
            headers=admin_headers(),
        )[1]
        release_id = active["candidate_id"]
        # A governed candidate that is not an active/superseded release is
        # the deterministic INVALID input for the diagnostic factory.
        ungoverned_candidate = _new_candidate_in_review(server)
        revision = _governance_revision(server)

        for command, headers, namespace in (
            ("replay", replay_headers(), "s09-replay"),
            ("simulate", simulation_headers(), "s09-simulation"),
        ):
            # INVALID: a release that is not governed stays a closed success
            # envelope with an INVALID bundle.
            status, invalid = _command(
                server,
                command,
                {
                    "release_candidate_id": ungoverned_candidate,
                    "application_id": application_id,
                    "idempotency_key": f"s09-r7-http-{command}-invalid",
                    "expected_governance_revision": revision,
                },
                headers,
            )
            assert status == 200, invalid
            assert invalid["namespace"] == namespace
            assert invalid["bundle_count"] == 1
            assert invalid["bundles"][0]["outcome"] == "INVALID"
            assert invalid["bundles"][0]["reason_code"] == "RELEASE_NOT_GOVERNED"
            assert invalid["bundles"][0]["bundle_id"] is None
            # UNREPRODUCIBLE: an application with no fixed evidence snapshot
            # stays a closed UNREPRODUCIBLE bundle.
            status, unreproducible = _command(
                server,
                command,
                {
                    "release_candidate_id": release_id,
                    "application_id": "application-without-a-run",
                    "idempotency_key": f"s09-r7-http-{command}-unrepro",
                    "expected_governance_revision": revision,
                },
                headers,
            )
            assert status == 200, unreproducible
            assert unreproducible["bundles"][0]["outcome"] == "UNREPRODUCIBLE"
            assert (
                unreproducible["bundles"][0]["reason_code"]
                == "FIXED_SNAPSHOT_UNAVAILABLE"
            )
            # REPRODUCED: the exact release over the fixed snapshot.
            status, reproduced = _command(
                server,
                command,
                {
                    "release_candidate_id": release_id,
                    "application_id": application_id,
                    "idempotency_key": f"s09-r7-http-{command}-ok",
                    "expected_governance_revision": revision,
                },
                headers,
            )
            assert status == 200, reproduced
            assert reproduced["bundles"][0]["outcome"] == "REPRODUCED"
            assert reproduced["bundles"][0]["bundle_id"]
            assert reproduced["bundles"][0]["bundle_digest"]
            assert reproduced["bundles"][0]["business_revision_delta"] == 0
            # The two commands keep separate result schemas: the namespace
            # is fixed per command and the DTOs validate independently.
            payload = json.dumps(reproduced, ensure_ascii=False)
            for banned in (
                "半真壬",
                "LSVAA4182N5000054",
                "182000",
                "S2ENG54Z",
                "s08-registered",
                "s09-registered",
                "c-demo-policy",
                "/home/",
                "C:\\",
                "sqlite3",
            ):
                assert banned not in payload, (
                    f"{command} result leaked {banned!r}"
                )
            assert payload.count('"namespace"') >= 2
            assert all(
                item.get("namespace") == namespace
                for item in (reproduced, reproduced["bundles"][0])
            )


def _s09_governance_fact_counts(state_path: Path) -> dict[str, int]:
    with sqlite3.connect(state_path) as connection:
        return {
            "governance": connection.execute(
                "SELECT COUNT(*) FROM policy_governance_events"
            ).fetchone()[0],
            "audit": connection.execute(
                "SELECT COUNT(*) FROM audit_events"
            ).fetchone()[0],
            "outbox": connection.execute(
                "SELECT COUNT(*) FROM outbox"
            ).fetchone()[0],
            "idempotency": connection.execute(
                "SELECT COUNT(*) FROM idempotency"
            ).fetchone()[0],
        }


_ALIAS_CREDENTIALS = {
    "admin": ADMIN_CREDENTIAL,
    "approver": APPROVER_CREDENTIAL,
    "operator": OPERATOR_CREDENTIAL,
}
_ALIAS_SUBJECTS = {
    "admin": "c-demo-policy-admin",
    "approver": "c-demo-policy-approver",
    "operator": "c-demo-policy-operator",
}


@pytest.mark.parametrize("other_role", ("admin", "approver", "operator"))
@pytest.mark.parametrize("alias", ("credential", "subject"))
def test_t09_auditor_alias_disables_governance_scope(
    tmp_path: Path, other_role: str, alias: str
) -> None:
    """P-5 fail-closed over the real HTTP seam: when the Auditor identity
    aliases any S08 registered identity (Admin, Approver or the activation
    Operator) in credential or subject, the whole T09 governance scope is
    disabled before authorization — the workspace query, the React shell
    and the Operator command all fail closed with 503, no
    Governance/audit/outbox/idempotency fact is appended, and the unaffected
    S08 three-role surface plus S01 health stay live."""
    state_path = tmp_path / f"alias-{other_role}-{alias}.sqlite3"
    env = {
        "TASK4_S01_TEST_FIXTURE_ROOT": str(ROOT / "fixtures" / "applications"),
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
    }
    if alias == "credential":
        env["TASK4_S01_AUDITOR_CREDENTIAL"] = _ALIAS_CREDENTIALS[other_role]
    else:
        env["TASK4_S01_AUDITOR_SUBJECT"] = _ALIAS_SUBJECTS[other_role]
    with s08_test_loopback(env) as server:
        # Baseline after the governed bootstrap: the aliased configuration
        # must not append a single Governance/audit/outbox/idempotency fact.
        baseline = _s09_governance_fact_counts(state_path)
        # The aliased bearer must never resolve to the Operator mutation
        # role; the governance scope is closed before any authorization.
        for path in (
            "/controlled/s09/api/queries/workspace",
            "/controlled/s09/react",
        ):
            response = server.request("GET", path, headers=auditor_auth_headers())
            assert response.status == 503, f"{path}: {response.status}"
            assert response.json()["detail"]["error"] == "S08_UNAVAILABLE"
        denied = server.request(
            "POST",
            "/controlled/s09/api/commands/impose_hold",
            body={
                "reason_code": "ALIAS_TEST",
                "hold_scope": "open_cycle",
                "idempotency_key": f"alias-{other_role}-{alias}-hold-1",
                "expected_governance_revision": 0,
            },
            headers=auditor_auth_headers(),
        )
        assert denied.status == 503
        assert denied.json()["detail"]["error"] == "S08_UNAVAILABLE"
        # The retained S08 three-role surface and the S01 health endpoint
        # stay live; only the governed T09 scope is disabled.
        status, _ = _json(
            server,
            "GET",
            "/controlled/s08/api/queries/status",
            headers=admin_headers(),
        )
        assert status == 200
        health = server.request("GET", "/api/health")
        assert health.status == 200
    # No Governance, audit, outbox, or idempotency fact was appended by the
    # failed-closed requests beyond the startup bootstrap baseline.
    assert _s09_governance_fact_counts(state_path) == baseline
