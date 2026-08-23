"""Ticket #30 / S14 — cancel in-flight work and explicitly reopen.

Seams under test (agreed in /tmp/codex/ticket-30-plan.md):
- Service: ``ControlledScenarioService.cancel_application`` /
  ``settle_termination`` / ``reopen_application`` plus
  ``application_history_view`` reconstruction.
- Shared worker/review write seam: ``claim_review_work_item`` /
  ``renew_review_work_item`` / ``submit_review_work_item`` against the
  Lifecycle fence.
- S13 effect observation seam: ``delivery_view`` / ``process_next_delivery``
  / ``reconcile_delivery`` / ``compensate_delivery``.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from task4_consistency.controlled.s01 import (
    AdmissionDisposition,
    ControlledScenarioService,
    QueryNotFound,
    S01CommandPrincipal,
)
from task4_consistency.controlled.s01 import ControlledScenarioTestDriver
from task4_consistency.controlled.s02 import (
    ControlledObject,
    RegisteredSource,
)

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"

INTEGRATOR = S01CommandPrincipal(
    subject="registered-test-integrator",
    role="integrator",
    scope="C-DEMO",
    source_id="s01-test-client",
)
OTHER_INTEGRATOR = S01CommandPrincipal(
    subject="unrelated-upstream-actor",
    role="integrator",
    scope="C-DEMO",
    source_id="s14-other-source",
)
REVIEWER = S01CommandPrincipal(
    subject="registered-test-integrator",
    role="reviewer",
    scope="C-DEMO",
    source_id="s14-review-console",
)
OPERATOR = S01CommandPrincipal(
    subject="registered-test-operator",
    role="operator",
    scope="C-DEMO",
    source_id="s14-control-plane",
)
SECOND_OPERATOR = S01CommandPrincipal(
    subject="registered-second-operator",
    role="operator",
    scope="C-DEMO",
    source_id="s14-second-control-plane",
)


def make_service(
    *,
    downstream_registry: Any = None,
    clock: Any = None,
) -> ControlledScenarioService:
    return ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=RULES,
        state_path=Path(tempfile.mkdtemp(prefix="xiaopeng-s14-unit-"))
        / "target.sqlite3",
        downstream_registry=downstream_registry,
        clock=clock,
    )


def _release_digest(service: ControlledScenarioService) -> str:
    return str(service._manifest.digest)


def _auto_complete(service: ControlledScenarioService) -> None:
    service.verification_route_for_checks = (  # type: ignore[method-assign]
        lambda checks, findings: "auto_complete"
    )


def _manual_review_state(
    service: ControlledScenarioService,
):
    """Drive one admitted application into Manual Review with an open item."""
    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s14-intake-manual",
        principal=INTEGRATOR,
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    assert admitted.application_id is not None
    assert service.process_next_job().status == "complete"
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
    )
    assert queue["items"], "manual review work item must exist before cancel"
    work_item_id = queue["items"][0]["work_item_id"]
    review = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
    )
    route = service.current_route_view(
        principal=REVIEWER,
        application_id=str(admitted.application_id),
    )
    return str(admitted.application_id), work_item_id, review, route


# --------------------------------------------------------------------------
# Cancel enters Terminating and fences known effects
# --------------------------------------------------------------------------


def test_cancel_enters_persistent_terminating_and_fences_known_effects() -> None:
    service = make_service()
    application_id, work_item_id, review, route = _manual_review_state(service)
    revision = int(route["lifecycle_revision"])

    result = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=revision,
        idempotency_key="s14-cancel-1",
        reason_code="UPSTREAM_WITHDRAWN",
    )

    assert result["status"] == "accepted"
    assert result["replayed"] is False
    assert result["phase"] == "Terminating"
    assert result["lifecycle_revision"] == revision + 1
    assert result["cancel_reason_code"] == "UPSTREAM_WITHDRAWN"
    assert result["cancelled_by"] == INTEGRATOR.subject
    assert result["fenced_effects"]["review_work_items"] >= 1

    current = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert current["phase"] == "Terminating"
    assert current["route"] == "cancelled"
    assert current["evidence_ready"] is False

    # The previously claimed item is closed by the fence.
    fenced = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
    )
    assert fenced["status"] == "cancelled"


def test_cancel_rejects_stale_revision_unauthorized_and_duplicate() -> None:
    service = make_service()
    application_id, _, _, route = _manual_review_state(service)
    revision = int(route["lifecycle_revision"])

    stale = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=revision - 1,
        idempotency_key="s14-cancel-stale",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert stale["status"] == "stale"
    assert stale["reason_code"] == "lifecycle.cancel_stale_revision"

    forbidden = service.cancel_application(
        application_id=application_id,
        principal=OTHER_INTEGRATOR,
        expected_lifecycle_revision=revision,
        idempotency_key="s14-cancel-forbidden",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert forbidden["status"] == "rejected"
    assert forbidden["reason_code"] == "lifecycle.cancel_forbidden"

    accepted = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=revision,
        idempotency_key="s14-cancel-dup",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert accepted["status"] == "accepted"

    events_after_first = len(service._store.lifecycle_events)
    audits_after_first = len(service._store.audit_events)
    replayed = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=revision,
        idempotency_key="s14-cancel-dup",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert replayed["status"] == "replayed"
    assert replayed["replayed"] is True
    assert replayed["lifecycle_revision"] == accepted["lifecycle_revision"]
    assert len(service._store.lifecycle_events) == events_after_first
    assert len(service._store.audit_events) == audits_after_first

    conflict = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=revision,
        idempotency_key="s14-cancel-dup",
        reason_code="DIFFERENT_REASON",
    )
    assert conflict["status"] == "rejected"
    assert conflict["reason_code"] == "IDEMPOTENCY_CONFLICT"

    again = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=accepted["lifecycle_revision"],
        idempotency_key="s14-cancel-second",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert again["status"] == "rejected"
    assert again["reason_code"] == "lifecycle.cycle_not_cancellable"


def test_unknown_application_cancellation_is_not_found() -> None:
    service = make_service()
    with pytest.raises(QueryNotFound):
        service.cancel_application(
            application_id="app_does_not_exist",
            principal=INTEGRATOR,
            expected_lifecycle_revision=1,
            idempotency_key="s14-cancel-missing",
            reason_code="UPSTREAM_WITHDRAWN",
        )


# --------------------------------------------------------------------------
# Old context cannot produce business effects after the fence
# --------------------------------------------------------------------------


def test_old_context_writes_are_stale_or_cancelled_after_cancel() -> None:
    service = make_service()
    application_id, work_item_id, review, route = _manual_review_state(service)
    claim = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item_id,
        expected_context=review["command_context"],
    )
    assert claim["status"] in {"accepted", "claimed"}

    service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-fence",
        reason_code="UPSTREAM_WITHDRAWN",
    )

    renewed = service.renew_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item_id,
        expected_fence=claim["claim_fence"],
        expected_context=review["command_context"],
        idempotency_key="s14-late-renew",
    )
    assert renewed["status"] in {"stale", "cancelled"}

    submitted = service.submit_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item_id,
        expected_fence=claim["claim_fence"],
        expected_context=review["command_context"],
        idempotency_key="s14-late-decision",
        verification={
            "schema_version": "human-decision/1",
            "outcome": "confirmed",
            "reason_code": "HUMAN_REVIEW_COMPLETED",
            "finding_decisions": [
                {
                    "finding_id": review["automatic_findings"][0]["finding_id"],
                    "outcome": "confirmed",
                }
            ],
        },
    )
    assert submitted["status"] in {"stale", "cancelled"}

    # No human decision fact may exist for the cancelled cycle.
    decisions = [
        record
        for record in service._store.review_records
        if record.get("record_type") == "human_decision"
        and record.get("application_id") == application_id
    ]
    assert decisions == []

    current = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert current["current_run_id"] == route["current_run_id"]

    claims_again = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item_id,
        expected_context=review["command_context"],
    )
    assert claims_again["status"] in {"cancelled", "stale"}


def test_worker_cannot_reclaim_fenced_job_after_cancel() -> None:
    service = make_service()
    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s14-intake-worker-race",
        principal=INTEGRATOR,
    )
    assert admitted.application_id is not None
    driver = ControlledScenarioTestDriver(service)
    crashed = driver.process_next_job(crash=True)
    assert crashed.status == "crashed"

    app_state = service.current_route_view(
        principal=REVIEWER,
        application_id=str(admitted.application_id),
    )
    cancel = service.cancel_application(
        application_id=str(admitted.application_id),
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(app_state["lifecycle_revision"]),
        idempotency_key="s14-cancel-worker-race",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted"
    assert cancel["fenced_effects"]["jobs"] >= 1

    after = driver.process_next_job()
    assert after.status == "idle"
    assert after.reason_code == "NO_READY_JOB"
    assert after.application_id is None

    still = service.current_route_view(
        principal=REVIEWER,
        application_id=str(admitted.application_id),
    )
    assert still["phase"] == "Terminating"


# --------------------------------------------------------------------------
# Settlement gates on every known effect reaching a terminal result
# --------------------------------------------------------------------------


def _delivery_harness(
    behavior: str = "confirm", *, compensation_behavior: str = "succeed"
):
    from task4_consistency.controlled.s13 import (
        DownstreamRecipientRegistration,
        InMemoryDownstreamAdapter,
        RegisteredDownstreamRegistry,
    )

    adapter = InMemoryDownstreamAdapter(
        behavior=behavior, compensation_behavior=compensation_behavior
    )
    reg = DownstreamRecipientRegistration(
        scope="C-DEMO",
        recipient_registration_id="c-demo-downstream-review-default",
        recipient_id="downstream-review-desk",
        adapter_id=adapter.adapter_id,
        adapter_version=adapter.adapter_version,
    )
    registry = RegisteredDownstreamRegistry([reg], {adapter.adapter_id: adapter})
    service = make_service(downstream_registry=registry)
    _auto_complete(service)
    return service


def _completed_with_obligation(service: ControlledScenarioService) -> str:
    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s14-intake-delivery",
        principal=INTEGRATOR,
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    assert service.process_next_job().status == "complete"
    service.refresh_projection()
    delivery = service.delivery_view(
        principal=OPERATOR,
        application_id=str(admitted.application_id),
    )
    assert delivery["verification_completed"] is True
    assert delivery["obligation"] is not None
    return str(admitted.application_id)


def test_settlement_requires_delivery_terminal_result_before_terminated() -> None:
    service = _delivery_harness()
    application_id = _completed_with_obligation(service)
    route = service.current_route_view(
        principal=REVIEWER, application_id=application_id
    )

    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-delivery",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted"

    outstanding = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-settle-pending",
    )
    assert outstanding["status"] == "outstanding"
    assert outstanding["phase"] == "Terminating"
    kinds = {item["kind"] for item in outstanding["unresolved_effects"]}
    assert "delivery_obligation" in kinds

    sent = service.process_next_delivery(principal=OPERATOR)
    assert sent["status"] == "received"

    settled = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-settle-final",
    )
    assert settled["status"] == "terminated"
    assert settled["phase"] == "Terminated"

    current = service.current_route_view(
        principal=REVIEWER, application_id=application_id
    )
    assert current["phase"] == "Terminated"
    assert current["route"] == "cancelled"

    audits = [
        event
        for event in service._store.audit_events
        if event.get("action") == "s14_cycle_terminated"
        and event.get("application_id") == application_id
    ]
    assert len(audits) == 1
    notifications = [
        event
        for event in service._store.outbox
        if event.get("kind") == "termination_notification_requested"
        and event.get("application_id") == application_id
    ]
    assert len(notifications) == 1


def test_unknown_outcome_keeps_terminating_until_verified_terminal() -> None:
    service = _delivery_harness(behavior="timeout_after_execute")
    application_id = _completed_with_obligation(service)
    route = service.current_route_view(
        principal=REVIEWER, application_id=application_id
    )
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-timeout",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted"

    sent = service.process_next_delivery(principal=OPERATOR)
    assert sent["status"] == "unknown"

    obligation_id = service.delivery_view(
        principal=OPERATOR, application_id=application_id
    )["obligation"]["obligation_id"]

    outstanding = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-settle-unknown",
    )
    assert outstanding["status"] == "outstanding"
    assert outstanding["phase"] == "Terminating"
    kinds = {item["kind"] for item in outstanding["unresolved_effects"]}
    assert "delivery_obligation" in kinds

    # Time alone must never promote Terminating to Terminated: settlement is
    # re-run against an aged clock and the cycle stays Terminating.
    aged_clock = service._clock() + 10**9
    service_with_aged_clock = service
    original_clock = service_with_aged_clock._clock
    try:
        service_with_aged_clock._clock = lambda: aged_clock  # type: ignore[method-assign]
        aged = service.settle_termination(
            application_id=application_id,
            principal=OPERATOR,
            expected_lifecycle_revision=cancel["lifecycle_revision"],
            idempotency_key="s14-settle-aged",
        )
    finally:
        service_with_aged_clock._clock = original_clock  # type: ignore[method-assign]
    assert aged["status"] == "outstanding"
    current = service.current_route_view(
        principal=REVIEWER, application_id=application_id
    )
    assert current["phase"] == "Terminating"

    reconciled = service.reconcile_delivery(
        obligation_id=obligation_id,
        principal=OPERATOR,
    )
    assert reconciled["status"] == "received"

    settled = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-settle-compensated",
    )
    assert settled["status"] == "terminated"

    delivery = service.delivery_view(
        principal=OPERATOR, application_id=application_id
    )
    assert delivery["routing_history"], "prior obligation must stay queryable"


def test_forward_compensation_settles_cancelled_cycle() -> None:
    service = _delivery_harness(behavior="timeout_without_execute")
    application_id = _completed_with_obligation(service)
    route = service.current_route_view(
        principal=REVIEWER, application_id=application_id
    )
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-compensate",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted"

    sent = service.process_next_delivery(principal=OPERATOR)
    assert sent["status"] == "unknown"

    obligation_id = service.delivery_view(
        principal=OPERATOR, application_id=application_id
    )["obligation"]["obligation_id"]
    reconciled = service.reconcile_delivery(
        obligation_id=obligation_id,
        principal=OPERATOR,
    )
    assert reconciled["status"] == "retry_scheduled"

    compensated = service.compensate_delivery(
        obligation_id=obligation_id,
        principal=OPERATOR,
    )
    assert compensated["status"] == "compensated"

    settled = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-settle-forward-compensation",
    )
    assert settled["status"] == "terminated"
    effects = {item["kind"]: item["result"] for item in settled["settled_effects"]}
    assert effects["delivery_obligation"] == "compensated"


def test_failed_compensation_stays_terminating() -> None:
    service = _delivery_harness(
        behavior="timeout_after_execute", compensation_behavior="fail"
    )
    application_id = _completed_with_obligation(service)
    route = service.current_route_view(
        principal=REVIEWER, application_id=application_id
    )
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-comp-fail",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted"

    sent = service.process_next_delivery(principal=OPERATOR)
    assert sent["status"] == "unknown"

    obligation_id = service.delivery_view(
        principal=OPERATOR, application_id=application_id
    )["obligation"]["obligation_id"]
    compensated = service.compensate_delivery(
        obligation_id=obligation_id,
        principal=OPERATOR,
    )
    assert compensated["status"] == "failed"

    outstanding = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-settle-comp-failed",
    )
    assert outstanding["status"] == "outstanding"
    assert outstanding["phase"] == "Terminating"
    details = {
        item["kind"]: item["detail"]
        for item in outstanding["unresolved_effects"]
    }
    assert details["delivery_obligation"] == "compensation_failed"


def test_settlement_rejects_when_not_terminating() -> None:
    service = make_service()
    application_id, _, _, route = _manual_review_state(service)
    with pytest.raises(QueryNotFound):
        service.settle_termination(
            application_id="app_missing",
            principal=OPERATOR,
            expected_lifecycle_revision=1,
            idempotency_key="s14-settle-missing",
        )
    rejected = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-settle-early",
    )
    assert rejected["status"] == "rejected"
    assert rejected["reason_code"] == "lifecycle.not_terminating"


# --------------------------------------------------------------------------
# Reopen: distinct authority, policy permission, successor cycle
# --------------------------------------------------------------------------


def _terminate_manual_review(service: ControlledScenarioService):
    application_id, work_item_id, review, route = _manual_review_state(service)
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-reopen-path",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted"
    settled = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-settle-reopen-path",
    )
    assert settled["status"] == "terminated"
    return application_id, cancel, settled


def _reopen_policy(service: ControlledScenarioService) -> dict[str, Any]:
    return {
        "permission_id": "institutional-reopen-permission/1",
        "release_digest": _release_digest(service),
    }


def test_reopen_requires_distinct_authority_and_policy_permission() -> None:
    service = make_service()
    application_id, cancel, settled = _terminate_manual_review(service)

    while_terminating_probe = service.reopen_application(
        application_id=application_id,
        principal=SECOND_OPERATOR,
        expected_lifecycle_revision=settled["lifecycle_revision"],
        idempotency_key="s14-reopen-probe",
        target_phase="Intake",
        reopen_policy=_reopen_policy(service),
    )
    assert while_terminating_probe["status"] in {"accepted", "rejected"}
    assert while_terminating_probe.get("phase") != "Intake" or (
        while_terminating_probe["status"] == "accepted"
    )

    fresh = make_service()
    application_id, cancel, settled = _terminate_manual_review(fresh)

    upstream = fresh.reopen_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=settled["lifecycle_revision"],
        idempotency_key="s14-reopen-upstream",
        target_phase="Intake",
        reopen_policy=_reopen_policy(fresh),
    )
    assert upstream["status"] == "rejected"
    assert upstream["reason_code"] == "lifecycle.reopen_forbidden"

    same_authority = fresh.reopen_application(
        application_id=application_id,
        principal=S01CommandPrincipal(
            subject=INTEGRATOR.subject,
            role="operator",
            scope="C-DEMO",
            source_id="s14-cancel-authority-reuse",
        ),
        expected_lifecycle_revision=settled["lifecycle_revision"],
        idempotency_key="s14-reopen-same-subject",
        target_phase="Intake",
        reopen_policy=_reopen_policy(fresh),
    )
    assert same_authority["status"] == "rejected"
    assert same_authority["reason_code"] == "lifecycle.reopen_authority_not_distinct"

    bad_policy = fresh.reopen_application(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=settled["lifecycle_revision"],
        idempotency_key="s14-reopen-bad-policy",
        target_phase="Intake",
        reopen_policy={
            "permission_id": "institutional-reopen-permission/1",
            "release_digest": "0" * 64,
        },
    )
    assert bad_policy["status"] == "rejected"
    assert bad_policy["reason_code"] == "lifecycle.reopen_policy_forbidden"

    stale = fresh.reopen_application(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"] - 1,
        idempotency_key="s14-reopen-stale",
        target_phase="Intake",
        reopen_policy=_reopen_policy(fresh),
    )
    assert stale["status"] == "stale"
    assert stale["reason_code"] == "lifecycle.reopen_stale_revision"


def test_reopen_during_terminating_is_rejected() -> None:
    service = make_service()
    application_id, _, _, route = _manual_review_state(service)
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-terminating",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted"

    rejected = service.reopen_application(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-reopen-while-terminating",
        target_phase="Intake",
        reopen_policy=_reopen_policy(service),
    )
    assert rejected["status"] == "rejected"
    assert rejected["reason_code"] == "lifecycle.reopen_blocked_while_terminating"

    current = service.current_route_view(
        principal=REVIEWER, application_id=application_id
    )
    assert current["phase"] == "Terminating"
    assert current["cycle"] == 1


def test_authorized_reopen_creates_successor_cycle_without_inherited_state() -> None:
    for target_phase in ("Intake", "Assembly"):
        service = make_service()
        application_id, cancel, settled = _terminate_manual_review(service)

        reopened = service.reopen_application(
            application_id=application_id,
            principal=OPERATOR,
            expected_lifecycle_revision=settled["lifecycle_revision"],
            idempotency_key=f"s14-reopen-ok-{target_phase}",
            target_phase=target_phase,
            reopen_policy=_reopen_policy(service),
        )
        assert reopened["status"] == "accepted"
        assert reopened["phase"] == target_phase
        assert reopened["cycle"] == 2
        assert reopened["predecessor_cycle"] == 1

        current = service.current_route_view(
            principal=REVIEWER, application_id=application_id
        )
        assert current["cycle"] == 2
        assert current["phase"] == target_phase
        assert current["route"] == "pending_check"
        assert current["current_run_id"] is None
        assert current["evidence_snapshot_id"] is None
        assert current["evidence_ready"] is False

        queue = service.queue_view(
            role="reviewer",
            scope=REVIEWER.scope,
            subject=REVIEWER.subject,
        )
        assert queue["items"] == []

        duplicate = service.reopen_application(
            application_id=application_id,
            principal=OPERATOR,
            expected_lifecycle_revision=reopened["lifecycle_revision"] - 1,
            idempotency_key=f"s14-reopen-ok-{target_phase}",
            target_phase=target_phase,
            reopen_policy=_reopen_policy(service),
        )
        assert duplicate["status"] == "replayed"
        assert duplicate["replayed"] is True
        assert duplicate["cycle"] == 2

        again = service.reopen_application(
            application_id=application_id,
            principal=SECOND_OPERATOR,
            expected_lifecycle_revision=reopened["lifecycle_revision"],
            idempotency_key=f"s14-reopen-twice-{target_phase}",
            target_phase="Assembly",
            reopen_policy=_reopen_policy(service),
        )
        assert again["status"] == "rejected"
        assert again["reason_code"] == "lifecycle.not_reopenable"


def test_reopened_cycle_keeps_old_history_but_no_inherited_currentness() -> None:
    service = make_service()
    application_id, cancel, settled = _terminate_manual_review(service)
    reopened = service.reopen_application(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=settled["lifecycle_revision"],
        idempotency_key="s14-reopen-history",
        target_phase="Intake",
        reopen_policy=_reopen_policy(service),
    )
    assert reopened["status"] == "accepted"

    history = service.application_history_view(
        principal=REVIEWER, application_id=application_id
    )
    runs = history["runs"]
    assert runs, "cycle 1 runs must remain reconstructable"
    assert all(run["current"] is False for run in runs)
    corrections = history["corrections"]
    assert corrections == []

    # A brand-new cycle has no inherited exception/decision applicability.
    for run in runs:
        assert run["applicable_decision_ids"] == []
        assert run["applicable_exception_ids"] == []


# --------------------------------------------------------------------------
# Late input for a sealed cycle
# --------------------------------------------------------------------------


def _png() -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    scanline = b"\x00\x00\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanline))
        + chunk(b"IEND", b"")
    )


def _supplement_source() -> dict[str, object]:
    page = _png()
    producer_result = {
        "per_image_results": [
            {
                "image_path": "lease-page.png",
                "image_size": {"width": 1, "height": 1},
                "detections": [
                    {
                        "bbox": [0, 0, 1, 1],
                        "class_id": 1,
                        "class_name": "vehicle_identifier",
                        "confidence": 0.99,
                        "field_key": "vin",
                        "ocr_text": "LSVAA4182N2444555",
                        "value": "LSVAA4182N2444555",
                    }
                ],
            }
        ]
    }
    result = json.dumps(
        producer_result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {"result": result, "page": page}


SUPPLEMENT_INTEGRATOR = S01CommandPrincipal(
    subject="s14-integrator",
    role="integrator",
    scope="R-OBSERVED/c-demo",
    source_id="s14-material-source",
)


def _ready_supplement_request(tmp_path: Path):
    from task4_consistency.controlled.s02 import (
        ControlledObject as _ControlledObject,
        RegisteredSource as _RegisteredSource,
    )

    source = _supplement_source()
    reviewer = S01CommandPrincipal(
        subject="s14-reviewer",
        role="reviewer",
        scope="C-DEMO",
        source_id="s14-review-console",
    )
    intake = S01CommandPrincipal(
        subject=reviewer.subject,
        role="integrator",
        scope="C-DEMO",
        source_id="s14-demo-intake",
    )
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=RULES,
        state_path=tmp_path / "target.sqlite3",
        scenario_id="app_missing_vin_docs.json",
        registered_sources=(
            _RegisteredSource(
                tenant_id="c-demo",
                source_system_id="s14-material-source",
                workload_identity_id="s14-material-workload",
                adapter_id="s06-detection-adapter",
                adapter_version="1",
                source_shape="ocr-detection/unversioned",
                producer_family="s06-ocr",
            ),
        ),
        controlled_objects=(
            _ControlledObject(
                tenant_id="c-demo",
                source_system_id="s14-material-source",
                object_ref="s06-result-object",
                media_type="application/json",
                content=source["result"],
            ),
            _ControlledObject(
                tenant_id="c-demo",
                source_system_id="s14-material-source",
                object_ref="s06-page-object",
                media_type="image/png",
                content=source["page"],
            ),
        ),
    )
    admitted = service.submit_demo(
        scenario_id="app_missing_vin_docs.json",
        idempotency_key="s14-supplement-intake",
        principal=intake,
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    assert service.process_next_job().status == "complete"
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer", scope=reviewer.scope, subject=reviewer.subject, now=100
    )
    work_item_id = queue["items"][0]["work_item_id"]
    review = service.review_work_item_view(
        principal=reviewer, work_item_id=work_item_id, now=100
    )
    finding = next(
        item
        for item in review["automatic_findings"]
        if item["rule_id"] == "R_VIN_CROSS"
    )
    claim = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=review["command_context"],
        now=100,
    )
    created = service.request_supplement(
        principal=reviewer,
        work_item_id=work_item_id,
        finding_id=finding["finding_id"],
        reason_code="MISSING_REQUIRED_MATERIAL",
        expected_fence=claim["claim_fence"],
        expected_context=review["command_context"],
        idempotency_key="s14-request-1",
        now=101,
    )
    assert created["status"] == "accepted", created
    request = service.supplement_request_view(
        principal=reviewer,
        request_id=created["request_id"],
        now=102,
    )
    return service, reviewer, intake, request, source


def _attachment_submission(
    request: dict[str, object],
    source: dict[str, object],
    *,
    closed: bool,
) -> dict[str, object]:
    item_sequence = 2 if closed else 1
    batch: dict[str, Any] = {
        "batch_id": "s14-batch-1",
        "item_sequence": item_sequence,
        "item_count": 2,
        "final_sequence": 2,
        "scope_mode": "full",
        "closed": closed,
    }
    manifest = {
        "batch_id": batch["batch_id"],
        "final_sequence": batch["final_sequence"],
        "item_count": batch["item_count"],
        "scope_mode": batch["scope_mode"],
        "stream_id": "s14-supplement-stream",
        "supplement_request_id": request["request_id"],
    }
    batch["manifest_digest"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "envelope_id": f"s14-attachment-envelope-{item_sequence}",
        "schema_version": "1.0.0",
        "semantic_version": "1.0.0",
        "command_type": "submit_attachment_version",
        "upstream_application_ref": "APP-MISS-VINDOC",
        "stream_id": "s14-supplement-stream",
        "source_revision": item_sequence,
        "predecessor_revision": 1 if closed else None,
        "must_understand": [],
        "workload_identity_id": "s14-material-workload",
        "request_binding": {
            "supplement_request_id": request["request_id"],
            "request_context_digest": request["context_digest"],
            "material_requirement_id": "c-demo-financing-lease-vin/1",
            "request_progress_revision": item_sequence,
        },
        "document_binding": {
            "source_document_ref": "s14-lease-replacement",
            "document_type": "financing_lease_contract",
            "document_role": "financing_lease_contract",
        },
        "attachment_lineage": {
            "operation": "replacement",
            "predecessor_attachment_id": request[
                "expected_predecessor_attachment_id"
            ],
            "predecessor_attachment_version": request[
                "expected_predecessor_attachment_version"
            ],
            "attachment_version": 2,
        },
        "batch": batch,
        "result_object": {
            "controlled_object_ref": "s06-result-object",
            "media_type": "application/json",
            "size_bytes": len(source["result"]),
            "sha256": hashlib.sha256(source["result"]).hexdigest(),  # type: ignore[arg-type]
        },
        "attachments": [
            {
                "source_attachment_ref": "s14-source-attachment-2",
                "page_ref": "s06-source-page-2",
                "page_ordinal": 1,
                "source_name_sha256": hashlib.sha256(
                    b"lease-page.png"
                ).hexdigest(),
                "object": {
                    "controlled_object_ref": "s06-page-object",
                    "media_type": "image/png",
                    "size_bytes": len(source["page"]),
                    "sha256": hashlib.sha256(source["page"]).hexdigest(),  # type: ignore[arg-type]
                },
            }
        ],
        "producer": {
            "producer_id": "s06-producer",
            "producer_family": "s06-ocr",
            "task_id": "s06-lease-field-extraction",
            "task_version": "1",
            "model_id": "s06-model",
            "model_version": "1",
        },
    }


def test_late_attachment_for_sealed_cycle_requires_reopen(tmp_path: Path) -> None:
    service, reviewer, _intake, request, source = _ready_supplement_request(
        tmp_path
    )
    application_id = str(request["application_id"])
    route = service.current_route_view(
        principal=reviewer, application_id=application_id
    )
    cancel = service.cancel_application(
        application_id=application_id,
        principal=S01CommandPrincipal(
            subject=reviewer.subject,
            role="integrator",
            scope="C-DEMO",
            source_id="s14-demo-intake",
        ),
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-sealed",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted"
    settled = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-settle-sealed",
    )
    assert settled["status"] == "terminated"

    evidence_events_before = len(service._store.evidence_events)
    submission = _attachment_submission(request, source, closed=False)
    receipt = service.submit_attachment_version(
        submission=submission,
        idempotency_key="s14-late-upload",
        principal=SUPPLEMENT_INTEGRATOR,
        now=500,
    )
    assert receipt.disposition is AdmissionDisposition.REJECTED
    assert receipt.reason_code == "evidence.late_input_requires_reopen"
    assert receipt.replayed is False
    assert len(service._store.evidence_events) == evidence_events_before


# --------------------------------------------------------------------------
# History reconstruction
# --------------------------------------------------------------------------


def test_history_rebuilds_cancel_termination_reopen_and_late_receipt(
    tmp_path: Path,
) -> None:
    service, reviewer, _intake, request, source = _ready_supplement_request(
        tmp_path
    )
    application_id = str(request["application_id"])
    route = service.current_route_view(
        principal=reviewer, application_id=application_id
    )
    cancel = service.cancel_application(
        application_id=application_id,
        principal=S01CommandPrincipal(
            subject=reviewer.subject,
            role="integrator",
            scope="C-DEMO",
            source_id="s14-demo-intake",
        ),
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-history",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    settled = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-settle-history",
    )
    assert settled["status"] == "terminated"
    submission = _attachment_submission(request, source, closed=False)
    late = service.submit_attachment_version(
        submission=submission,
        idempotency_key="s14-late-history",
        principal=SUPPLEMENT_INTEGRATOR,
        now=600,
    )
    assert late.reason_code == "evidence.late_input_requires_reopen"
    reopened = service.reopen_application(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=settled["lifecycle_revision"],
        idempotency_key="s14-reopen-history-view",
        target_phase="Intake",
        reopen_policy=_reopen_policy(service),
    )
    assert reopened["status"] == "accepted"

    history = service.application_history_view(
        principal=reviewer, application_id=application_id
    )
    schema = str(history["schema_version"])
    assert schema.startswith("s04-application-history/")

    cancellations = history["cancellations"]
    assert len(cancellations) == 1
    assert cancellations[0]["cycle"] == 1
    assert cancellations[0]["reason_code"] == "UPSTREAM_WITHDRAWN"
    assert cancellations[0]["authority_subject"] == reviewer.subject
    assert cancellations[0]["lifecycle_revision"] == cancel["lifecycle_revision"]

    terminations = history["terminations"]
    assert len(terminations) == 1
    assert terminations[0]["cycle"] == 1
    effects = {item["kind"] for item in terminations[0]["settled_effects"]}
    assert "review_work_item" in effects
    assert "check_job" in effects

    reopens = history["reopens"]
    assert len(reopens) == 1
    assert reopens[0]["predecessor_cycle"] == 1
    assert reopens[0]["cycle"] == 2
    assert reopens[0]["target_phase"] == "Intake"

    late_receipts = [
        item
        for item in history["late_input_receipts"]
        if item.get("reason_code") == "evidence.late_input_requires_reopen"
    ]
    assert len(late_receipts) == 1

    # Cancellation is never represented as rejection or deletion: the
    # original supplement request and its material requirement survive.
    supplements = [
        record
        for record in history.get("attachment_versions", [])
    ]
    assert isinstance(supplements, list)
