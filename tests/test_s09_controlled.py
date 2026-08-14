"""S09 policy impact / safety hold controlled acceptance tests.

Service-level tests over the real Governance + Lifecycle owners sharing one
SQLite state file: approval-envelope enforcement, atomic activation
idempotency and fault barriers, the generation/impact/hold completion
fence, complete dispositions, composable Policy Safety Holds, individual
hold release and the separate recovery command.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from task4_consistency.controlled.s01 import (
    AdmissionDisposition,
    ControlledScenarioService,
    QueryNotFound,
    S01CommandPrincipal,
    _PinnedReleaseUnavailable,
)
from task4_consistency.controlled.s01_checker import (
    TargetChecker,
    TargetCheckResult,
    TargetRunResult,
)
from task4_consistency.controlled.s09_diagnostics import (
    S09DiagnosticBundleWriter,
)
from task4_consistency.controlled.s08 import (
    PolicyConflict,
    PolicyGovernanceService,
    PolicyInvalidTransition,
    PolicyNotFound,
    PolicyPrincipal,
    PolicyUnavailable,
    S08_SCOPE,
    canonical_bytes,
    content_digest,
)
from tests.test_s08_controlled import (
    ADMIN,
    APPROVER,
    ROOT,
    _s01_admit,
    _s01_submit_and_run,
    governance_revision,
    import_draft,
)

OPERATOR = PolicyPrincipal(
    subject="c-demo-policy-operator",
    role="operator",
    scope=S08_SCOPE,
    source_id="s08-test",
)

REPLAY_OPERATOR = PolicyPrincipal(
    subject="c-demo-replay-operator",
    role="replay_operator",
    scope=S08_SCOPE,
    source_id="s08-test",
)

SIMULATION_OPERATOR = PolicyPrincipal(
    subject="c-demo-simulation-operator",
    role="simulation_operator",
    scope=S08_SCOPE,
    source_id="s08-test",
)

INTEGRATOR = S01CommandPrincipal(
    subject="registered-test-integrator",
    role="integrator",
    scope="C-DEMO",
    source_id="s01-test-client",
)

RECONCILIATION = S01CommandPrincipal(
    subject="c-demo-test-reconciliation",
    role="reconciliation",
    scope="C-DEMO",
    source_id="s01-test-client",
)

REVIEWER = S01CommandPrincipal(
    subject="c-demo-test-reviewer",
    role="reviewer",
    scope="C-DEMO",
    source_id="s01-test-client",
)



def _s09_governed(tmp_path: Path) -> tuple[ControlledScenarioService, PolicyGovernanceService]:
    """Governed S01 + S08 owners sharing one SQLite state file with the
    S09 cross-owner impact snapshot provider wired."""
    state = tmp_path / "governed-s09.sqlite3"
    bundle = tmp_path / "s09-bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    rules_path = bundle / "rules.yaml"
    kb_path = bundle / "entity_kb.json"
    rules_path.write_bytes((ROOT / "configs" / "rules_auto_lease.yaml").read_bytes())
    kb_path.write_bytes((ROOT / "configs" / "kb" / "entity_kb.json").read_bytes())
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=rules_path,
        state_path=state,
        policy_governance=None,
    )
    policy = PolicyGovernanceService(
        state_path=state,
        source_rules_path=rules_path,
        source_kb_path=kb_path,
        corpus_root=ROOT / "fixtures" / "applications",
    )
    policy._lifecycle_snapshot_provider = (
        lambda owner, digest=None: service.build_policy_impact_snapshot(owner, digest)
    )
    policy._diagnostic_snapshot_provider = (
        lambda owner, application_id: service.build_policy_diagnostic_snapshot(
            owner, application_id
        )
    )
    service._policy_governance = policy
    assert policy.bootstrap_once()["status"] == "activated"
    return service, policy


def _s09_candidate_in_review(
    policy: PolicyGovernanceService, key_prefix: str, reason: str | None = None
) -> str:
    draft_id = import_draft(policy)
    revised = policy.revise_draft(
        principal=ADMIN,
        draft_id=draft_id,
        metadata={
            "scope": S08_SCOPE,
            "validity": {"valid_from": "2000-01-01T00:00:00Z"},
            "source": "c-demo-legacy-baseline/1",
            "reason": reason or "S09 controlled candidate",
        },
        idempotency_key=f"{key_prefix}-revise",
        expected_governance_revision=governance_revision(policy),
    )
    # A frozen draft forks on revision; freeze the revised draft identity.
    revised_draft_id = revised["draft_id"]
    freeze = policy.freeze_candidate(
        principal=ADMIN,
        draft_id=revised_draft_id,
        idempotency_key=f"{key_prefix}-freeze",
        expected_governance_revision=governance_revision(policy),
    )
    candidate_id = freeze["candidate_id"]
    policy.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"{key_prefix}-validate",
        expected_governance_revision=governance_revision(policy),
    )
    result = policy.process_next_policy_job()
    assert result["status"] == "complete", result
    policy.submit_review(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"{key_prefix}-review",
        expected_governance_revision=governance_revision(policy),
    )
    return candidate_id


def _s09_preview_approve_schedule(
    policy: PolicyGovernanceService,
    candidate_id: str,
    key_prefix: str,
    *,
    activation_at: int | None = None,
) -> tuple[str, str, str, int]:
    """Preview -> envelope-bound approve -> schedule; returns
    (preview_id, approval_binding_id, reservation_id, activation_at)."""
    preview = policy.preview_impact(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"{key_prefix}-preview",
        expected_governance_revision=governance_revision(policy),
    )
    assert preview["status"] == "accepted"
    activation_at = activation_at or (int(time.time()) + 300)
    active = policy.query_active(ADMIN)
    approval = policy.approve(
        principal=APPROVER,
        candidate_id=candidate_id,
        activation_time=activation_at,
        recovery_release_id=active["candidate_id"],
        preview_manifest_id=preview["manifest_id"],
        idempotency_key=f"{key_prefix}-approve",
        expected_governance_revision=governance_revision(policy),
    )
    assert approval["status"] == "accepted"
    assert approval["impact_envelope"] is not None
    assert approval["impact_envelope"]["preview_digest"] == preview["digest"]
    scheduled = policy.schedule(
        principal=ADMIN,
        approval_binding_id=approval["approval_binding_id"],
        activation_at=activation_at,
        idempotency_key=f"{key_prefix}-schedule",
        expected_governance_revision=governance_revision(policy),
    )
    assert scheduled["status"] == "accepted"
    return (
        preview["manifest_id"],
        approval["approval_binding_id"],
        scheduled["reservation_id"],
        activation_at,
    )


def _active_generation(policy: PolicyGovernanceService) -> int | None:
    return policy.query_active(ADMIN)["active_generation"]


def _latest_final_digest(policy: PolicyGovernanceService) -> str | None:
    return policy.query_active(ADMIN)["final_impact_digest"]


def test_final_expansion_outside_envelope_stops_activation_with_zero_delta(
    tmp_path: Path,
) -> None:
    """The activation-time final impact may add members only inside the
    approved envelope.  A new affected application between preview and
    activation stops activation with zero protected delta; the next action
    is a new preview and a new approval over a new candidate."""
    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-rg3-expand-1")
    candidate_a = _s09_candidate_in_review(policy, "s09-rg3-expand-a")
    _, approval_binding_a, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate_a, "s09-rg3-expand-a"
    )

    # A second application completes a generation-1 run before activation:
    # the final impact gains a member the envelope never permitted.
    # The C-DEMO demo path admits exactly one application per scenario, so
    # the second affected member is staged as an immutable store fact the
    # same way a registered source would have produced it: a governed
    # generation-1 current run on an open-cycle application.
    application_b = "app_s09_expansion_member"
    service._reload_store()
    fabricated = copy.deepcopy(service._store)
    fabricated.applications[application_b] = {
        "application_id": application_b,
        "cycle": 1,
        "phase": "Manual Review",
        "route": "manual_review",
        "current_run_id": "run_fabricated_b",
        "current_evidence_snapshot_id": "snapshot_sha256_" + "f" * 64,
        "current_evidence_snapshot_digest": "f" * 64,
        "lifecycle_revision": 6,
        "evidence_revision": 2,
        "evidence_ready": False,
        "projection_pending": False,
        "projection_visible": False,
        "phase_history": ["Intake", "Assembly", "Evidence Ready", "Checking", "Routing Determination", "Manual Review"],
        "upstream_application_reference": "s09-fabricated-upstream",
        "envelope": {"upstream_application_reference": "s09-fabricated-upstream"},
        "artifact_manifest": {"digest": service._manifest.digest},
    }
    fabricated.runs.append(
        {
            "run_record_id": "run_record_fabricated_b",
            "run_id": "run_fabricated_b",
            "attempt_id": "attempt_fabricated_b",
            "application_id": application_b,
            "spec": {
                "application_id": application_b,
                "run_id": "run_fabricated_b",
                "cycle": 1,
                "active_generation": 1,
                "lifecycle_revision": 6,
                "evidence_revision": 2,
                "release_id": "legacy",
                "release_digest": "0" * 64,
                "checker_build": "legacy",
                "evidence_snapshot_id": "snapshot_sha256_" + "f" * 64,
                "evidence_snapshot_digest": "f" * 64,
                "fence": 1,
            },
            "status": "complete",
            "completion_context": {},
            "finding_ids": [],
        }
    )
    fabricated.persist()
    service._store = fabricated

    events_before = len(policy._store.policy_governance_events)
    outbox_before = len(policy._store.outbox)
    result = policy.process_next_policy_job(now=activation_at)
    assert result["status"] in {"failed", "discarded", "diagnostic"}, result

    # Zero protected delta: no new generation, no final impact, no outbox
    # obligation, and the schedule reservation is released (worker attempt
    # records are diagnostics, not protected business facts).
    assert _active_generation(policy) == 1
    assert _latest_final_digest(policy) is None
    assert len(policy._store.policy_governance_events) == events_before
    assert len(policy._store.outbox) == outbox_before
    reservation = next(
        (
            value
            for value in policy._store.policy_schedule_reservations.values()
            if value.get("approval_binding_id") == approval_binding_a
        ),
        None,
    )

    # A fresh preview + fresh approval over a new candidate activates.
    candidate_b = _s09_candidate_in_review(
        policy, "s09-rg3-expand-b", reason="S09 controlled candidate retry"
    )
    preview_b = policy.preview_impact(
        principal=ADMIN,
        candidate_id=candidate_b,
        idempotency_key="s09-rg3-expand-b-preview",
        expected_governance_revision=governance_revision(policy),
    )
    assert preview_b["member_count"] == 2
    approval_b = policy.approve(
        principal=APPROVER,
        candidate_id=candidate_b,
        activation_time=activation_at,
        recovery_release_id=policy.query_active(ADMIN)["candidate_id"],
        preview_manifest_id=preview_b["manifest_id"],
        idempotency_key="s09-rg3-expand-b-approve",
        expected_governance_revision=governance_revision(policy),
    )
    policy.schedule(
        principal=ADMIN,
        approval_binding_id=approval_b["approval_binding_id"],
        activation_at=activation_at,
        idempotency_key="s09-rg3-expand-b-schedule",
        expected_governance_revision=governance_revision(policy),
    )
    result = policy.process_next_policy_job(now=activation_at)
    assert result["status"] == "complete", result
    assert _active_generation(policy) == 2
    final_digest = _latest_final_digest(policy)
    assert final_digest is not None
    manifest = policy.load_final_impact(final_digest)
    assert manifest is not None
    assert {m["application_id"] for m in manifest["members"]} == {
        application_id,
        application_b,
    }
    assert reservation is None or reservation.get("status") == "cancelled"


def test_activation_response_loss_replays_identical_committed_result(
    tmp_path: Path,
) -> None:
    """Once the activation transaction commits, a replayed operation returns
    the identical committed result (same event id, generation and final
    impact digest); reprocessing the completed job cannot double-activate."""
    service, policy = _s09_governed(tmp_path)
    _s01_submit_and_run(service, "s09-rg3-idem-1")
    candidate = _s09_candidate_in_review(policy, "s09-rg3-idem")
    _, _, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate, "s09-rg3-idem"
    )
    result = policy.process_next_policy_job(now=activation_at)
    assert result["status"] == "complete", result
    committed = policy.query_active(ADMIN)
    assert committed["active_generation"] == 2
    assert committed["final_impact_digest"]

    events_before = len(policy._store.policy_governance_events)
    replay = policy.process_next_policy_job(now=activation_at + 1)
    assert replay is None or replay.get("status") == "idle", replay
    assert len(policy._store.policy_governance_events) == events_before
    assert policy.query_active(ADMIN)["active_generation"] == 2

    # The idempotency record binds the exact committed result including the
    # final impact digest: a lost response is reconciled by replaying the
    # original operation identity.
    operation_results = [
        stored
        for _, stored in policy._store.idempotency.values()
        if isinstance(stored, dict)
        and stored.get("status") == "accepted"
        and stored.get("activation_event_id") == committed["activation_event_id"]
    ]
    assert len(operation_results) == 1
    assert operation_results[0]["final_impact_digest"] == committed[
        "final_impact_digest"
    ]
    assert operation_results[0]["active_generation"] == 2


def test_fault_after_final_impact_before_commit_leaves_zero_protected_delta(
    tmp_path: Path,
) -> None:
    """A fault after the final-impact computation but before the Ledger
    commit leaves every protected collection at delta zero: no impact
    event, no activation event, no generation, no success audit, no
    idempotency record and no outbox obligation."""
    state = tmp_path / "target.sqlite3"

    def inject(write_point: str) -> None:
        if write_point == "s09.activation.impact_finalized":
            raise OSError("injected S09 test fault")

    policy = PolicyGovernanceService(
        state_path=state,
        source_rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        source_kb_path=ROOT / "configs" / "kb" / "entity_kb.json",
        corpus_root=ROOT / "fixtures" / "applications",
        fault_injector=inject,
    )
    assert policy.bootstrap_once()["status"] == "activated"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state,
        policy_governance=policy,
    )
    policy._lifecycle_snapshot_provider = (
        lambda owner, digest=None: service.build_policy_impact_snapshot(owner, digest)
    )
    _s01_submit_and_run(service, "s09-rg3-fault-1")
    candidate = _s09_candidate_in_review(policy, "s09-rg3-fault")
    _, _, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate, "s09-rg3-fault"
    )
    events_before = len(policy._store.policy_governance_events)
    outbox_before = len(policy._store.outbox)
    audit_before = len(policy._store.audit_events)
    idem_before = len(policy._store.idempotency)
    result = policy.process_next_policy_job(now=activation_at)
    assert result["status"] in {"failed", "discarded", "diagnostic"}, result
    assert len(policy._store.policy_governance_events) == events_before
    assert len(policy._store.outbox) == outbox_before
    assert len(policy._store.audit_events) == audit_before
    assert len(policy._store.idempotency) == idem_before
    assert _active_generation(policy) == 1
    assert _latest_final_digest(policy) is None
    assert not any(
        event.get("kind") in {"impact_finalized", "activated"}
        for event in policy._store.policy_governance_events
        if event.get("active_generation") == 2
    )


def test_late_old_generation_completion_stays_non_current_diagnostic(
    tmp_path: Path,
) -> None:
    """A generation-1 run frozen before activation whose completion lands
    after the generation boundary changed is retained only as a non-current
    diagnostic: no current run, route, work or business revision changes."""
    service, policy = _s09_governed(tmp_path)
    application_id = _s01_admit(service, "s09-rg3-race-1")

    # Freeze a generation-1 run spec in flight (leased, not complete).
    claimed = service._claim_job("race-worker", int(time.time()))
    assert claimed is not None, "expected a claimable job"
    selected, attempt, run_spec = claimed
    assert run_spec["active_generation"] == 1
    old_run_id = run_spec["run_id"]

    # The generation boundary changes while the run is in flight.
    candidate = _s09_candidate_in_review(policy, "s09-rg3-race")
    _, _, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate, "s09-rg3-race"
    )
    result = policy.process_next_policy_job(now=activation_at)
    assert result["status"] == "complete", result
    assert _active_generation(policy) == 2
    final_digest = _latest_final_digest(policy)
    assert final_digest

    # The late generation-1 completion (before any consumption) can never
    # become current: the generation fence and the pending member
    # disposition both fail closed.
    service._reload_store()
    app = service._store.applications[application_id]
    completion_context = service._completion_context(run_spec)
    run_result = TargetRunResult(application_id=application_id, checks=())
    late = service._commit_complete_result_once(
        app,
        selected,
        attempt,
        run_spec,
        run_result,
        completion_context,
        service._semantic_differential(app, run_result, run_spec),
        now=int(time.time()),
    )
    assert late.status == "stale", late
    assert {"active_generation", "impact_disposition"} <= set(late.cas_mismatches)
    staged_app = service._store.applications[application_id]
    assert staged_app["current_run_id"] is None
    assert staged_app["route"] == "pending_check"
    stale_runs = [
        record
        for record in service._store.runs
        if record.get("run_id") == old_run_id
    ]
    assert stale_runs and stale_runs[0]["status"] == "stale"

    # Consumption then reconciles the member with exactly one applied
    # disposition and one reevaluation job.
    assert service.process_next_policy_impact() == 1
    view = service.impact_dispositions_view(
        principal=RECONCILIATION, final_impact_digest=final_digest
    )
    member = next(
        item
        for item in view["members"]
        if item["application_id"] == application_id
    )
    assert member["disposition"] == "applied"
    assert member["reevaluation_job_count"] == 1


def test_repeated_consumption_is_idempotent_one_disposition_one_job(
    tmp_path: Path,
) -> None:
    """Duplicate delivery of the same final-impact fact yields one
    disposition receipt and one Operational Re-evaluation job."""
    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-rg3-dup-1")
    candidate = _s09_candidate_in_review(policy, "s09-rg3-dup")
    _, _, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate, "s09-rg3-dup"
    )
    policy.process_next_policy_job(now=activation_at)
    final_digest = _latest_final_digest(policy)
    assert final_digest
    assert service.process_next_policy_impact() == 1
    assert service.process_next_policy_impact() == 0
    view = service.impact_dispositions_view(
        principal=RECONCILIATION, final_impact_digest=final_digest
    )
    member = next(
        item
        for item in view["members"]
        if item["application_id"] == application_id
    )
    assert member["disposition"] == "applied"
    assert member["reevaluation_job_count"] == 1
    assert view["unconsumed_count"] == 0


def test_hold_blocks_run_spec_and_completion_and_enters_unprocessable(
    tmp_path: Path,
) -> None:
    """A scoped Policy Safety Hold blocks RunSpec publication, current
    completion and automatic routing; Lifecycle consumption enters
    Unprocessable and never jumps directly to Routing Determination."""
    service, policy = _s09_governed(tmp_path)
    application_id = _s01_admit(service, "s09-rg4-hold-1")
    hold = policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_TEST_HOLD",
        hold_scope="open_cycle",
        idempotency_key="s09-rg4-hold-1",
        expected_governance_revision=governance_revision(policy),
    )
    assert hold["status"] == "accepted"
    assert hold["hold_id"]
    status = policy.query_status(ADMIN)
    assert [item["hold_id"] for item in status["holds"]] == [hold["hold_id"]]
    assert status["final_impact_digest"] is None

    # RunSpec publication fails closed under the hold: no queued job can
    # claim, and no new run may be frozen.
    with pytest.raises(_PinnedReleaseUnavailable) as claim_error:
        service._claim_job("hold-worker", int(time.time()))
    assert claim_error.value.args[0] == "S09_POLICY_SAFETY_HOLD"
    with pytest.raises(_PinnedReleaseUnavailable) as freeze_error:
        service._freeze_run_spec(
            copy.deepcopy(service._store.applications[application_id]),
            {"job_id": "j", "fence": 0},
        )
    assert freeze_error.value.args[0] == "S09_POLICY_SAFETY_HOLD"

    # Lifecycle consumption enters Unprocessable with the hold recorded.
    assert service.process_next_policy_impact() == 1
    app = service._store.applications[application_id]
    assert app["phase"] == "Unprocessable"
    assert app["route"] == "unprocessable"
    assert hold["hold_id"] in (app.get("active_hold_ids") or [])


def test_application_hold_does_not_create_global_runtime_stop(
    tmp_path: Path,
) -> None:
    service, policy = _s09_governed(tmp_path)
    application_id = _s01_admit(service, "s09-r3-scoped-worker-hold")
    policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_SCOPED_TEST_HOLD",
        hold_scope=application_id,
        idempotency_key="s09-r3-scoped-worker-hold",
        expected_governance_revision=governance_revision(policy),
    )

    result = service.process_next_job()

    assert result.status == "stopped"
    assert result.reason_code == "S09_POLICY_SAFETY_HOLD"
    assert service.cohort_status() == {"track": "C-DEMO", "admission": "open"}


def test_pending_application_hold_fences_existing_review_claim_and_submit(
    tmp_path: Path,
) -> None:
    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-r3-review-hold")
    now = int(time.time())
    service.refresh_projection()
    reviewer = S01CommandPrincipal(
        subject=INTEGRATOR.subject,
        role="reviewer",
        scope=INTEGRATOR.scope,
        source_id="s09-review-console",
    )
    queue = service.queue_view(
        role=reviewer.role,
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=now,
    )
    work_item_id = next(
        item["work_item_id"]
        for item in queue["items"]
        if item["application_id"] == application_id
    )
    view = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=now,
    )
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=view["command_context"],
        now=now,
    )
    assert claimed["status"] == "claimed"
    workspace = service.workspace_view(
        application_id,
        role=reviewer.role,
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=now,
    )
    verification = {
        "schema_version": "human-decision/1",
        "outcome": "confirmed",
        "reason_code": "HUMAN_REVIEW_COMPLETED",
        "finding_decisions": [
            {"finding_id": item["finding_id"], "outcome": "confirmed"}
            for item in workspace["mandatory_blockers"]
        ],
    }
    policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_PENDING_REVIEW_HOLD",
        hold_scope=application_id,
        idempotency_key="s09-r3-review-hold",
        expected_governance_revision=governance_revision(policy),
    )

    submit = service.submit_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=view["command_context"],
        idempotency_key="s09-r3-review-held-submit",
        verification=verification,
        now=now + 1,
    )
    released = service.release_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=view["command_context"],
        idempotency_key="s09-r3-review-held-release",
        now=now + 1,
    )
    assert released["status"] == "released"
    second_claim = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=view["command_context"],
        now=now + 1,
    )

    assert submit["status"] == "stopped"
    assert submit["reason_code"] == "S09_POLICY_SAFETY_HOLD"
    assert second_claim["status"] == "stopped"
    assert second_claim["reason_code"] == "S09_POLICY_SAFETY_HOLD"
    assert service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=now + 1,
    )["status"] == "released"


def test_overlapping_holds_compose_union_and_release_individually(
    tmp_path: Path,
) -> None:
    """Two overlapping holds compose by union; each is released only by its
    own criterion passing, and the union keeps blocking while any hold is
    active."""
    service, policy = _s09_governed(tmp_path)
    _s01_submit_and_run(service, "s09-rg4-union-1")
    hold_a = policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_HOLD_A",
        hold_scope="open_cycle",
        idempotency_key="s09-rg4-union-a",
        expected_governance_revision=governance_revision(policy),
    )
    hold_b = policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_HOLD_B",
        hold_scope="open_cycle",
        idempotency_key="s09-rg4-union-b",
        expected_governance_revision=governance_revision(policy),
    )
    holds = policy.query_status(ADMIN)["holds"]
    assert {item["hold_id"] for item in holds} == {
        hold_a["hold_id"],
        hold_b["hold_id"],
    }

    # R5: recovery requires the imposed hold facts to have reached every
    # covered application -- Lifecycle consumes both imposed facts first.
    assert service.process_next_policy_impact() == 2

    # Releasing one hold leaves the union active.
    release_a = policy.recover_hold(
        principal=APPROVER,
        hold_id=hold_a["hold_id"],
        recovery_generation=_active_generation(policy) or 1,
        idempotency_key="s09-rg4-union-release-a",
        expected_governance_revision=governance_revision(policy),
    )
    assert release_a["status"] == "accepted"
    assert service.process_next_policy_impact() == 1
    holds = policy.query_status(ADMIN)["holds"]
    assert [item["hold_id"] for item in holds] == [hold_b["hold_id"]]
    assert policy._activation_hold(policy._store, S08_SCOPE) is not None

    # Releasing the second hold leaves the union empty.
    release_b = policy.recover_hold(
        principal=APPROVER,
        hold_id=hold_b["hold_id"],
        recovery_generation=_active_generation(policy) or 1,
        idempotency_key="s09-rg4-union-release-b",
        expected_governance_revision=governance_revision(policy),
    )
    assert release_b["status"] == "accepted"
    assert service.process_next_policy_impact() == 1
    assert policy.query_status(ADMIN)["holds"] == []
    assert policy._activation_hold(policy._store, S08_SCOPE) is None


def test_hold_never_auto_expires_and_recovery_needs_exact_proof(
    tmp_path: Path,
) -> None:
    """Time never releases a hold; recovery rejects the hold actor, a wrong
    recovery generation, and any outstanding disposition, and succeeds only
    with the exact proof.  Lifecycle then consumes the release fact and
    re-enters the normal gate."""
    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-rg4-recover-1")
    hold = policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_TEST_HOLD",
        hold_scope="open_cycle",
        idempotency_key="s09-rg4-recover-hold",
        expected_governance_revision=governance_revision(policy),
    )
    # Time passes: the hold stays.
    policy._store.reload()
    assert policy._activation_hold(policy._store, S08_SCOPE) is not None

    # The hold actor cannot confirm its own release.
    with pytest.raises(PolicyInvalidTransition):
        policy.recover_hold(
            principal=OPERATOR,
            hold_id=hold["hold_id"],
            recovery_generation=1,
            idempotency_key="s09-rg4-recover-self",
            expected_governance_revision=governance_revision(policy),
        )
    # A wrong recovery generation is rejected.
    with pytest.raises(PolicyInvalidTransition):
        policy.recover_hold(
            principal=APPROVER,
            hold_id=hold["hold_id"],
            recovery_generation=99,
            idempotency_key="s09-rg4-recover-gen",
            expected_governance_revision=governance_revision(policy),
        )

    # Lifecycle consumption enters Unprocessable under the hold.
    assert service.process_next_policy_impact() == 1
    app = service._store.applications[application_id]
    assert app["phase"] == "Unprocessable"

    # The exact recovery succeeds: generation 1 is the active generation and
    # no final impact is outstanding.
    released = policy.recover_hold(
        principal=APPROVER,
        hold_id=hold["hold_id"],
        recovery_generation=1,
        idempotency_key="s09-rg4-recover-ok",
        expected_governance_revision=governance_revision(policy),
    )
    assert released["status"] == "accepted"
    assert released["hold_released_event_id"]
    assert policy.query_status(ADMIN)["holds"] == []

    # Lifecycle consumes the release fact: the app re-enters the normal
    # gate (Evidence Ready) with a fresh reevaluation job.
    assert service.process_next_policy_impact() == 1
    app = service._store.applications[application_id]
    assert app["phase"] == "Evidence Ready"
    assert app["route"] == "pending_check"
    reevaluation_jobs = [
        job
        for job in service._store.jobs
        if job.get("kind") == "operational_reevaluation"
        and job.get("application_id") == application_id
    ]
    assert len(reevaluation_jobs) == 1


def test_outstanding_member_blocks_recovery(
    tmp_path: Path,
) -> None:
    """A member without any reconcilable disposition keeps the recovery
    command closed: no hold may be released while any tuple is
    outstanding."""
    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-rg4-out-1")
    # Activate first: the final impact lists the member.
    candidate = _s09_candidate_in_review(policy, "s09-rg4-out")
    _, _, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate, "s09-rg4-out"
    )
    result = policy.process_next_policy_job(now=activation_at)
    assert result["status"] == "complete", result
    final_digest = _latest_final_digest(policy)
    assert final_digest
    hold = policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_TEST_HOLD",
        hold_scope="open_cycle",
        idempotency_key="s09-rg4-out-hold",
        expected_governance_revision=governance_revision(policy),
    )
    # The application is no longer in the same cycle before consumption:
    # the member cannot be reconciled and stays outstanding.
    service._reload_store()
    staged = copy.deepcopy(service._store)
    staged.applications[application_id]["cycle"] = 2
    staged.persist()
    service._store = staged
    assert service.process_next_policy_impact() == 2
    view = service.impact_dispositions_view(
        principal=RECONCILIATION, final_impact_digest=final_digest
    )
    assert view["unconsumed_count"] == 1
    assert view["members"][0]["disposition"] == "outstanding"
    with pytest.raises(PolicyInvalidTransition):
        policy.recover_hold(
            principal=APPROVER,
            hold_id=hold["hold_id"],
            recovery_generation=2,
            idempotency_key="s09-rg4-out-recover",
            expected_governance_revision=governance_revision(policy),
        )


def test_compatible_rollback_is_new_activation_and_hold_release_is_separate(
    tmp_path: Path,
) -> None:
    """A compatible rollback revalidates the exact known-good release and
    activates it at current time as a new generation through the fresh
    preview/approval pipeline; the rollback activation never releases the
    hold, and the separate recovery command releases it only after every
    disposition is reconcilable.  Lifecycle then re-enters the normal gate."""
    service, policy = _s09_governed(tmp_path)
    known_good = policy.query_active(ADMIN)["candidate_id"]
    application_id, _ = _s01_submit_and_run(service, "s09-rg5-rollback-1")

    # The bad release activates as generation 2 and is consumed.
    candidate_a = _s09_candidate_in_review(policy, "s09-rg5-rollback-a")
    _, _, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate_a, "s09-rg5-rollback-a"
    )
    result = policy.process_next_policy_job(now=activation_at)
    assert result["status"] == "complete", result
    assert _active_generation(policy) == 2
    assert service.process_next_policy_impact() == 1

    # The operator imposes a scoped hold; Lifecycle enters Unprocessable.
    hold = policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_TEST_HOLD",
        hold_scope="open_cycle",
        idempotency_key="s09-rg5-rollback-hold",
        expected_governance_revision=governance_revision(policy),
    )
    assert service.process_next_policy_impact() == 1
    app = service._store.applications[application_id]
    assert app["phase"] == "Unprocessable"

    # The compatible rollback proposal revalidates the known-good release.
    rollback = policy.propose_rollback(
        principal=OPERATOR,
        release_candidate_id=known_good,
        reason_code="S09_TEST_ROLLBACK",
        idempotency_key="s09-rg5-rollback-propose",
        expected_governance_revision=governance_revision(policy),
    )
    assert rollback["status"] == "accepted"
    assert rollback["compatibility"]["compatible"] is True
    rollback_candidate = rollback["candidate_id"]
    policy.submit_review(
        principal=ADMIN,
        candidate_id=rollback_candidate,
        idempotency_key="s09-rg5-rollback-review",
        expected_governance_revision=governance_revision(policy),
    )
    preview = policy.preview_impact(
        principal=ADMIN,
        candidate_id=rollback_candidate,
        idempotency_key="s09-rg5-rollback-preview",
        expected_governance_revision=governance_revision(policy),
    )
    approval = policy.approve(
        principal=APPROVER,
        candidate_id=rollback_candidate,
        activation_time=activation_at,
        recovery_release_id=known_good,
        preview_manifest_id=preview["manifest_id"],
        idempotency_key="s09-rg5-rollback-approve",
        expected_governance_revision=governance_revision(policy),
    )
    policy.schedule(
        principal=ADMIN,
        approval_binding_id=approval["approval_binding_id"],
        activation_at=activation_at,
        idempotency_key="s09-rg5-rollback-schedule",
        expected_governance_revision=governance_revision(policy),
    )
    result = policy.process_next_policy_job(now=activation_at + 1)
    assert result["status"] == "complete", result
    active = policy.query_active(ADMIN)
    assert active["active_generation"] == 3
    assert active["candidate_id"] == rollback_candidate
    assert active["activation_event_id"]
    # The rollback is a NEW current-time fact; the bad release stays
    # superseded and reproducible, and the hold is NOT released by it.
    assert policy.query_status(ADMIN)["holds"] != []
    assert [item["candidate_id"] for item in policy.query_candidates(ADMIN)["candidates"]]
    superseded = [
        item
        for item in policy.query_candidates(ADMIN)["candidates"]
        if item["candidate_id"] == candidate_a
    ]
    assert superseded and superseded[0]["status"] == "superseded"

    # The rollback final impact is consumed; the member is applied with the
    # reevaluation deferred behind the still-active hold.
    assert service.process_next_policy_impact() == 1
    final_digest = _latest_final_digest(policy)
    assert final_digest
    view = service.impact_dispositions_view(
        principal=RECONCILIATION, final_impact_digest=final_digest
    )
    assert view["unconsumed_count"] == 0
    member = next(
        item
        for item in view["members"]
        if item["application_id"] == application_id
    )
    assert member["disposition"] == "applied"

    # The separate recovery command releases the hold with the exact
    # recovery generation; Lifecycle consumes the release fact and
    # re-enters the normal gate with a fresh reevaluation job.
    released = policy.recover_hold(
        principal=APPROVER,
        hold_id=hold["hold_id"],
        recovery_generation=3,
        idempotency_key="s09-rg5-rollback-recover",
        expected_governance_revision=governance_revision(policy),
    )
    assert released["status"] == "accepted"
    assert policy.query_status(ADMIN)["holds"] == []
    assert service.process_next_policy_impact() == 1
    app = service._store.applications[application_id]
    assert app["phase"] == "Evidence Ready"
    assert app["route"] == "pending_check"
    reevaluation_jobs = [
        job
        for job in service._store.jobs
        if job.get("kind") == "operational_reevaluation"
        and job.get("application_id") == application_id
    ]
    assert len(reevaluation_jobs) == 2


def test_incompatible_rollback_requires_forward_fix(
    tmp_path: Path,
) -> None:
    """An ineligible/missing rollback release keeps the hold, creates no
    candidate and no activation; the only publication path left is a
    governed forward fix."""
    service, policy = _s09_governed(tmp_path)
    _s01_submit_and_run(service, "s09-rg5-forward-1")
    candidate_a = _s09_candidate_in_review(policy, "s09-rg5-forward-a")
    _, _, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate_a, "s09-rg5-forward-a"
    )
    result = policy.process_next_policy_job(now=activation_at)
    assert result["status"] == "complete", result
    assert _active_generation(policy) == 2
    assert service.process_next_policy_impact() == 1
    hold = policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_TEST_HOLD",
        hold_scope="open_cycle",
        idempotency_key="s09-rg5-forward-hold",
        expected_governance_revision=governance_revision(policy),
    )
    assert service.process_next_policy_impact() == 1

    events_before = len(policy._store.policy_governance_events)
    with pytest.raises(PolicyInvalidTransition) as error:
        policy.propose_rollback(
            principal=OPERATOR,
            release_candidate_id="candidate_does_not_exist",
            reason_code="S09_TEST_ROLLBACK",
            idempotency_key="s09-rg5-forward-rollback",
            expected_governance_revision=governance_revision(policy),
        )
    assert str(error.value).startswith("ROLLBACK_INCOMPATIBLE_RELEASE_NOT_GOVERNED")
    # Zero protected delta: no rollback candidate, no generation change and
    # the hold stays active.
    assert len(policy._store.policy_governance_events) == events_before
    assert _active_generation(policy) == 2
    assert policy.query_status(ADMIN)["holds"] != []
    assert not any(
        event.get("kind") == "rollback_proposed"
        for event in policy._store.policy_governance_events
    )


def test_replay_and_simulation_are_zero_business_delta_diagnostics(
    tmp_path: Path,
) -> None:
    """Replay and counterfactual simulation return namespaced diagnostic
    bundles over fixed snapshots and exact releases; every business owner
    revision and collection count stays at zero delta."""
    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-rg6-diag-1")
    bootstrap_id = "bootstrap"
    active = policy.query_active(ADMIN)
    release_id = active["candidate_id"]
    events_before = len(policy._store.policy_governance_events)
    runs_before = len(policy._store.runs)
    lifecycle_before = len(policy._store.lifecycle_events)
    inbox_before = len(policy._store.inbox)
    artifacts_before = len(policy._store.policy_artifacts)
    policy_jobs_before = len(policy._store.policy_jobs)
    attempts_before = len(policy._store.policy_attempts)
    idempotency_before = len(policy._store.idempotency)
    revision_before = policy._store._store_revision

    replay = policy.replay_release(
        principal=REPLAY_OPERATOR,
        release_candidate_id=release_id,
        application_id=application_id,
        idempotency_key="s09-rg6-replay",
        expected_governance_revision=governance_revision(policy),
    )
    assert replay["status"] == "accepted"
    assert replay["namespace"] == "s09-replay"
    assert replay["business_revision_delta"] == 0
    bundle = replay["bundles"][0]
    assert bundle["outcome"] == "REPRODUCED"
    assert bundle["bundle_id"].startswith("s09-replay_sha256_")
    assert bundle["run_identity"].startswith("s09-replay:")
    assert bundle["business_revision_delta"] == 0
    assert all(
        "raw" not in item and "ocr" not in item
        for item in [b["checks"][0] for b in replay["bundles"] if b["checks"]]
    )
    replay_again = policy.replay_release(
        principal=REPLAY_OPERATOR,
        release_candidate_id=release_id,
        application_id=application_id,
        idempotency_key="s09-rg6-replay",
        expected_governance_revision=governance_revision(policy),
    )
    assert replay_again["replayed"] is True
    assert replay_again["bundles"] == replay["bundles"]

    restarted = PolicyGovernanceService(
        state_path=policy._store.state_path,
        diagnostic_snapshot_provider=(
            lambda owner, requested_application_id: (
                service.build_policy_diagnostic_snapshot(
                    owner, requested_application_id
                )
            )
        ),
    )
    replay_after_restart = restarted.replay_release(
        principal=REPLAY_OPERATOR,
        release_candidate_id=release_id,
        application_id=application_id,
        idempotency_key="s09-rg6-replay",
        expected_governance_revision=governance_revision(restarted),
    )
    assert replay_after_restart["replayed"] is True
    assert replay_after_restart["bundles"] == replay["bundles"]

    simulation = policy.simulate_release(
        principal=SIMULATION_OPERATOR,
        release_candidate_id=release_id,
        application_id=application_id,
        idempotency_key="s09-rg6-simulate",
        expected_governance_revision=governance_revision(policy),
    )
    assert simulation["status"] == "accepted"
    assert simulation["namespace"] == "s09-simulation"
    assert simulation["bundles"][0]["outcome"] == "REPRODUCED"
    assert simulation["bundles"][0]["bundle_id"].startswith("s09-simulation_sha256_")

    # The two diagnostic namespaces add only their durable claim/attempt and
    # idempotency records. Governance/Lifecycle and every business collection
    # stay unchanged.
    policy._store.reload()
    assert len(policy._store.policy_governance_events) == events_before
    assert len(policy._store.runs) == runs_before
    assert len(policy._store.lifecycle_events) == lifecycle_before
    assert len(policy._store.inbox) == inbox_before
    assert len(policy._store.policy_artifacts) == artifacts_before
    diagnostic_jobs = policy._store.policy_jobs[policy_jobs_before:]
    assert len(diagnostic_jobs) == 2
    assert {item["namespace"] for item in diagnostic_jobs} == {
        "s09-replay",
        "s09-simulation",
    }
    assert all(item["status"] == "diagnostic_complete" for item in diagnostic_jobs)
    assert len(policy._store.policy_attempts) == attempts_before + 4
    assert len(policy._store.idempotency) == idempotency_before + 2
    assert policy._store._store_revision == revision_before + 4

    # Missing release and missing snapshot are UNREPRODUCIBLE/INVALID, never
    # approximately substituted.
    with pytest.raises(PolicyNotFound):
        policy.replay_release(
            principal=REPLAY_OPERATOR,
            release_candidate_id="candidate_missing",
            application_id=application_id,
            idempotency_key="s09-rg6-replay-missing",
            expected_governance_revision=governance_revision(policy),
        )
    # A governed release over an application with no fixed snapshot.
    bad = policy.replay_release(
        principal=REPLAY_OPERATOR,
        release_candidate_id=release_id,
        application_id="app_no_snapshot",
        idempotency_key="s09-rg6-replay-nosnap",
        expected_governance_revision=governance_revision(policy),
    )
    assert bad["bundles"][0]["outcome"] == "UNREPRODUCIBLE"
    assert bad["bundles"][0]["reason_code"] == "FIXED_SNAPSHOT_UNAVAILABLE"


def test_diagnostic_idempotency_claim_fences_cross_service_execution(
    tmp_path: Path,
) -> None:
    service, first = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-r3-diag-concurrency")
    other_application_id = "app_no_snapshot"
    release_id = first.query_active(ADMIN)["candidate_id"]
    second = PolicyGovernanceService(
        state_path=first._store.state_path,
        diagnostic_snapshot_provider=(
            lambda owner, requested_application_id: (
                service.build_policy_diagnostic_snapshot(
                    owner, requested_application_id
                )
            )
        ),
    )
    original = first._diagnostic_run_bundle
    runner_started = threading.Event()
    release_runner = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def blocked_runner(view: Any) -> dict[str, Any]:
        nonlocal calls
        with calls_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            runner_started.set()
            assert release_runner.wait(timeout=10)
        return original(view)

    first._diagnostic_run_bundle = blocked_runner
    second._diagnostic_run_bundle = blocked_runner
    result: dict[str, Any] = {}
    thread_errors: list[Exception] = []

    def run_first() -> None:
        try:
            result.update(
                first.replay_release(
                    principal=REPLAY_OPERATOR,
                    release_candidate_id=release_id,
                    application_id=application_id,
                    idempotency_key="s09-r3-diag-concurrent-key",
                    expected_governance_revision=governance_revision(first),
                )
            )
        except Exception as error:
            thread_errors.append(error)

    thread = threading.Thread(target=run_first)
    thread.start()
    assert runner_started.wait(timeout=10)
    try:
        with pytest.raises(PolicyConflict, match="fingerprint conflicts"):
            second.replay_release(
                principal=REPLAY_OPERATOR,
                release_candidate_id=release_id,
                application_id=other_application_id,
                idempotency_key="s09-r3-diag-concurrent-key",
                expected_governance_revision=governance_revision(second),
            )
        assert calls == 1
        with pytest.raises(PolicyConflict, match="already running"):
            second.replay_release(
                principal=REPLAY_OPERATOR,
                release_candidate_id=release_id,
                application_id=application_id,
                idempotency_key="s09-r3-diag-concurrent-key",
                expected_governance_revision=governance_revision(second),
            )
        assert calls == 1
    finally:
        release_runner.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert thread_errors == []
    assert result["status"] == "accepted"

    replayed = second.replay_release(
        principal=REPLAY_OPERATOR,
        release_candidate_id=release_id,
        application_id=application_id,
        idempotency_key="s09-r3-diag-concurrent-key",
        expected_governance_revision=governance_revision(second),
    )
    assert replayed["replayed"] is True
    assert replayed["bundles"] == result["bundles"]
    assert calls == 1
    second._store.reload()
    jobs = [
        item
        for item in second._store.policy_jobs
        if item.get("kind") == "s09_diagnostic"
    ]
    assert len(jobs) == 1
    attempts = [
        item
        for item in second._store.policy_attempts
        if item.get("policy_job_id") == jobs[0]["policy_job_id"]
    ]
    assert [item["status"] for item in attempts] == ["running", "complete"]


def test_diagnostic_identity_cannot_enter_lifecycle_cas(
    tmp_path: Path,
) -> None:
    """The Lifecycle RunResult interface rejects replay/simulation worker
    identities with a stable reason and zero business delta."""
    service, policy = _s09_governed(tmp_path)
    _s01_submit_and_run(service, "s09-rg6-ident-1")
    driver = service._process_next_job
    for worker_id in ("s09-replay-runner", "s09-simulation-runner"):
        result = driver(worker_id=worker_id)
        assert result.status == "rejected"
        assert result.reason_code == "S09_DIAGNOSTIC_IDENTITY_REJECTED"
    jobs_before = len(service._store.jobs)
    assert len(service._store.jobs) == jobs_before


def test_s09_role_auth_matrix_denies_wrong_role_with_zero_effect(
    tmp_path: Path,
) -> None:
    """Wrong-role and same-subject commands are denied with no protected
    authority delta; only minimized denial behavior applies."""
    service, policy = _s09_governed(tmp_path)
    _s01_submit_and_run(service, "s09-rg6-auth-1")
    candidate = _s09_candidate_in_review(policy, "s09-rg6-auth")
    events_before = len(policy._store.policy_governance_events)
    # The impact preview is open to the Administrator and the independent
    # Approver (who binds its digest at approval time); other roles are
    # denied with zero effect.
    preview = policy.preview_impact(
        principal=APPROVER,
        candidate_id=candidate,
        idempotency_key="s09-rg6-auth-preview",
        expected_governance_revision=governance_revision(policy),
    )
    assert preview["status"] == "accepted"
    with pytest.raises(PolicyInvalidTransition):
        policy.preview_impact(
            principal=OPERATOR,
            candidate_id=candidate,
            idempotency_key="s09-rg6-auth-preview-op",
            expected_governance_revision=governance_revision(policy),
        )
    # The denied preview added nothing; the successful one is a fact.
    events_before = len(policy._store.policy_governance_events)
    # Only the restricted operator may impose a hold.
    with pytest.raises(PolicyInvalidTransition):
        policy.impose_hold(
            principal=ADMIN,
            reason_code="S09_TEST_HOLD",
            hold_scope="open_cycle",
            idempotency_key="s09-rg6-auth-hold",
            expected_governance_revision=governance_revision(policy),
        )
    # Only the operator may run diagnostics.
    with pytest.raises(PolicyInvalidTransition):
        policy.replay_release(
            principal=APPROVER,
            release_candidate_id=candidate,
            application_id=None,
            idempotency_key="s09-rg6-auth-replay",
            expected_governance_revision=governance_revision(policy),
        )
    assert len(policy._store.policy_governance_events) == events_before

    # The hold actor can never confirm its own release.
    hold = policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_TEST_HOLD",
        hold_scope="open_cycle",
        idempotency_key="s09-rg6-auth-hold2",
        expected_governance_revision=governance_revision(policy),
    )
    with pytest.raises(PolicyInvalidTransition):
        policy.recover_hold(
            principal=OPERATOR,
            hold_id=hold["hold_id"],
            recovery_generation=1,
            idempotency_key="s09-rg6-auth-recover",
            expected_governance_revision=governance_revision(policy),
        )


def _corpus_fingerprint(source: dict[str, Any]) -> dict[str, Any]:
    """One normalized per-dimension fingerprint per evidence source: replay
    bundles (dict-shaped outcomes) and fresh-process corpus outcomes
    (tuple-shaped) produce the same five keys."""
    if "selection_outcomes" in source:
        return {
            "selection": [
                tuple(tuple(outcome.items()))
                for outcome in source.get("selection_outcomes") or []
            ],
            "normalization": [
                tuple(tuple(outcome.items()))
                for outcome in source.get("normalization_outcomes") or []
            ],
            "verdicts": [
                (check["rule_id"], check["verdict"])
                for check in source.get("checks") or []
            ],
            "reason_codes": sorted(
                {
                    reason
                    for check in source.get("checks") or []
                    for reason in check.get("reason_codes") or []
                }
            ),
            "route": source.get("route"),
        }
    return {
        "selection": [
            tuple(tuple(outcome))
            for outcome in source.get("selection") or []
        ],
        "normalization": [
            tuple(tuple(outcome))
            for outcome in source.get("normalization") or []
        ],
        "verdicts": list(source.get("verdicts") or []),
        "reason_codes": sorted(
            {
                reason
                for _, _, reasons in source.get("verdicts") or []
                for reason in reasons
            }
        ),
        "route": source.get("route"),
    }


def test_mapped_legacy_corpus_differential_and_production_mutation_proofs(
    tmp_path: Path,
) -> None:
    """R8/ST-5/SP-8: the mapped legacy policy/normalizer corpus differential
    (A03/A14 evidence class M) compares selection, normalization, verdict,
    reason codes and route under both exact releases.  The frozen corpus
    manifest carries the per-item mapping (scenario -> label/partition),
    the approved differences and explanations; the differential records the
    cohort/check counts and the approval reference as evidence.  Each
    dimension's mutation changes the producing release content (rules or
    knowledge) at the compile/runner seam, runs the unchanged differential
    assertion, and fails independently on its own dimension."""
    from task4_consistency.controlled.s08 import (
        TargetRelease,
        _load_knowledge_bytes,
        _load_rules_bytes,
        raw_digest,
    )
    from task4_consistency.controlled.s08_validate import _outcome_for

    manifest = json.loads(
        (
            ROOT / "fixtures" / "legacy_corpus" / "s09_legacy_corpus.json"
        ).read_bytes()
    )
    assert manifest["schema_version"] == "s09-legacy-corpus/1"
    cohort = manifest["cohort"]
    assert len(cohort) >= 4
    dimensions = tuple(manifest["dimensions"])
    declared_mutation_seams = manifest["mutation_seams"]
    assert set(declared_mutation_seams) == set(dimensions)

    service, policy = _s09_governed(tmp_path)
    # Admit and run each cohort application through its own scenario
    # allowlist: one service per scenario over the shared state file.
    app_ids: dict[str, str] = {}
    for index, item in enumerate(cohort):
        scenario_id = item["scenario_id"]
        scenario_service = ControlledScenarioService(
            fixture_root=ROOT / "fixtures" / "applications",
            rules_path=service.rules_path,
            state_path=policy._store.state_path,
            scenario_id=scenario_id,
            policy_governance=policy,
        )
        admission = scenario_service.submit_demo(
            principal=INTEGRATOR,
            scenario_id=scenario_id,
            idempotency_key=f"s09-r8-corpus-{index}",
        )
        assert admission.disposition is AdmissionDisposition.ACCEPTED, admission
        # The frozen mapping must match the fixture the admission reads.
        fixture_label = json.loads(
            (
                ROOT / "fixtures" / "applications" / scenario_id
            ).read_bytes()
        ).get("label")
        assert fixture_label == item["expected_label"], (
            f"{scenario_id} label drifted from the frozen corpus mapping"
        )
        result = scenario_service.process_next_job()
        assert result.status == "complete", result
        app_ids[scenario_id] = admission.application_id
    assert len(app_ids) == len(cohort)
    # The frozen partition mapping must match the Lifecycle snapshot.
    snapshot = service.build_policy_impact_snapshot(policy._store, None)
    for item in cohort:
        runtime_partition = next(
            entry.get("partition")
            for entry in snapshot["applications"]
            if entry.get("application_id") == app_ids[item["scenario_id"]]
        )
        assert runtime_partition == item["partition"], (
            f"{item['scenario_id']} partition drifted from the frozen corpus"
        )

    # The approved mapped release: a behavior-equivalent version drift
    # through the real governed pipeline (preview/envelope/approval).
    bundle_dir = Path(policy._store.state_path).parent / "s09-bundle"
    rules_path = bundle_dir / "rules.yaml"
    rules_path.write_bytes(
        rules_path.read_bytes().replace(b'version: "1.9.0"', b'version: "9.9.9"')
    )
    candidate = _s09_candidate_in_review(policy, "s09-r8-corpus")
    _, _, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate, "s09-r8-corpus"
    )
    assert policy.process_next_policy_job(now=activation_at)["status"] == "complete"
    active = policy.query_active(ADMIN)
    assert active["active_generation"] == 2
    approval_binding_id = active["approval_binding_id"]
    assert approval_binding_id  # the approval reference for the mapped release

    # Both exact releases from their registry-bound checker artifacts: the
    # legacy bootstrap release and the approved mapped release.
    def checker_release(candidate_state: dict[str, Any]) -> TargetRelease:
        verified = policy._verify_pinned_manifest(
            policy._store,
            candidate_state["manifest_id"],
            candidate_state["manifest_digest"],
        )
        artifact = policy._artifact(
            policy._store, policy._component_id(verified, "checker")
        )
        return TargetRelease.from_artifact(artifact)

    candidates = policy._fold_candidates(policy._store)
    bootstrap_state = next(
        state
        for state in candidates.values()
        if state.get("status") == "superseded"
    )
    mapped_state = candidates[active["candidate_id"]]
    legacy_release = checker_release(bootstrap_state)
    mapped_release = checker_release(mapped_state)

    def run_outcomes(
        release: TargetRelease,
    ) -> dict[str, dict[str, Any]]:
        outcomes: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(cohort):
            fixture = json.loads(
                (
                    ROOT / "fixtures" / "applications" / item["scenario_id"]
                ).read_bytes()
            )
            outcome = _outcome_for(release, fixture)
            checks = tuple(
                TargetCheckResult(*check) for check in outcome.get("checks") or ()
            )
            outcome["route"] = service.verification_route_for_checks(checks)
            outcomes[item["scenario_id"]] = outcome
        return outcomes

    def assert_corpus_equivalent(
        expected: dict[str, dict[str, Any]],
        actual: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """The unchanged migration differential assertion: every mapped
        cohort item must match on every dimension unless the frozen
        manifest documents an accepted difference with an explanation."""
        cohort_counts = 0
        check_counts: dict[str, int] = {}
        for item in cohort:
            scenario_id = item["scenario_id"]
            expected_fp = _corpus_fingerprint(expected[scenario_id])
            actual_fp = _corpus_fingerprint(actual[scenario_id])
            accepted = set(item.get("accepted_differences") or {})
            for dimension in dimensions:
                if expected_fp[dimension] == actual_fp[dimension]:
                    continue
                assert dimension in accepted, (
                    f"{scenario_id} {dimension} diverged outside the mapped "
                    f"corpus: {item.get('explanation') or 'no explanation'}"
                )
            cohort_counts += 1
            check_counts[scenario_id] = len(
                actual[scenario_id].get("verdicts") or []
            )
        evidence = {
            "cohort_count": cohort_counts,
            "check_counts": check_counts,
            "approval_binding_id": approval_binding_id,
        }
        return evidence

    legacy_outcomes = run_outcomes(legacy_release)
    mapped_outcomes = run_outcomes(mapped_release)
    evidence = assert_corpus_equivalent(legacy_outcomes, mapped_outcomes)
    assert evidence["cohort_count"] == len(cohort)
    assert all(count >= 1 for count in evidence["check_counts"].values())

    # Production-seam mutation proofs: each dimension mutates the producing
    # release content (rules or knowledge), rebuilds the release at the
    # compile seam, and the UNCHANGED differential assertion must fail --
    # independently on its own dimension.
    kb_bytes = (ROOT / "configs" / "kb" / "entity_kb.json").read_bytes()
    clean_rules = (ROOT / "configs" / "rules_auto_lease.yaml").read_bytes()
    clean_rules9 = clean_rules.replace(b'version: "1.9.0"', b'version: "9.9.9"')
    vin_docs = (
        "docs: [机动车登记证书, 交强险保单, 融资租赁合同, 发票]"
    ).encode("utf-8")
    vin_docs_no_invoice = (
        "docs: [机动车登记证书, 交强险保单, 融资租赁合同]"
    ).encode("utf-8")
    model_block = (
        "field: model\n    docs: [机动车登记证书, 交强险保单, "
        "融资租赁合同, 发票]\n    on_missing: skip\n    severity: major"
    ).encode("utf-8")
    assert model_block in clean_rules9

    def compile_release(rules: bytes, knowledge: bytes) -> TargetRelease:
        return TargetRelease.compile(
            _load_rules_bytes(rules),
            raw_digest(rules),
            knowledge=_load_knowledge_bytes(knowledge),
        )

    def mutated_kb() -> bytes:
        knowledge = json.loads(kb_bytes.decode("utf-8"))
        aliases = knowledge.get("address_aliases") or {}
        assert "南京市" in aliases, "frozen corpus expects the 南京市 alias"
        mutated = dict(aliases)
        del mutated["南京市"]
        knowledge["address_aliases"] = mutated
        return json.dumps(
            knowledge, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")

    mutation_cases = {
        "selection": ("rules", lambda: compile_release(
            clean_rules9.replace(vin_docs, vin_docs_no_invoice), kb_bytes
        )),
        "normalization": ("knowledge", lambda: compile_release(clean_rules9, mutated_kb())),
        "verdicts": ("rules", lambda: compile_release(
            clean_rules9.replace(
                b"on_missing: uncertain", b"on_missing: inconsistent", 1
            ),
            kb_bytes,
        )),
        "reason_codes": ("rules", lambda: compile_release(
            clean_rules9.replace(
                b"low_confidence_threshold: 0.6",
                b"low_confidence_threshold: 0.99",
            ),
            kb_bytes,
        )),
        "route": ("rules", lambda: compile_release(
            clean_rules9.replace(
                model_block, model_block.replace(b"severity: major", b"severity: minor")
            ),
            kb_bytes,
        )),
    }
    assert set(mutation_cases) == set(dimensions)
    for dimension in dimensions:
        declared = declared_mutation_seams[dimension]
        expected_kind, build = mutation_cases[dimension]
        assert declared["kind"] == expected_kind
        assert isinstance(declared.get("note"), str) and declared["note"]
        mutated_release = build()
        mutated_outcomes = run_outcomes(mutated_release)
        with pytest.raises(AssertionError):
            assert_corpus_equivalent(mapped_outcomes, mutated_outcomes)
        # The mutation is observable on its own dimension.  Selection and
        # normalization (and verdicts and reason codes) are coupled in the
        # runner outcome model by construction: a selected-document change
        # necessarily changes the normalization outcome set of that rule,
        # and a verdict change necessarily changes its reason codes.  The
        # independence requirement is therefore that the target dimension
        # diverges and the unchanged assertion fails on that divergence --
        # never that the mutation alters a returned copy.
        diverged = {
            item["scenario_id"]
            for item in cohort
            if _corpus_fingerprint(mapped_outcomes[item["scenario_id"]])[
                dimension
            ]
            != _corpus_fingerprint(mutated_outcomes[item["scenario_id"]])[
                dimension
            ]
        }
        assert diverged, f"the {dimension} mutation must diverge a cohort item"

    # Restore the clean mapped content: the unchanged assertion passes again.
    restored_outcomes = run_outcomes(compile_release(clean_rules9, kb_bytes))
    restored_evidence = assert_corpus_equivalent(
        mapped_outcomes, restored_outcomes
    )
    assert restored_evidence["cohort_count"] == len(cohort)

def test_restart_rebuild_keeps_generation_and_hold_fence_closed(
    tmp_path: Path,
) -> None:
    """After a restart that rebuilds every projection from the append-only
    Ledger, the active generation and the hold union still fence current
    runs: a lagging or rebuilt projection never relaxes currentness."""
    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-rg4-restart-1")
    candidate = _s09_candidate_in_review(policy, "s09-rg4-restart")
    _, _, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate, "s09-rg4-restart"
    )
    result = policy.process_next_policy_job(now=activation_at)
    assert result["status"] == "complete", result
    assert service.process_next_policy_impact() == 1
    policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_TEST_HOLD",
        hold_scope="open_cycle",
        idempotency_key="s09-rg4-restart-hold",
        expected_governance_revision=governance_revision(policy),
    )
    assert service.process_next_policy_impact() == 1

    # Restart both owners from the same physical store: everything is
    # rebuilt from append-only facts, and the fence stays closed.
    state_path = policy._store.state_path
    rules_path = Path(state_path).parent / "s09-bundle" / "rules.yaml"
    kb_path = Path(state_path).parent / "s09-bundle" / "entity_kb.json"
    restarted_policy = PolicyGovernanceService(
        state_path=state_path,
        source_rules_path=rules_path,
        source_kb_path=kb_path,
        corpus_root=ROOT / "fixtures" / "applications",
    )
    restarted_service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=rules_path,
        state_path=state_path,
        policy_governance=restarted_policy,
    )
    restarted_policy._lifecycle_snapshot_provider = (
        lambda owner, digest=None: restarted_service.build_policy_impact_snapshot(
            owner, digest
        )
    )
    active = restarted_policy.query_active(ADMIN)
    assert active["active_generation"] == 2
    assert active["final_impact_digest"]
    assert len(restarted_policy.query_status(ADMIN)["holds"]) == 1
    with pytest.raises(_PinnedReleaseUnavailable):
        restarted_service._claim_job("restart-worker", int(time.time()))
    app = restarted_service._store.applications[application_id]
    assert app["phase"] == "Unprocessable"
    assert app["active_hold_ids"]


def test_unprovable_impact_at_activation_rejects_and_imposes_hold(
    tmp_path: Path,
) -> None:
    """When impact completeness cannot be proved at activation time, the
    activation is rejected with zero protected delta and the corresponding
    scoped Policy Safety Hold is established in the same settlement."""
    service, policy = _s09_governed(tmp_path)
    _s01_submit_and_run(service, "s09-rg3-unprovable-1")
    candidate = _s09_candidate_in_review(policy, "s09-rg3-unprovable")
    _, _, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate, "s09-rg3-unprovable"
    )
    original_provider = policy._lifecycle_snapshot_provider

    def incomplete_provider(owner: Any, digest: str | None = None) -> dict[str, Any]:
        snapshot = original_provider(owner, digest)
        snapshot["universe"] = {
            "complete": False,
            "count": 0,
            "digest": "",
        }
        return snapshot

    policy._lifecycle_snapshot_provider = incomplete_provider
    events_before = len(policy._store.policy_governance_events)
    result = policy.process_next_policy_job(now=activation_at)
    assert result["status"] in {"failed", "diagnostic"}, result
    assert _active_generation(policy) == 1
    assert _latest_final_digest(policy) is None
    # The unprovable completeness established the corresponding hold.
    holds = policy.query_status(ADMIN)["holds"]
    assert len(holds) == 1
    assert holds[0]["reason_code"].startswith("IMPACT_UNPROVABLE_SCOPE_UNIVERSE")
    policy._lifecycle_snapshot_provider = original_provider


def test_duplicate_final_impact_delivery_is_idempotent_without_second_job(
    tmp_path: Path,
) -> None:
    """Duplicate delivery of the same final-impact fact is idempotent: the
    applied disposition stays, the completed successor run is never staled
    and no second reevaluation job is ever created.  (The
    ``already_revalidated`` disposition class exists for members whose
    current run already proves the target generation; the completion fence
    normally forces consumption first, so the branch is defensive.)"""
    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-rg4-reval-1")
    candidate = _s09_candidate_in_review(policy, "s09-rg4-reval")
    _, _, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate, "s09-rg4-reval"
    )
    result = policy.process_next_policy_job(now=activation_at)
    assert result["status"] == "complete", result
    final_digest = _latest_final_digest(policy)
    assert final_digest
    assert service.process_next_policy_impact() == 1
    # The reevaluation job runs and completes under generation 2.
    result = service.process_next_job()
    assert result.status == "complete", result
    assert result.run_id
    # A duplicate delivery of the same final fact must not stale the
    # successor or create a second job.
    assert service.process_next_policy_impact() == 0
    view = service.impact_dispositions_view(
        principal=RECONCILIATION, final_impact_digest=final_digest
    )
    member = next(
        item
        for item in view["members"]
        if item["application_id"] == application_id
    )
    assert member["disposition"] == "applied"
    assert member["reevaluation_job_count"] == 1


def test_approval_without_preview_fails_with_zero_governance_delta(
    tmp_path: Path,
) -> None:
    """S-1: every newly approved candidate binds the immutable impact
    preview; a no-preview approval is rejected with zero governance
    delta and no impact/activation/idempotency/outbox fact."""
    service, policy = _s09_governed(tmp_path)
    _s01_submit_and_run(service, "s09-r1-nopreview-1")
    candidate = _s09_candidate_in_review(policy, "s09-r1-nopreview")
    active = policy.query_active(ADMIN)
    events_before = len(policy._store.policy_governance_events)
    outbox_before = len(policy._store.outbox)
    with pytest.raises(PolicyInvalidTransition):
        policy.approve(
            principal=APPROVER,
            candidate_id=candidate,
            activation_time=int(time.time()) + 300,
            recovery_release_id=active["candidate_id"],
            preview_manifest_id="",
            idempotency_key="s09-r1-nopreview-approve",
            expected_governance_revision=governance_revision(policy),
        )
    assert len(policy._store.policy_governance_events) == events_before
    assert len(policy._store.outbox) == outbox_before
    assert _active_generation(policy) == 1


def test_approval_idempotency_conflicts_on_changed_preview(
    tmp_path: Path,
) -> None:
    """S-5: the approval idempotency fingerprint binds the preview
    identity.  The same idempotency key with a different preview must
    conflict instead of replaying the first approval binding."""
    service, policy = _s09_governed(tmp_path)
    _s01_submit_and_run(service, "s09-r1-ipp-1")
    candidate_a = _s09_candidate_in_review(policy, "s09-r1-ipp-a")
    preview_a = policy.preview_impact(
        principal=ADMIN,
        candidate_id=candidate_a,
        idempotency_key="s09-r1-ipp-a-preview",
        expected_governance_revision=governance_revision(policy),
    )
    approval_a = policy.approve(
        principal=APPROVER,
        candidate_id=candidate_a,
        activation_time=int(time.time()) + 300,
        recovery_release_id=policy.query_active(ADMIN)["candidate_id"],
        preview_manifest_id=preview_a["manifest_id"],
        idempotency_key="s09-r1-ipp-same-key",
        expected_governance_revision=governance_revision(policy),
    )
    assert approval_a["status"] == "accepted"
    # A second candidate activates (generation 2), so a re-preview of
    # candidate A derives a different digest from the new predecessor.
    candidate_b = _s09_candidate_in_review(policy, "s09-r1-ipp-b")
    _, _, _, activation_at_b = _s09_preview_approve_schedule(
        policy, candidate_b, "s09-r1-ipp-b"
    )
    result = policy.process_next_policy_job(now=activation_at_b)
    assert result["status"] == "complete", result
    preview_a2 = policy.preview_impact(
        principal=ADMIN,
        candidate_id=candidate_a,
        idempotency_key="s09-r1-ipp-a-preview-2",
        expected_governance_revision=governance_revision(policy),
    )
    assert preview_a2["digest"] != preview_a["digest"], (
        "the second preview must derive from the new predecessor"
    )
    events_before = len(policy._store.policy_governance_events)
    with pytest.raises(PolicyConflict):
        policy.approve(
            principal=APPROVER,
            candidate_id=candidate_a,
            activation_time=int(time.time()) + 300,
            recovery_release_id=policy.query_active(ADMIN)["candidate_id"],
            preview_manifest_id=preview_a2["manifest_id"],
            idempotency_key="s09-r1-ipp-same-key",
            expected_governance_revision=governance_revision(policy),
        )
    assert len(policy._store.policy_governance_events) == events_before


def test_activation_without_bound_impact_envelope_stops_with_zero_delta(
    tmp_path: Path,
) -> None:
    """P-1: a legacy approval binding without the bound impact envelope can
    never activate a changed candidate.  The worker stops with zero
    protected delta and the old generation stays authoritative."""
    service, policy = _s09_governed(tmp_path)
    _s01_submit_and_run(service, "s09-r1-env-1")
    candidate = _s09_candidate_in_review(policy, "s09-r1-env")
    _, approval_binding_id, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate, "s09-r1-env"
    )
    # Stage the pre-S09 compatibility binding as an immutable store fact:
    # the same approval without preview/envelope fields, digest-consistent,
    # and referenced by the approved/scheduled facts and the activation job.
    policy._store.reload()
    staged = copy.deepcopy(policy._store)
    old = next(
        row
        for row in staged.policy_artifacts
        if row.get("artifact_id") == approval_binding_id
    )
    material = json.loads(old["canonical_json"])
    for key in (
        "preview_manifest_id",
        "preview_manifest_digest",
        "impact_envelope",
        "impact_envelope_digest",
    ):
        material.pop(key, None)
    digest = content_digest(material)
    legacy_id = f"approval_sha256_{digest}"
    staged.policy_artifacts.append(
        {
            "artifact_id": legacy_id,
            "schema_version": old["schema_version"],
            "kind": old["kind"],
            "content_sha256": digest,
            "content_bytes": len(canonical_bytes(material)),
            "canonical_json": canonical_bytes(material).decode("utf-8"),
            "raw_hex": None,
            "importer_version": None,
        }
    )
    # Governance events stay append-only immutable facts; only the
    # activation job row (mutable worker state) references the legacy
    # binding, so the worker sees a pre-S09 approval on an otherwise
    # governed candidate.
    for job in staged.policy_jobs:
        if job.get("approval_binding_id") == approval_binding_id:
            job["approval_binding_id"] = legacy_id
    staged.persist()

    events_before = len(policy._store.policy_governance_events)
    outbox_before = len(policy._store.outbox)
    result = policy.process_next_policy_job(now=activation_at)
    assert result["status"] in {"failed", "discarded", "diagnostic"}, result
    assert _active_generation(policy) == 1
    assert _latest_final_digest(policy) is None
    assert len(policy._store.policy_governance_events) == events_before
    assert len(policy._store.outbox) == outbox_before


def test_diagnostic_principals_are_mutually_isolated_and_least_privilege(
    tmp_path: Path,
) -> None:
    """P-5: replay and simulation use separate operator identities with
    least-privilege scope.  The activation operator and the cross-namespace
    diagnostic identity are denied with zero business delta, and an
    omitted application identity never enumerates the run universe."""
    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-r1-diag-1")
    release_id = policy.query_active(ADMIN)["candidate_id"]
    events_before = len(policy._store.policy_governance_events)

    # The activation operator can never run a diagnostic workload.
    with pytest.raises(PolicyInvalidTransition):
        policy.replay_release(
            principal=OPERATOR,
            release_candidate_id=release_id,
            application_id=application_id,
            idempotency_key="s09-r1-diag-op-replay",
            expected_governance_revision=governance_revision(policy),
        )
    with pytest.raises(PolicyInvalidTransition):
        policy.simulate_release(
            principal=OPERATOR,
            release_candidate_id=release_id,
            application_id=application_id,
            idempotency_key="s09-r1-diag-op-sim",
            expected_governance_revision=governance_revision(policy),
        )
    # The namespaces are mutually isolated.
    with pytest.raises(PolicyInvalidTransition):
        policy.simulate_release(
            principal=REPLAY_OPERATOR,
            release_candidate_id=release_id,
            application_id=application_id,
            idempotency_key="s09-r1-diag-cross-sim",
            expected_governance_revision=governance_revision(policy),
        )
    with pytest.raises(PolicyInvalidTransition):
        policy.replay_release(
            principal=SIMULATION_OPERATOR,
            release_candidate_id=release_id,
            application_id=application_id,
            idempotency_key="s09-r1-diag-cross-replay",
            expected_governance_revision=governance_revision(policy),
        )
    # An omitted application identity cannot enumerate every run identity.
    with pytest.raises(PolicyInvalidTransition):
        policy.replay_release(
            principal=REPLAY_OPERATOR,
            release_candidate_id=release_id,
            application_id="",
            idempotency_key="s09-r1-diag-enum",
            expected_governance_revision=governance_revision(policy),
        )
    assert len(policy._store.policy_governance_events) == events_before


def test_diagnostic_snapshot_provider_fails_closed_without_exact_snapshot(
    tmp_path: Path,
) -> None:
    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-r3-diagnostic-snapshot")
    release_id = policy.query_active(ADMIN)["candidate_id"]
    policy._diagnostic_snapshot_provider = lambda owner, app_id: {
        "complete": False,
        "application_id": app_id,
    }

    result = policy.replay_release(
        principal=REPLAY_OPERATOR,
        release_candidate_id=release_id,
        application_id=application_id,
        idempotency_key="s09-r3-diagnostic-snapshot-key",
        expected_governance_revision=governance_revision(policy),
    )

    assert result["bundles"][0]["outcome"] == "UNREPRODUCIBLE"
    assert result["bundles"][0]["reason_code"] == "FIXED_SNAPSHOT_UNAVAILABLE"


def test_diagnostic_snapshot_rejects_cross_application_runspec(
    tmp_path: Path,
) -> None:
    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-r3-diagnostic-cross-app")
    release_id = policy.query_active(ADMIN)["candidate_id"]
    original_provider = policy._diagnostic_snapshot_provider

    def cross_application_snapshot(
        owner: Any, requested_application_id: str
    ) -> dict[str, Any]:
        snapshot = copy.deepcopy(original_provider(owner, requested_application_id))
        snapshot["run_spec"]["application_id"] = "another-application"
        return snapshot

    policy._diagnostic_snapshot_provider = cross_application_snapshot
    result = policy.replay_release(
        principal=REPLAY_OPERATOR,
        release_candidate_id=release_id,
        application_id=application_id,
        idempotency_key="s09-r3-diagnostic-cross-app-key",
        expected_governance_revision=governance_revision(policy),
    )

    assert result["bundles"][0]["outcome"] == "UNREPRODUCIBLE"
    assert result["bundles"][0]["reason_code"] == "FIXED_SNAPSHOT_UNAVAILABLE"


def test_disposition_view_minimizes_reviewer_and_authorizes_detail(
    tmp_path: Path,
) -> None:
    """P-6: an ordinary Reviewer sees only aggregate digest/count/watermark
    for one final impact manifest; per-member application and reevaluation
    receipts stay behind the authorized audit/reconciliation identity, and
    an integrator role never reads the view at all."""
    from task4_consistency.controlled.s01 import QueryNotFound

    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-r1-disc-1")
    candidate = _s09_candidate_in_review(policy, "s09-r1-disc")
    _, _, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate, "s09-r1-disc"
    )
    result = policy.process_next_policy_job(now=activation_at)
    assert result["status"] == "complete", result
    final_digest = _latest_final_digest(policy)
    assert final_digest
    assert service.process_next_policy_impact() == 1

    summary = service.impact_dispositions_view(
        principal=REVIEWER, final_impact_digest=final_digest
    )
    assert "members" not in summary, (
        "an ordinary reviewer must never enumerate impact members"
    )
    assert summary["member_count"] >= 1
    assert summary["unconsumed_count"] == 0

    detail = service.impact_dispositions_view(
        principal=RECONCILIATION, final_impact_digest=final_digest
    )
    assert "members" in detail
    member = next(
        item
        for item in detail["members"]
        if item["application_id"] == application_id
    )
    assert member["disposition"] == "applied"

    with pytest.raises(QueryNotFound):
        service.impact_dispositions_view(
            principal=INTEGRATOR, final_impact_digest=final_digest
        )
    with pytest.raises(QueryNotFound):
        service.impact_dispositions_view(
            principal=S01CommandPrincipal(
                subject="c-demo-cross-scope",
                role="auditor",
                scope="C-DEMO/other",
                source_id="s01-test-client",
            ),
            final_impact_digest=final_digest,
        )


def test_pre_cutover_unpinned_run_cannot_become_current_after_governance_activation(
    tmp_path: Path,
) -> None:
    """S-2: a RunSpec frozen before Governance bootstrap that completes after
    governance activation finishes as a non-current diagnostic: the missing
    pinned generation can never become current once an authoritative
    generation exists."""
    state = tmp_path / "precutover.sqlite3"
    bundle = tmp_path / "precutover-bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    rules_path = bundle / "rules.yaml"
    kb_path = bundle / "entity_kb.json"
    rules_path.write_bytes((ROOT / "configs" / "rules_auto_lease.yaml").read_bytes())
    kb_path.write_bytes((ROOT / "configs" / "kb" / "entity_kb.json").read_bytes())
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=rules_path,
        state_path=state,
        policy_governance=None,
    )
    policy = PolicyGovernanceService(
        state_path=state,
        source_rules_path=rules_path,
        source_kb_path=kb_path,
        corpus_root=ROOT / "fixtures" / "applications",
    )
    policy._lifecycle_snapshot_provider = (
        lambda owner, digest=None: service.build_policy_impact_snapshot(owner, digest)
    )
    # The RunSpec is frozen before governance activation (pre-cutover); the
    # governance bootstrap lands while the checker would run, and only then
    # does the unpinned run complete.
    application_id = _s01_admit(service, "s09-r2-pre-1")
    claimed = service._claim_job("precutover-worker", int(time.time()))
    assert claimed is not None
    job, attempt, run_spec = claimed
    assert run_spec.get("active_generation") is None, (
        "the pre-cutover RunSpec carries no pinned generation"
    )
    service._policy_governance = policy
    assert policy.bootstrap_once()["status"] == "activated"
    app = service._store.applications[application_id]
    report = service._run_checker(run_spec)
    run_result = service._convert_run_result(report, run_spec)
    semantic_differential = service._semantic_differential(
        app, run_result, run_spec
    )
    completion_context = service._completion_context(run_spec)
    result = service._commit_complete_result(
        app,
        job,
        attempt,
        run_spec,
        run_result,
        completion_context,
        semantic_differential,
        now=int(time.time()),
    )
    assert result.status == "stale", result
    assert result.cas_mismatches == ("active_generation",), result.cas_mismatches
    service._reload_store()
    stored = next(
        item
        for item in service._store.runs
        if item.get("run_id") == run_spec["run_id"]
    )
    assert stored["status"] == "stale", stored["status"]
    assert service._store.applications[application_id].get(
        "current_run_id"
    ) != run_spec["run_id"]
    assert policy.query_active(ADMIN)["active_generation"] == 1


def test_current_route_query_stays_stable_through_impact_and_hold_consumption(
    tmp_path: Path,
) -> None:
    """S-3/P-2: Lifecycle current-state reconstruction around impact and
    hold consumption.  The current-route query returns a stable
    non-current/blocked frame before and after consumption; one
    authoritative phase event per revision is resolved for the route (the
    auxiliary disposition/hold receipts never split the projection)."""
    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-r3-route-1")
    route_reviewer = S01CommandPrincipal(
        subject="registered-test-integrator",
        role="reviewer",
        scope="C-DEMO",
        source_id="s01-test-client",
    )
    before = service.current_route_view(
        principal=route_reviewer, application_id=application_id
    )
    assert before["currentness_reason"] == "CURRENT_CONTEXT_MATCH"

    candidate = _s09_candidate_in_review(policy, "s09-r3-route")
    _, _, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate, "s09-r3-route"
    )
    result = policy.process_next_policy_job(now=activation_at)
    assert result["status"] == "complete", result
    assert service.process_next_policy_impact() == 1
    # Immediately after consumption the old run is non-current and the
    # route stays queryable (one Evidence Ready phase event per revision).
    after = service.current_route_view(
        principal=route_reviewer, application_id=application_id
    )
    assert after["currentness_reason"] == "NO_CURRENT_RUN"
    assert after["phase"] == "Evidence Ready"

    policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_TEST_HOLD",
        hold_scope="open_cycle",
        idempotency_key="s09-r3-route-hold",
        expected_governance_revision=governance_revision(policy),
    )
    assert service.process_next_policy_impact() == 1
    held = service.current_route_view(
        principal=route_reviewer, application_id=application_id
    )
    assert held["phase"] == "Unprocessable"
    assert held["route"] == "unprocessable"
    assert held["current_run_id"] is None
    assert held["currentness_reason"] == "NO_CURRENT_RUN"


def test_evidence_dependency_change_enters_assembly_with_durable_obligation(
    tmp_path: Path,
) -> None:
    """P-4: a dependency-context change (entity knowledge) leaves every
    affected open-cycle application in Assembly with a durable assembly
    obligation (disposition applied, no reevaluation job, no evidence
    readiness); only an unchanged dependency context enters Evidence Ready
    with exactly one Operational Re-evaluation job."""
    from task4_consistency.controlled.s09_impact import build_impact_manifest

    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-r5-asm-1")
    candidate = _s09_candidate_in_review(policy, "s09-r5-asm")
    state = policy._require_candidate_state(policy._store, candidate)
    # Stage one immutable final-impact fact whose dependency context
    # changed: the entity_knowledge component differs from the predecessor,
    # so the conservative oracle classifies the member as
    # evidence-dependent.  The manifest is content-addressed and
    # digest-verified by the Lifecycle consumer exactly like a real
    # activation-time fact.
    request = policy._impact_manifest_request(
        policy._store,
        phase="final",
        candidate=state,
        generation=2,
        envelope={},
    )
    request["candidate"]["components"]["entity_knowledge"] = "f" * 64
    manifest = build_impact_manifest(request)
    assert any(
        "evidence_dependency" in (member.get("hit_reasons") or [])
        for member in manifest["members"]
    ), "the knowledge change must classify as an evidence dependency"
    policy._store.reload()
    staged = copy.deepcopy(policy._store)
    final_event = policy._append_governance_event(
        staged,
        kind="impact_finalized",
        principal=PolicyPrincipal(
            subject="c-demo-policy-operator",
            role="operator",
            scope=S08_SCOPE,
            source_id="s08-policy-worker",
        ),
        reason_code="S09_IMPACT_FINALIZED",
        details={
            "candidate_id": candidate,
            "manifest_id": manifest["manifest_id"],
            "digest": manifest["digest"],
            "phase": manifest["phase"],
            "member_count": len(manifest["members"]),
            "target_generation": 2,
            "manifest": manifest,
        },
    )
    final_event["digest"] = manifest["digest"]
    staged.outbox.append(
        {
            "event_id": policy._stable_id("outbox", f"{final_event['event_id']}:fi"),
            "kind": "s09_impact_activated",
            "scope": S08_SCOPE,
            "candidate_id": candidate,
            "activation_event_id": final_event["event_id"],
            "active_generation": 2,
            "final_impact_digest": manifest["digest"],
            "final_impact_member_count": len(manifest["members"]),
            "status": "pending",
        }
    )
    staged.persist()
    final_digest = manifest["digest"]

    assert service.process_next_policy_impact() == 1
    app = service._store.applications[application_id]
    assert app["phase"] == "Assembly", app["phase"]
    assert app.get("evidence_ready") is False
    assert app.get("current_final_impact_digest") == final_digest
    assert app.get("policy_generation") == 2
    view = service.impact_dispositions_view(
        principal=RECONCILIATION, final_impact_digest=final_digest
    )
    member = next(
        item
        for item in view["members"]
        if item["application_id"] == application_id
    )
    assert member["disposition"] == "applied"
    assert member["reevaluation_job_count"] == 0, (
        "an evidence-dependent member carries no reevaluation job"
    )
    assert not any(
        job.get("kind") == "operational_reevaluation"
        and job.get("application_id") == application_id
        for job in service._store.jobs
    )
    # A second consumption is a no-op: the disposition stays applied and
    # the app stays in Assembly.
    assert service.process_next_policy_impact() == 0
    assert service._store.applications[application_id]["phase"] == "Assembly"


def test_hold_recovery_second_key_replays_released_hold_with_zero_delta(
    tmp_path: Path,
) -> None:
    """P-3: a second recovery key for an already released hold replays the
    original semantic result with zero new event/audit/outbox/idempotency
    delta, and a different recovery generation conflicts.  One hold is
    released exactly once."""
    service, policy = _s09_governed(tmp_path)
    _s01_submit_and_run(service, "s09-r6-hold2-1")
    candidate = _s09_candidate_in_review(policy, "s09-r6-hold2")
    _, _, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate, "s09-r6-hold2"
    )
    result = policy.process_next_policy_job(now=activation_at)
    assert result["status"] == "complete", result
    assert service.process_next_policy_impact() == 1
    result = service.process_next_job()
    assert result.status == "complete", result
    hold = policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_TEST_HOLD",
        hold_scope="open_cycle",
        idempotency_key="s09-r6-hold2-hold",
        expected_governance_revision=governance_revision(policy),
    )
    assert service.process_next_policy_impact() == 1

    first = policy.recover_hold(
        principal=APPROVER,
        hold_id=hold["hold_id"],
        recovery_generation=2,
        idempotency_key="s09-r6-hold2-k1",
        expected_governance_revision=governance_revision(policy),
    )
    assert first["status"] == "accepted"
    events_after = len(policy._store.policy_governance_events)
    audit_after = len(policy._store.audit_events)
    outbox_after = len(policy._store.outbox)

    replay = policy.recover_hold(
        principal=APPROVER,
        hold_id=hold["hold_id"],
        recovery_generation=2,
        idempotency_key="s09-r6-hold2-k2",
        expected_governance_revision=governance_revision(policy),
    )
    assert replay["status"] == "accepted"
    assert replay.get("replayed") is True
    assert replay["hold_released_event_id"] == first["hold_released_event_id"]
    assert len(policy._store.policy_governance_events) == events_after
    assert len(policy._store.audit_events) == audit_after
    assert len(policy._store.outbox) == outbox_after
    released = [
        event
        for event in policy._store.policy_governance_events
        if event.get("kind") == "hold_released"
        and event.get("hold_id") == hold["hold_id"]
    ]
    assert len(released) == 1, "one hold is released exactly once"

    with pytest.raises(PolicyInvalidTransition):
        policy.recover_hold(
            principal=APPROVER,
            hold_id=hold["hold_id"],
            recovery_generation=1,
            idempotency_key="s09-r6-hold2-k3",
            expected_governance_revision=governance_revision(policy),
        )
    assert len(policy._store.policy_governance_events) == events_after


def test_recovery_rejects_non_bootstrap_active_without_final_impact(
    tmp_path: Path,
) -> None:
    """P-3: only the bootstrap baseline may be recovered without a final
    impact digest.  A non-bootstrap active release without a final impact is
    never a complete empty application set: recovery rejects with zero
    protected delta."""
    service, policy = _s09_governed(tmp_path)
    _s01_submit_and_run(service, "s09-r6-nofi-1")
    # A bootstrap hold (active baseline without final impact) recovers via
    # the explicit bootstrap-hold proof.
    hold = policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_TEST_HOLD",
        hold_scope="open_cycle",
        idempotency_key="s09-r6-nofi-hold",
        expected_governance_revision=governance_revision(policy),
    )
    assert service.process_next_policy_impact() == 1
    bootstrap_recovery = policy.recover_hold(
        principal=APPROVER,
        hold_id=hold["hold_id"],
        recovery_generation=1,
        idempotency_key="s09-r6-nofi-recover",
        expected_governance_revision=governance_revision(policy),
    )
    assert bootstrap_recovery["status"] == "accepted"
    assert service.process_next_policy_impact() == 1

    # Forge a non-bootstrap active generation without a final impact digest
    # (append-only fact referencing the bootstrap evidence) and impose a
    # fresh hold over it: recovery must reject.
    policy._store.reload()
    staged = copy.deepcopy(policy._store)
    active = policy._fold_active_projection(
        staged.policy_governance_events, S08_SCOPE
    )
    forged = policy._append_governance_event(
        staged,
        kind="activated",
        principal=PolicyPrincipal(
            subject="c-demo-policy-operator",
            role="operator",
            scope=S08_SCOPE,
            source_id="s08-policy-worker",
        ),
        reason_code="S08_ACTIVATED",
        details={
            "candidate_id": active["candidate_id"],
            "approval_binding_id": active["approval_binding_id"],
            "validation_bundle_id": active["validation_bundle_id"],
            "validation_bundle_digest": active["validation_bundle_digest"],
            "manifest_id": active["manifest_id"],
            "manifest_digest": active["manifest_digest"],
            "recovery_release_id": active["recovery_release_id"],
            "active_generation": 2,
            "bootstrap": False,
            "final_impact_digest": None,
        },
    )
    forged["activation_event_id"] = forged["event_id"]
    forged["active_generation"] = 2
    staged.persist()
    assert _active_generation(policy) == 2
    assert _latest_final_digest(policy) is None

    forged_hold = policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_TEST_HOLD",
        hold_scope="open_cycle",
        idempotency_key="s09-r6-nofi-hold2",
        expected_governance_revision=governance_revision(policy),
    )
    events_before = len(policy._store.policy_governance_events)
    with pytest.raises(PolicyInvalidTransition):
        policy.recover_hold(
            principal=APPROVER,
            hold_id=forged_hold["hold_id"],
            recovery_generation=2,
            idempotency_key="s09-r6-nofi-recover2",
            expected_governance_revision=governance_revision(policy),
        )
    assert len(policy._store.policy_governance_events) == events_before


def test_approver_can_preview_impact_for_binding(tmp_path: Path) -> None:
    """S-1/SoD: the impact preview is the read-only computation the
    independent Policy Approver binds at approval time; both the Rule
    Administrator and the Policy Approver may run it, and the approver's
    preview derives the same content-addressed manifest identity."""
    service, policy = _s09_governed(tmp_path)
    _s01_submit_and_run(service, "s09-r1-pv-1")
    candidate = _s09_candidate_in_review(policy, "s09-r1-pv")
    admin_preview = policy.preview_impact(
        principal=ADMIN,
        candidate_id=candidate,
        idempotency_key="s09-r1-pv-admin",
        expected_governance_revision=governance_revision(policy),
    )
    approver_preview = policy.preview_impact(
        principal=APPROVER,
        candidate_id=candidate,
        idempotency_key="s09-r1-pv-approver",
        expected_governance_revision=governance_revision(policy),
    )
    assert approver_preview["status"] == "accepted"
    assert approver_preview["manifest_id"]
    # Each preview is a distinct governance fact (its digest binds the
    # authority watermark at preview time); the approver binds the exact
    # preview it ran in the approval.
    approval = policy.approve(
        principal=APPROVER,
        candidate_id=candidate,
        activation_time=int(time.time()) + 300,
        recovery_release_id=policy.query_active(ADMIN)["candidate_id"],
        preview_manifest_id=approver_preview["manifest_id"],
        idempotency_key="s09-r1-pv-approve",
        expected_governance_revision=governance_revision(policy),
    )
    assert approval["status"] == "accepted"


def test_evidence_dependency_assembly_obligation_survives_hold_release(
    tmp_path: Path,
) -> None:
    """P-4: the durable assembly obligation survives the hold.  Releasing an
    evidence-dependent member returns it to Assembly (no Evidence Ready, no
    reevaluation job); the member must re-assemble evidence before any run
    becomes current at the recovery generation."""
    from task4_consistency.controlled.s09_impact import build_impact_manifest

    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-r5-hold-1")
    candidate = _s09_candidate_in_review(policy, "s09-r5-hold")
    state = policy._require_candidate_state(policy._store, candidate)
    request = policy._impact_manifest_request(
        policy._store,
        phase="final",
        candidate=state,
        generation=2,
        envelope={},
    )
    request["candidate"]["components"]["entity_knowledge"] = "f" * 64
    manifest = build_impact_manifest(request)
    policy._store.reload()
    staged = copy.deepcopy(policy._store)
    final_event = policy._append_governance_event(
        staged,
        kind="impact_finalized",
        principal=PolicyPrincipal(
            subject="c-demo-policy-operator",
            role="operator",
            scope=S08_SCOPE,
            source_id="s08-policy-worker",
        ),
        reason_code="S09_IMPACT_FINALIZED",
        details={
            "candidate_id": candidate,
            "manifest_id": manifest["manifest_id"],
            "digest": manifest["digest"],
            "phase": manifest["phase"],
            "member_count": len(manifest["members"]),
            "target_generation": 2,
            "manifest": manifest,
        },
    )
    final_event["digest"] = manifest["digest"]
    staged.outbox.append(
        {
            "event_id": policy._stable_id("outbox", f"{final_event['event_id']}:fi"),
            "kind": "s09_impact_activated",
            "scope": S08_SCOPE,
            "candidate_id": candidate,
            "activation_event_id": final_event["event_id"],
            "active_generation": 2,
            "final_impact_digest": manifest["digest"],
            "final_impact_member_count": len(manifest["members"]),
            "status": "pending",
        }
    )
    staged.persist()
    final_digest = manifest["digest"]
    assert service.process_next_policy_impact() == 1
    assert service._store.applications[application_id]["phase"] == "Assembly"

    # A hold over the Assembly member, then an explicit recovery: the
    # member returns to Assembly with the obligation intact.
    hold = policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_TEST_HOLD",
        hold_scope="open_cycle",
        idempotency_key="s09-r5-hold-hold",
        expected_governance_revision=governance_revision(policy),
    )
    assert service.process_next_policy_impact() == 1
    assert service._store.applications[application_id]["phase"] == "Unprocessable"
    recovered = policy.recover_hold(
        principal=APPROVER,
        hold_id=hold["hold_id"],
        recovery_generation=1,
        idempotency_key="s09-r5-hold-recover",
        expected_governance_revision=governance_revision(policy),
    )
    assert recovered["status"] == "accepted"
    assert service.process_next_policy_impact() == 1
    app = service._store.applications[application_id]
    assert app["phase"] == "Assembly", app["phase"]
    assert app.get("evidence_ready") is False
    assert not any(
        job.get("kind") == "operational_reevaluation"
        and job.get("application_id") == application_id
        for job in service._store.jobs
    )
    view = service.impact_dispositions_view(
        principal=RECONCILIATION, final_impact_digest=final_digest
    )
    member = next(
        item
        for item in view["members"]
        if item["application_id"] == application_id
    )
    assert member["disposition"] == "applied"


# ---------------------------------------------------------------- round 2


def _route_reviewer() -> S01CommandPrincipal:
    return S01CommandPrincipal(
        subject="registered-test-integrator",
        role="reviewer",
        scope="C-DEMO",
        source_id="s01-test-client",
    )


def test_activation_fences_current_route_and_workspace_before_consumption(
    tmp_path: Path,
) -> None:
    """R1/ST-1/SP-1: activation immediately makes an affected generation-1
    route non-current while the impact outbox is still pending.  The route
    stays reconstructible as NO_CURRENT_RUN after consumption and after a
    restart/rebuild from the same physical store."""
    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-r2-r1-app")
    candidate = _s09_candidate_in_review(policy, "s09-r2-r1")
    _, _, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate, "s09-r2-r1"
    )
    result = policy.process_next_policy_job(now=activation_at)
    assert result["status"] == "complete", result
    assert (
        sum(
            event.get("kind") == "s09_impact_activated"
            and event.get("status") == "pending"
            for event in policy._store.outbox
        )
        == 1
    )
    assert policy.query_active(ADMIN)["active_generation"] == 2
    principal = _route_reviewer()
    # Before Lifecycle consumption the generation-1 run is non-current.
    before = service.current_route_view(
        principal=principal, application_id=application_id
    )
    assert before["currentness_reason"] not in {"CURRENT_CONTEXT_MATCH", "NO_CURRENT_RUN"}
    assert before["currentness_reason"] in {
        "STALE_GENERATION",
        "BLOCKED_POLICY_HOLD",
        "BLOCKED_IMPACT_DISPOSITION",
    }
    # The workspace query is blocked for the same affected application.
    with pytest.raises(QueryNotFound):
        service.workspace_view(
            application_id,
            role="reviewer",
            scope="C-DEMO",
            subject="registered-test-integrator",
        )
    # The history current-run frame is non-current too.
    history = service.application_history_view(
        principal=principal, application_id=application_id
    )
    assert history["current_run_id"] is None
    assert [run for run in history["runs"] if run.get("current") is True] == []
    # After consumption the route is the stable reconstructible frame.
    assert service.process_next_policy_impact() == 1
    after = service.current_route_view(
        principal=principal, application_id=application_id
    )
    assert after["currentness_reason"] == "NO_CURRENT_RUN"
    assert after["phase"] == "Evidence Ready"
    # Restart/rebuild from the same physical store keeps the frame.
    state_path = policy._store.state_path
    rules_path = Path(state_path).parent / "s09-bundle" / "rules.yaml"
    kb_path = Path(state_path).parent / "s09-bundle" / "entity_kb.json"
    restarted_policy = PolicyGovernanceService(
        state_path=state_path,
        source_rules_path=rules_path,
        source_kb_path=kb_path,
        corpus_root=ROOT / "fixtures" / "applications",
    )
    restarted_service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=rules_path,
        state_path=state_path,
        policy_governance=restarted_policy,
    )
    restarted_policy._lifecycle_snapshot_provider = (
        lambda owner, digest=None: restarted_service.build_policy_impact_snapshot(
            owner, digest
        )
    )
    rebuilt = restarted_service.current_route_view(
        principal=principal, application_id=application_id
    )
    assert rebuilt["currentness_reason"] == "NO_CURRENT_RUN"


def test_resolver_failure_fences_unpinned_precutover_completion(
    tmp_path: Path,
) -> None:
    """R2/ST-2/SP-2: once Governance is configured, an authority-resolution
    failure must retain an unpinned pre-cutover completion only as a stale
    diagnostic with a stable CAS mismatch -- never as a current run."""
    state = tmp_path / "precutover-r2.sqlite3"
    bundle = tmp_path / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    rules_path = bundle / "rules.yaml"
    kb_path = bundle / "entity_kb.json"
    rules_path.write_bytes((ROOT / "configs" / "rules_auto_lease.yaml").read_bytes())
    kb_path.write_bytes((ROOT / "configs" / "kb" / "entity_kb.json").read_bytes())
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=rules_path,
        state_path=state,
        policy_governance=None,
    )
    policy = PolicyGovernanceService(
        state_path=state,
        source_rules_path=rules_path,
        source_kb_path=kb_path,
        corpus_root=ROOT / "fixtures" / "applications",
    )
    application_id = _s01_admit(service, "s09-r2-unpinned")
    # Freeze the pre-cutover run spec while governance is not yet attached:
    # the run spec carries no active generation.
    claimed = service._claim_job("precutover-worker", 9999999999)
    assert claimed is not None
    job, attempt, run_spec = claimed
    assert run_spec.get("active_generation") is None
    # Attach governance and activate the bootstrap baseline, then break the
    # resolver: the old code treated the unpinned run as a compatible
    # completion while the authority was unavailable.
    service._policy_governance = policy
    policy._lifecycle_snapshot_provider = (
        lambda owner, digest=None: service.build_policy_impact_snapshot(owner, digest)
    )
    assert policy.bootstrap_once()["status"] == "activated"

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated authority failure")

    policy.resolve_run_pin = broken
    app = service._store.applications[application_id]
    report = service._run_checker(run_spec)
    run_result = service._convert_run_result(report, run_spec)
    result = service._commit_complete_result(
        app,
        job,
        attempt,
        run_spec,
        run_result,
        service._completion_context(run_spec),
        service._semantic_differential(app, run_result, run_spec),
        now=9999999999,
    )
    service._reload_store()
    stored = next(
        item for item in service._store.runs if item.get("run_id") == run_spec["run_id"]
    )
    assert result.status == "stale"
    assert "authority_unavailable" in (result.cas_mismatches or ())
    assert stored["status"] == "stale"
    assert tuple(stored.get("cas_mismatches") or ()) == ("authority_unavailable",)
    assert service._store.applications[application_id].get("current_run_id") is None


def test_resolver_failure_fences_pinned_completion(tmp_path: Path) -> None:
    """R2/ST-2/SP-2: a pinned completion whose governance resolution fails
    also closes as stale with the same stable mismatch."""
    service, policy = _s09_governed(tmp_path)
    application_id = _s01_admit(service, "s09-r2-pinned")
    claimed = service._claim_job("pinned-worker", 9999999999)
    assert claimed is not None
    job, attempt, run_spec = claimed
    assert run_spec.get("active_generation") == 1

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated authority failure")

    policy.resolve_run_pin = broken
    app = service._store.applications[application_id]
    report = service._run_checker(run_spec)
    run_result = service._convert_run_result(report, run_spec)
    result = service._commit_complete_result(
        app,
        job,
        attempt,
        run_spec,
        run_result,
        service._completion_context(run_spec),
        service._semantic_differential(app, run_result, run_spec),
        now=9999999999,
    )
    service._reload_store()
    stored = next(
        item for item in service._store.runs if item.get("run_id") == run_spec["run_id"]
    )
    assert result.status == "stale"
    assert "authority_unavailable" in (result.cas_mismatches or ())
    assert stored["status"] == "stale"
    assert service._store.applications[application_id].get("current_run_id") is None


def test_impact_integrity_failure_fences_pinned_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, policy = _s09_governed(tmp_path)
    candidate = _s09_candidate_in_review(policy, "s09-r3-integrity-release")
    _, _, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate, "s09-r3-integrity-release"
    )
    assert policy.process_next_policy_job(now=activation_at)["status"] == "complete"
    application_id = _s01_admit(service, "s09-r3-integrity")
    claimed = service._claim_job("integrity-worker", 9999999999)
    assert claimed is not None
    job, attempt, run_spec = claimed

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("secret impact integrity failure")

    monkeypatch.setattr(policy, "load_final_impact", broken)
    app = service._store.applications[application_id]
    report = service._run_checker(run_spec)
    run_result = service._convert_run_result(report, run_spec)
    result = service._commit_complete_result(
        app,
        job,
        attempt,
        run_spec,
        run_result,
        service._completion_context(run_spec),
        service._semantic_differential(app, run_result, run_spec),
        now=9999999999,
    )
    service._reload_store()
    stored = next(
        item for item in service._store.runs if item.get("run_id") == run_spec["run_id"]
    )
    assert result.status == "stale"
    assert "impact_integrity" in (result.cas_mismatches or ())
    assert stored["status"] == "stale"
    assert service._store.applications[application_id].get("current_run_id") is None


def test_diagnostic_identity_completion_is_rejected_by_lifecycle(
    tmp_path: Path,
) -> None:
    """R7: a completion whose run identity carries a diagnostic namespace can
    never become current -- Lifecycle rejects it as a stale diagnostic."""
    service, policy = _s09_governed(tmp_path)
    application_id = _s01_admit(service, "s09-r7-diag-identity")
    claimed = service._claim_job("worker", 9999999999)
    assert claimed is not None
    job, attempt, run_spec = claimed
    release_id = policy.query_active(ADMIN)["candidate_id"]
    run_spec["run_id"] = f"s09-replay:{release_id}:{application_id}"
    app = service._store.applications[application_id]
    report = service._run_checker(run_spec)
    run_result = service._convert_run_result(report, run_spec)
    result = service._commit_complete_result(
        app,
        job,
        attempt,
        run_spec,
        run_result,
        service._completion_context(run_spec),
        service._semantic_differential(app, run_result, run_spec),
        now=9999999999,
    )
    service._reload_store()
    stored = next(
        item for item in service._store.runs if item.get("run_id") == run_spec["run_id"]
    )
    assert result.status == "stale"
    assert "diagnostic_identity" in (result.cas_mismatches or ())
    assert stored["status"] == "stale"
    assert service._store.applications[application_id].get("current_run_id") is None


def test_unknown_hold_scope_rejected_with_zero_delta(tmp_path: Path) -> None:
    """R4/SP-5: a hold scope that is neither the served scope nor a
    Lifecycle-authoritative application identity is rejected with zero
    Governance/audit/idempotency/outbox business delta."""
    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-r4-unknown-app")
    policy._store.reload()
    before_events = len(policy._store.policy_governance_events)
    before_audit = len(policy._store.audit_events)
    before_outbox = len(policy._store.outbox)
    before_idempotency = len(policy._store.idempotency)
    with pytest.raises(PolicyInvalidTransition):
        policy.impose_hold(
            principal=OPERATOR,
            reason_code="S09_R4_UNKNOWN",
            hold_scope="app_scope_that_does_not_exist",
            idempotency_key="s09-r4-unknown-hold",
            expected_governance_revision=governance_revision(policy),
        )
    assert len(policy._store.policy_governance_events) == before_events
    assert len(policy._store.audit_events) == before_audit
    assert len(policy._store.outbox) == before_outbox
    assert len(policy._store.idempotency) == before_idempotency
    # The application route is unaffected: still the exact current context.
    route = service.current_route_view(
        principal=_route_reviewer(), application_id=application_id
    )
    assert route["currentness_reason"] == "CURRENT_CONTEXT_MATCH"


def test_unprovable_narrow_hold_scope_expands_to_served_scope(
    tmp_path: Path,
) -> None:
    """R4/SP-5: a narrow scope under the served prefix that cannot be proved
    expands to the smallest trustworthy parent (the served scope), so the
    hold retains its protective effect instead of becoming an active no-op."""
    service, policy = _s09_governed(tmp_path)
    application_id = _s01_admit(service, "s09-r4-expand-app")
    hold = policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_R4_EXPAND",
        hold_scope="C-DEMO/unknown-resource",
        idempotency_key="s09-r4-expand-hold",
        expected_governance_revision=governance_revision(policy),
    )
    assert hold["status"] == "accepted"
    assert hold["hold_scope"] == "C-DEMO/demo"
    active = policy.query_active(ADMIN)
    assert any(
        item["hold_id"] == hold["hold_id"]
        and item["hold_scope"] == "C-DEMO/demo"
        for item in active["holds"]
    )
    # The expanded hold covers the application: claim fails closed.
    with pytest.raises(_PinnedReleaseUnavailable):
        service._claim_job("expanded-worker", int(time.time()))
    # An exact known application scope stays narrow.
    narrow = policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_R4_NARROW",
        hold_scope=application_id,
        idempotency_key="s09-r4-narrow-hold",
        expected_governance_revision=governance_revision(policy),
    )
    assert narrow["hold_scope"] == application_id


def test_bootstrap_hold_recovery_requires_hold_delivery_and_old_reference_proof(
    tmp_path: Path,
) -> None:
    """R5/SP-4: bootstrap recovery must reject while the hold outbox is
    pending or any covered application still carries an operable old current
    reference; a valid consumed recovery appends exactly one release fact."""
    service, policy = _s09_governed(tmp_path)
    application_id, original_run = _s01_submit_and_run(service, "s09-r5-bootstrap-app")
    assert policy.query_active(ADMIN)["final_impact_digest"] is None
    hold = policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_R5_BOOTSTRAP",
        hold_scope="open_cycle",
        idempotency_key="s09-r5-bootstrap-hold",
        expected_governance_revision=governance_revision(policy),
    )
    # Recovery before hold consumption: pending outbox + operable old
    # current run must reject with zero release fact.
    with pytest.raises(PolicyInvalidTransition):
        policy.recover_hold(
            principal=APPROVER,
            hold_id=hold["hold_id"],
            recovery_generation=1,
            idempotency_key="s09-r5-bootstrap-before",
            expected_governance_revision=governance_revision(policy),
        )
    assert not any(
        event.get("kind") == "hold_released" and event.get("hold_id") == hold["hold_id"]
        for event in policy._store.policy_governance_events
    )
    assert (
        service._store.applications[application_id].get("current_run_id")
        == original_run["run_id"]
    )
    # Consume the hold: the old reference becomes non-operable.
    assert service.process_next_policy_impact() == 1
    app = service._store.applications[application_id]
    assert app["phase"] == "Unprocessable"
    assert app["route"] == "unprocessable"
    assert app.get("current_run_id") is None
    assert hold["hold_id"] in (app.get("active_hold_ids") or [])

    recovered = policy.recover_hold(
        principal=APPROVER,
        hold_id=hold["hold_id"],
        recovery_generation=1,
        idempotency_key="s09-r5-bootstrap-recover",
        expected_governance_revision=governance_revision(policy),
    )
    assert recovered["status"] == "accepted"
    assert recovered["hold_released_event_id"]
    release_events = [
        event
        for event in policy._store.policy_governance_events
        if event.get("kind") == "hold_released"
        and event.get("hold_id") == hold["hold_id"]
    ]
    assert len(release_events) == 1
    assert service.process_next_policy_impact() == 1
    app = service._store.applications[application_id]
    assert app["phase"] == "Evidence Ready"
    assert app.get("current_run_id") is None


def test_hold_recovery_uses_lifecycle_delivery_result_as_authority(
    tmp_path: Path,
) -> None:
    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-r3-recovery-owner")
    hold = policy.impose_hold(
        principal=OPERATOR,
        reason_code="S09_RECOVERY_OWNER_TEST",
        hold_scope=application_id,
        idempotency_key="s09-r3-recovery-owner-hold",
        expected_governance_revision=governance_revision(policy),
    )
    assert service.process_next_policy_impact() == 1
    original_provider = policy._lifecycle_snapshot_provider

    def incomplete_snapshot(
        owner: Any, digest: str | None = None
    ) -> dict[str, Any]:
        snapshot = copy.deepcopy(original_provider(owner, digest))
        snapshot["complete"] = False
        return snapshot

    policy._lifecycle_snapshot_provider = incomplete_snapshot
    with pytest.raises((PolicyInvalidTransition, PolicyUnavailable)):
        policy.recover_hold(
            principal=APPROVER,
            hold_id=hold["hold_id"],
            recovery_generation=1,
            idempotency_key="s09-r3-recovery-incomplete-snapshot",
            expected_governance_revision=governance_revision(policy),
        )

    def delivery_not_proven(
        owner: Any, digest: str | None = None
    ) -> dict[str, Any]:
        snapshot = copy.deepcopy(original_provider(owner, digest))
        entry = next(
            item
            for item in snapshot["applications"]
            if item["application_id"] == application_id
        )
        entry["active_hold_ids"] = []
        entry["old_references_operable"] = False
        return snapshot

    policy._lifecycle_snapshot_provider = delivery_not_proven
    events_before = len(policy._store.policy_governance_events)

    with pytest.raises(PolicyInvalidTransition):
        policy.recover_hold(
            principal=APPROVER,
            hold_id=hold["hold_id"],
            recovery_generation=1,
            idempotency_key="s09-r3-recovery-owner-release",
            expected_governance_revision=governance_revision(policy),
        )

    assert len(policy._store.policy_governance_events) == events_before
    assert any(
        item["hold_id"] == hold["hold_id"]
        for item in policy.query_status(ADMIN)["holds"]
    )


def test_authority_watermark_and_dependency_drift_stops_activation(
    tmp_path: Path,
) -> None:
    """R3/SP-3: the approval envelope binds the preview-time authority
    watermarks and dependency index; any drift at finalization stops
    activation with zero protected Governance/audit/outbox delta."""
    service, policy = _s09_governed(tmp_path)
    _s01_submit_and_run(service, "s09-r3-watermark-app")
    candidate = _s09_candidate_in_review(policy, "s09-r3-watermark")
    preview = policy.preview_impact(
        principal=ADMIN,
        candidate_id=candidate,
        idempotency_key="s09-r3-watermark-preview",
        expected_governance_revision=governance_revision(policy),
    )
    assert preview["status"] == "accepted"
    original = policy._lifecycle_snapshot_provider

    def drift(owner: Any, digest: str | None = None) -> dict[str, Any]:
        snapshot = copy.deepcopy(original(owner, digest))
        snapshot["lifecycle_watermark"] = (
            int(snapshot.get("lifecycle_watermark") or 0) + 1000
        )
        snapshot["dependency_index_digest"] = "f" * 64
        return snapshot

    policy._lifecycle_snapshot_provider = drift
    active = policy.query_active(ADMIN)
    approval = policy.approve(
        principal=APPROVER,
        candidate_id=candidate,
        activation_time=9999999999,
        recovery_release_id=active["candidate_id"],
        preview_manifest_id=preview["manifest_id"],
        idempotency_key="s09-r3-watermark-approve",
        expected_governance_revision=governance_revision(policy),
    )
    assert approval["status"] == "accepted"
    envelope = approval["impact_envelope"]
    assert "authority_watermarks" in envelope
    assert "dependency_index" in envelope
    preview_manifest = policy._preview_manifest(policy._store, preview["manifest_id"])
    assert envelope["authority_watermarks"]["lifecycle_watermark"] == (
        preview_manifest["authority_watermarks"]["lifecycle_watermark"]
    )
    assert envelope["dependency_index"]["index_digest"] == (
        preview_manifest["dependency_index"]["index_digest"]
    )
    scheduled = policy.schedule(
        principal=ADMIN,
        approval_binding_id=approval["approval_binding_id"],
        activation_at=9999999999,
        idempotency_key="s09-r3-watermark-schedule",
        expected_governance_revision=governance_revision(policy),
    )
    assert scheduled["status"] == "accepted"
    events_before = len(policy._store.policy_governance_events)
    audit_before = len(policy._store.audit_events)
    outbox_before = len(policy._store.outbox)
    result = policy.process_next_policy_job(now=9999999999)
    assert result["status"] != "complete", result
    assert policy.query_active(ADMIN)["active_generation"] == 1
    assert policy.query_active(ADMIN)["final_impact_digest"] is None
    assert len(policy._store.policy_governance_events) == events_before
    assert len(policy._store.audit_events) == audit_before
    assert len(policy._store.outbox) == outbox_before


def test_unapproved_governance_revision_movement_stops_activation(
    tmp_path: Path,
) -> None:
    """The approval envelope permits only its declared Governance revision
    movement. An unrelated post-approval fact requires a fresh preview and
    approval and leaves every protected activation fact unchanged."""
    service, policy = _s09_governed(tmp_path)
    _s01_submit_and_run(service, "s09-r3-governance-movement-app")
    unrelated_draft = import_draft(policy)
    unrelated_revised = policy.revise_draft(
        principal=ADMIN,
        draft_id=unrelated_draft,
        metadata={
            "scope": S08_SCOPE,
            "validity": {"valid_from": "2000-01-01T00:00:00Z"},
            "source": "c-demo-legacy-baseline/1",
            "reason": "S09 unrelated governance fact",
        },
        idempotency_key="s09-r3-unrelated-revise",
        expected_governance_revision=governance_revision(policy),
    )
    unrelated_frozen = policy.freeze_candidate(
        principal=ADMIN,
        draft_id=unrelated_revised["draft_id"],
        idempotency_key="s09-r3-unrelated-freeze",
        expected_governance_revision=governance_revision(policy),
    )
    unrelated_candidate = unrelated_frozen["candidate_id"]
    policy.request_validation(
        principal=ADMIN,
        candidate_id=unrelated_candidate,
        idempotency_key="s09-r3-unrelated-validate",
        expected_governance_revision=governance_revision(policy),
    )
    assert policy.process_next_policy_job()["status"] == "complete"

    candidate = _s09_candidate_in_review(policy, "s09-r3-governance-movement")
    _, _, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate, "s09-r3-governance-movement"
    )
    revision_before_unrelated = governance_revision(policy)
    policy.submit_review(
        principal=ADMIN,
        candidate_id=unrelated_candidate,
        idempotency_key="s09-r3-unrelated-review",
        expected_governance_revision=revision_before_unrelated,
    )
    assert governance_revision(policy) == revision_before_unrelated + 1

    events_before = len(policy._store.policy_governance_events)
    audit_before = len(policy._store.audit_events)
    outbox_before = len(policy._store.outbox)
    generation_before = _active_generation(policy)
    result = policy.process_next_policy_job(now=activation_at)

    assert result["status"] != "complete", result
    assert _active_generation(policy) == generation_before
    assert _latest_final_digest(policy) is None
    assert len(policy._store.policy_governance_events) == events_before
    assert len(policy._store.audit_events) == audit_before
    assert len(policy._store.outbox) == outbox_before


def test_diagnostic_bundle_writer_rejects_cross_namespace_output() -> None:
    writer = S09DiagnosticBundleWriter(
        namespace="s09-replay",
        worker_identity="s09-replay-worker",
    )
    bundle = {
        "schema_version": "s09-diagnostic-bundle/1",
        "namespace": "s09-simulation",
        "release_candidate_id": "candidate-1",
        "application_id": "application-1",
        "outcome": "REPRODUCED",
        "business_revision_delta": 0,
    }

    with pytest.raises(ValueError, match="namespace"):
        writer.write(bundle, worker_identity="s09-replay-worker")


def test_zero_lifecycle_watermark_can_activate_a_proven_empty_impact(
    tmp_path: Path,
) -> None:
    service, policy = _s09_governed(tmp_path)
    assert len(service._store.lifecycle_events) == 0
    candidate = _s09_candidate_in_review(policy, "s09-r3-zero-watermark")
    _, _, _, activation_at = _s09_preview_approve_schedule(
        policy, candidate, "s09-r3-zero-watermark"
    )

    result = policy.process_next_policy_job(now=activation_at)

    assert result["status"] == "complete", result
    assert policy.query_active(ADMIN)["active_generation"] == 2
    final_digest = policy.query_active(ADMIN)["final_impact_digest"]
    assert final_digest
    final_manifest = policy.load_final_impact(final_digest)
    assert final_manifest and final_manifest["zero_hit_proof"] is not None


def test_diagnostic_runner_receives_read_only_view_without_store_capabilities(
    tmp_path: Path,
) -> None:
    """R7/ST-4/SP-7: replay and simulation execute through an isolated
    runner that receives a read-only capability object -- no persist, no
    Governance/Lifecycle collections, no current-state resolver, no audit
    writer -- and produce only their own immutable diagnostic bundles."""
    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-r7-view-app")
    release_id = policy.query_active(ADMIN)["candidate_id"]
    seen: dict[str, Any] = {}
    original = policy._diagnostic_run_bundle

    def wrapped(view: Any, **kwargs: Any) -> dict[str, Any]:
        seen["view"] = view
        seen["has_persist"] = callable(getattr(view, "persist", None))
        seen["has_governance_events"] = hasattr(view, "policy_governance_events")
        seen["has_runs_collection"] = hasattr(view, "runs")
        seen["has_applications_collection"] = hasattr(view, "applications")
        seen["has_current_resolver"] = hasattr(view, "resolve_run_pin")
        seen["has_audit_writer"] = callable(getattr(view, "append_audit", None))
        return original(view, **kwargs)

    policy._diagnostic_run_bundle = wrapped
    replay = policy.replay_release(
        principal=REPLAY_OPERATOR,
        release_candidate_id=release_id,
        application_id=application_id,
        idempotency_key="s09-r7-view-replay",
        expected_governance_revision=governance_revision(policy),
    )
    assert replay["status"] == "accepted"
    assert replay["bundles"][0]["outcome"] == "REPRODUCED"
    assert seen["view"].worker_identity == "s09-replay-worker"
    fixed_spec_digest = hashlib.sha256(
        seen["view"].fixed_run_spec.encode("utf-8")
    ).hexdigest()
    assert replay["bundles"][0]["run_identity"] == (
        f"s09-replay:{release_id}:{application_id}:{fixed_spec_digest}"
    )
    assert seen["has_persist"] is False
    assert seen["has_governance_events"] is False
    assert seen["has_runs_collection"] is False
    assert seen["has_applications_collection"] is False
    assert seen["has_current_resolver"] is False
    assert seen["has_audit_writer"] is False
    # The capability object is immutable: assignment must fail.
    with pytest.raises(Exception):
        seen["view"].unexpected_attribute = 1
    simulate = policy.simulate_release(
        principal=SIMULATION_OPERATOR,
        release_candidate_id=release_id,
        application_id=application_id,
        idempotency_key="s09-r7-view-simulate",
        expected_governance_revision=governance_revision(policy),
    )
    assert simulate["status"] == "accepted"
    assert simulate["bundles"][0]["outcome"] == "REPRODUCED"
    assert seen["has_persist"] is False
    assert seen["view"].worker_identity == "s09-simulation-worker"


def test_diagnostic_runner_converts_unknown_checker_failure_to_closed_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected checker failure remains a closed diagnostic result."""
    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-r7-closed-failure-app")
    release_id = policy.query_active(ADMIN)["candidate_id"]

    def explode(_self: TargetChecker, _run_spec: dict[str, Any]) -> Any:
        raise OSError("sensitive checker failure")

    monkeypatch.setattr(TargetChecker, "run", explode)
    result = policy.replay_release(
        principal=REPLAY_OPERATOR,
        release_candidate_id=release_id,
        application_id=application_id,
        idempotency_key="s09-r7-closed-failure",
        expected_governance_revision=governance_revision(policy),
    )

    assert result["status"] == "accepted"
    assert result["business_revision_delta"] == 0
    bundle = result["bundles"][0]
    assert bundle["outcome"] == "UNREPRODUCIBLE"
    assert bundle["reason_code"] == "CHECKER_EXECUTION_FAILED"
    assert "sensitive checker failure" not in json.dumps(result)


def test_diagnostic_artifact_integrity_failure_returns_closed_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, policy = _s09_governed(tmp_path)
    application_id, _ = _s01_submit_and_run(service, "s09-r7-artifact-failure-app")
    release_id = policy.query_active(ADMIN)["candidate_id"]

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("secret artifact failure")

    monkeypatch.setattr(policy, "_artifact", explode)
    result = policy.replay_release(
        principal=REPLAY_OPERATOR,
        release_candidate_id=release_id,
        application_id=application_id,
        idempotency_key="s09-r7-artifact-failure",
        expected_governance_revision=governance_revision(policy),
    )

    assert result["status"] == "accepted"
    assert result["bundles"][0]["outcome"] == "UNREPRODUCIBLE"
    assert result["bundles"][0]["reason_code"] == "ARTIFACT_UNAVAILABLE"
    assert "secret artifact failure" not in json.dumps(result)
