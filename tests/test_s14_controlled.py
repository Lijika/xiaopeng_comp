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

APPROVER = S01CommandPrincipal(
    subject="registered-approver-operator",
    role="operator",
    scope="C-DEMO",
    source_id="s14-approval-desk",
)


def make_service(
    *,
    downstream_registry: Any = None,
    clock: Any = None,
    recovery_verifier: Any = None,
    fault_injector: Any = None,
) -> ControlledScenarioService:
    return ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=RULES,
        state_path=Path(tempfile.mkdtemp(prefix="xiaopeng-s14-unit-"))
        / "target.sqlite3",
        downstream_registry=downstream_registry,
        clock=clock,
        recovery_verifier=recovery_verifier,
        fault_injector=fault_injector,
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
    return service, adapter


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


def test_cancelled_cycle_receives_no_new_send_and_settles_via_fence() -> None:
    service, adapter = _delivery_harness()
    application_id = _completed_with_obligation(service)
    sends_before = dict(adapter.executed_operations)
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
    assert cancel["fenced_effects"]["deliveries_fenced"] == 1

    # The sender refuses new business sends for the fenced cycle.
    fenced = service.process_next_delivery(principal=OPERATOR)
    assert fenced == {**fenced, "status": "fenced", "reason_code": "S14_DELIVERY_FENCED"}
    assert adapter.executed_operations == sends_before

    cancelled_fact = [
        event
        for event in service._store.audit_events
        if event.get("action") == "s14_delivery_cancelled"
        and event.get("application_id") == application_id
    ]
    assert len(cancelled_fact) == 1

    settled = _settle_to_terminated(
        service, application_id, cancel["lifecycle_revision"]
    )
    delivery_effects = [
        item
        for item in settled["settled_effects"]
        if item["kind"] == "delivery_obligation"
    ]
    assert delivery_effects == [
        {"kind": "delivery_obligation", "id": delivery_effects[0]["id"], "result": "cancelled_before_send"}
    ]

    audits = [
        event
        for event in service._store.audit_events
        if event.get("action") == "s14_cycle_terminated"
        and event.get("application_id") == application_id
    ]
    assert len(audits) == 1


def test_unknown_outcome_keeps_terminating_until_verified_terminal() -> None:
    service, _adapter = _delivery_harness(behavior="timeout_after_execute")
    application_id = _completed_with_obligation(service)
    # The send races BEFORE cancellation: the outcome is unknown.
    sent = service.process_next_delivery(principal=OPERATOR)
    assert sent["status"] == "unknown"
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
    kinds = {item["kind"] for item in outstanding["unresolved_effects"]}
    assert "termination_notification" in kinds

    # Time alone must never promote Terminating to Terminated.
    aged_clock = service._clock() + 10**9
    original_clock = service._clock
    try:
        service._clock = lambda: aged_clock  # type: ignore[method-assign]
        aged = service.settle_termination(
            application_id=application_id,
            principal=OPERATOR,
            expected_lifecycle_revision=cancel["lifecycle_revision"],
            idempotency_key="s14-settle-aged",
        )
    finally:
        service._clock = original_clock  # type: ignore[method-assign]
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

    settled = _settle_to_terminated(
        service, application_id, cancel["lifecycle_revision"]
    )
    effects = {item["kind"]: item["result"] for item in settled["settled_effects"]}
    assert effects["delivery_obligation"] == "received"

    delivery = service.delivery_view(
        principal=OPERATOR, application_id=application_id
    )
    assert delivery["routing_history"], "prior obligation must stay queryable"


def test_forward_compensation_settles_cancelled_cycle() -> None:
    service, _adapter = _delivery_harness(behavior="timeout_without_execute")
    application_id = _completed_with_obligation(service)
    sent = service.process_next_delivery(principal=OPERATOR)
    assert sent["status"] == "unknown"
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

    settled = _settle_to_terminated(
        service, application_id, cancel["lifecycle_revision"]
    )
    effects = {item["kind"]: item["result"] for item in settled["settled_effects"]}
    assert effects["delivery_obligation"] == "compensated"


def test_failed_compensation_stays_terminating() -> None:
    service, _adapter = _delivery_harness(
        behavior="timeout_after_execute", compensation_behavior="fail"
    )
    application_id = _completed_with_obligation(service)
    sent = service.process_next_delivery(principal=OPERATOR)
    assert sent["status"] == "unknown"
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


def _deliver_notification(service) -> dict:
    """Drive one pending notification to a verified terminal result using
    the external-effect protocol (send -> reconcile -> compensate)."""
    result: dict = {}
    for _ in range(4):
        result = service.process_termination_notification()
        if result["status"] == "delivered":
            return result
        if result["status"] == "unknown" and result.get("operation_id"):
            reconciled = service.reconcile_termination_notification(
                operation_id=result["operation_id"]
            )
            if reconciled["status"] == "delivered":
                return reconciled
    operation_id = str(result.get("operation_id") or "")
    compensated = service.compensate_termination_notification(
        operation_id=operation_id
    )
    assert compensated["status"] in {"compensated", "delivered"}, compensated
    return compensated


def _settle_to_terminated(service, application_id, revision) -> dict:
    """Full settlement: effects gate -> notification terminal result -> Terminated.

    The notification may settle as adapter-confirmed or forward-compensated;
    both are verified terminal results for the L14 gate.
    """
    first = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=revision,
        idempotency_key=f"s14-settle-arm-{application_id}",
    )
    assert first["status"] == "outstanding", first
    kinds = {item["kind"] for item in first["unresolved_effects"]}
    assert "termination_notification" in kinds
    delivered = _deliver_notification(service)
    assert delivered["status"] in {"delivered", "compensated"}, delivered
    final = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=revision,
        idempotency_key=f"s14-settle-seal-{application_id}",
    )
    assert final["status"] == "terminated", final
    return final


def _grant_exact_permission(
    service: ControlledScenarioService,
    application_id: str,
    *,
    permission_id: str = "institutional-reopen-permission/1",
    ttl_seconds: int = 3600,
    now: float | None = None,
    expected_lifecycle_revision: int | None = None,
    principal: Any = None,
    viewer: Any = None,
) -> dict:
    if expected_lifecycle_revision is None:
        expected_lifecycle_revision = int(
            service.current_route_view(
                principal=viewer or REVIEWER,
                application_id=application_id,
            )["lifecycle_revision"]
        )
    grant = service.grant_reopen_permission(
        application_id=application_id,
        principal=principal or OPERATOR,
        approver_subject=APPROVER.subject,
        permission_id=permission_id,
        expected_lifecycle_revision=expected_lifecycle_revision,
        idempotency_key=f"s14-grant-{permission_id}-{application_id}",
        ttl_seconds=ttl_seconds,
        now=now,
    )
    assert grant["status"] == "accepted", grant
    return grant


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
    settled = _settle_to_terminated(
        service, application_id, cancel["lifecycle_revision"]
    )
    return application_id, cancel, settled


def _reopen_policy(service: ControlledScenarioService) -> dict[str, Any]:
    permission = next(
        (
            item
            for item in reversed(service._store.review_records)
            if item.get("record_type") == "s14_reopen_permission"
        ),
        None,
    )
    release_digest = (permission or {}).get("policy_release_digest")
    if not str((permission or {}).get("policy_release_id") or "").startswith(
        "admission-bound:"
    ):
        release_digest = (permission or {}).get(
            "artifact_release_digest", _release_digest(service)
        )
    return {
        "permission_id": str(
            (permission or {}).get(
                "permission_id", "institutional-reopen-permission/1"
            )
        ),
        "release_digest": str(release_digest or _release_digest(service)),
    }


def test_reopen_requires_distinct_authority_and_policy_permission() -> None:
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

    # With an exact grant in place a wrong release digest is a policy
    # rejection, not an unknown permission.
    _grant_exact_permission(
        fresh, application_id, permission_id="institutional-reopen-permission/1", now=900
    )
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
        now=1000,
    )
    assert bad_policy["status"] == "rejected"
    assert bad_policy["reason_code"] == "lifecycle.reopen_policy_forbidden"

    stale = fresh.reopen_application(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"] - 1,
        idempotency_key="s14-reopen-stale",
        target_phase="Intake",
        reopen_policy={
            "permission_id": "institutional-reopen-permission/1",
            "release_digest": _reopen_policy(fresh)["release_digest"],
        },
        now=1000,
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
        _grant_exact_permission(service, application_id)

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
    _grant_exact_permission(service, application_id)
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
    _settle_to_terminated(
        service, application_id, cancel["lifecycle_revision"]
    )

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
    settled = _settle_to_terminated(
        service, application_id, cancel["lifecycle_revision"]
    )
    submission = _attachment_submission(request, source, closed=False)
    late = service.submit_attachment_version(
        submission=submission,
        idempotency_key="s14-late-history",
        principal=SUPPLEMENT_INTEGRATOR,
        now=600,
    )
    assert late.reason_code == "evidence.late_input_requires_reopen"
    _grant_exact_permission(
        service,
        application_id,
        viewer=reviewer,
    )
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


# --------------------------------------------------------------------------
# R1 fixes: notification gate, unresolved outcomes, governed permission,
# live principals, policy-impact race
# --------------------------------------------------------------------------


def test_terminated_is_impossible_while_notification_is_pending() -> None:
    service = make_service()
    application_id, _work_item_id, _review, route = _manual_review_state(service)
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-notif-gate",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted"

    armed = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-settle-arm-notif",
    )
    assert armed["status"] == "outstanding"
    assert armed["phase"] == "Terminating"
    notifications = [
        event
        for event in service._store.outbox
        if event.get("kind") == "termination_notification_requested"
    ]
    assert len(notifications) == 1 and notifications[0]["status"] == "pending"

    # No lifecycle transition happened: still Terminating at the same rev.
    current = service.current_route_view(
        principal=REVIEWER, application_id=application_id
    )
    assert current["phase"] == "Terminating"
    assert current["lifecycle_revision"] == cancel["lifecycle_revision"]

    # Aged time alone cannot deliver or seal.
    aged_clock = service._clock() + 10**9
    original_clock = service._clock
    try:
        service._clock = lambda: aged_clock  # type: ignore[method-assign]
        aged = service.settle_termination(
            application_id=application_id,
            principal=OPERATOR,
            expected_lifecycle_revision=cancel["lifecycle_revision"],
            idempotency_key="s14-settle-aged-notif",
        )
    finally:
        service._clock = original_clock  # type: ignore[method-assign]
    assert aged["status"] == "outstanding"

    first_delivery = _deliver_notification(service)
    assert first_delivery["status"] == "delivered"
    duplicate = service.process_termination_notification()
    assert duplicate["status"] == "idle"
    confirmations = [
        event
        for event in service._store.audit_events
        if event.get("action") == "s14_notification_confirmed"
    ]
    assert len(confirmations) == 1
    attempts = [
        item
        for item in service._store.attempts
        if str(item.get("job_id") or "").startswith("termination_notification:")
    ]
    assert attempts, "durable attempt rows must exist"
    assert any(
        item.get("outcome") == "claimed" for item in attempts
    )

    settled = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-settle-seal-notif",
    )
    assert settled["status"] == "terminated"




def test_reopen_permission_matrix_rejects_every_non_exact_grant() -> None:
    service = make_service()
    application_id, _cancel, settled = _terminate_manual_review(service)

    # Arbitrary / unapproved permission identifiers never resolve.
    unknown = service.reopen_application(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=settled["lifecycle_revision"],
        idempotency_key="s14-reopen-unknown-perm",
        target_phase="Intake",
        reopen_policy={
            "permission_id": "made-up-permission",
            "release_digest": _release_digest(service),
        },
    )
    assert unknown["status"] == "rejected"
    assert unknown["reason_code"] == "lifecycle.reopen_permission_unknown"

    # Permission cannot be granted while Terminating.
    terminating_service = make_service()
    app2, _, _, route2 = _manual_review_state(terminating_service)
    cancelling = terminating_service.cancel_application(
        application_id=app2,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route2["lifecycle_revision"]),
        idempotency_key="s14-cancel-perm-early",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    early = terminating_service.grant_reopen_permission(
        application_id=app2,
        principal=SECOND_OPERATOR,
        approver_subject=APPROVER.subject,
        permission_id="perm-terminating",
        expected_lifecycle_revision=1,
        idempotency_key="s14-grant-early",
    )
    assert early["status"] == "rejected"
    assert early["reason_code"] == "lifecycle.permission_requires_terminated"

    # Approver must be distinct from the canceller.
    colluding = terminating_service.settle_termination  # noqa: F841
    fresh = make_service()
    app3, cancel3, settled3 = _terminate_manual_review(fresh)
    as_canceller = fresh.grant_reopen_permission(
        application_id=app3,
        principal=SECOND_OPERATOR,
        approver_subject=INTEGRATOR.subject,
        permission_id="perm-canceller",
        expected_lifecycle_revision=settled3["lifecycle_revision"],
        idempotency_key="s14-grant-canceller",
    )
    assert as_canceller["status"] == "rejected"
    assert as_canceller["reason_code"] == "lifecycle.reopen_authority_not_distinct"

    # Self-approval is rejected.
    self_approved = fresh.grant_reopen_permission(
        application_id=app3,
        principal=SECOND_OPERATOR,
        approver_subject=SECOND_OPERATOR.subject,
        permission_id="perm-self",
        expected_lifecycle_revision=settled3["lifecycle_revision"],
        idempotency_key="s14-grant-self",
    )
    assert self_approved["status"] == "rejected"
    assert self_approved["reason_code"] == "lifecycle.reopen_authority_not_distinct"

    granted = fresh.grant_reopen_permission(
        application_id=app3,
        principal=OPERATOR,
        approver_subject=APPROVER.subject,
        permission_id="institutional-reopen-permission/1",
        expected_lifecycle_revision=settled3["lifecycle_revision"],
        idempotency_key="s14-grant-ok",
        ttl_seconds=3600,
        now=1000,
    )
    assert granted["status"] == "accepted", granted

    duplicate_grant = fresh.grant_reopen_permission(
        application_id=app3,
        principal=OPERATOR,
        approver_subject=APPROVER.subject,
        permission_id="institutional-reopen-permission/1",
        expected_lifecycle_revision=settled3["lifecycle_revision"],
        idempotency_key="s14-grant-ok-2",
        now=1001,
    )
    assert duplicate_grant["status"] == "rejected"
    assert duplicate_grant["reason_code"] == "lifecycle.reopen_permission_exists"

    # Wrong release digest in the reopen request is rejected.
    wrong_release = fresh.reopen_application(
        application_id=app3,
        principal=OPERATOR,
        expected_lifecycle_revision=settled3["lifecycle_revision"],
        idempotency_key="s14-reopen-wrong-release",
        target_phase="Intake",
        reopen_policy={
            "permission_id": "institutional-reopen-permission/1",
            "release_digest": "f" * 64,
        },
        now=1100,
    )
    assert wrong_release["status"] == "rejected"
    assert wrong_release["reason_code"] == "lifecycle.reopen_policy_forbidden"

    # Expired permission is rejected.
    expired_grant = make_service()
    app4, _c4, settled4 = _terminate_manual_review(expired_grant)
    short = expired_grant.grant_reopen_permission(
        application_id=app4,
        principal=OPERATOR,
        approver_subject=APPROVER.subject,
        permission_id="short-lived-permission",
        expected_lifecycle_revision=settled4["lifecycle_revision"],
        idempotency_key="s14-grant-short",
        ttl_seconds=10,
        now=5000,
    )
    assert short["status"] == "accepted"
    expired = expired_grant.reopen_application(
        application_id=app4,
        principal=OPERATOR,
        expected_lifecycle_revision=settled4["lifecycle_revision"],
        idempotency_key="s14-reopen-expired-perm",
        target_phase="Intake",
        reopen_policy={
            "permission_id": "short-lived-permission",
            "release_digest": _release_digest(expired_grant),
        },
        now=6000,
    )
    assert expired["status"] == "rejected"
    assert expired["reason_code"] == "lifecycle.reopen_permission_expired"

    # The approved exact permission opens exactly one successor cycle.
    reopened = fresh.reopen_application(
        application_id=app3,
        principal=OPERATOR,
        expected_lifecycle_revision=settled3["lifecycle_revision"],
        idempotency_key="s14-reopen-exact-perm",
        target_phase="Intake",
        reopen_policy={
            "permission_id": "institutional-reopen-permission/1",
            "release_digest": _release_digest(fresh),
        },
        now=1200,
    )
    assert reopened["status"] == "accepted"
    assert reopened["cycle"] == 2


def test_expired_and_source_mismatched_principals_are_rejected() -> None:
    service = make_service()
    application_id, _work_item_id, _review, route = _manual_review_state(service)
    revision = int(route["lifecycle_revision"])

    expired_integrator = S01CommandPrincipal(
        subject=INTEGRATOR.subject,
        role="integrator",
        scope="C-DEMO",
        source_id=INTEGRATOR.source_id,
        expires_at=1.0,
    )
    expired_cancel = service.cancel_application(
        application_id=application_id,
        principal=expired_integrator,
        expected_lifecycle_revision=revision,
        idempotency_key="s14-cancel-expired-principal",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert expired_cancel["status"] == "rejected"
    assert expired_cancel["reason_code"] == "lifecycle.cancel_forbidden"

    rogue_source = S01CommandPrincipal(
        subject=INTEGRATOR.subject,
        role="integrator",
        scope="C-DEMO",
        source_id="rogue-unregistered-source",
    )
    mismatched = service.cancel_application(
        application_id=application_id,
        principal=rogue_source,
        expected_lifecycle_revision=revision,
        idempotency_key="s14-cancel-rogue-source",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert mismatched["status"] == "rejected"
    assert mismatched["reason_code"] == "lifecycle.cancel_forbidden"

    accepted_cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=revision,
        idempotency_key="s14-cancel-live-principal",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert accepted_cancel["status"] == "accepted"

    expired_operator = S01CommandPrincipal(
        subject=OPERATOR.subject,
        role="operator",
        scope="C-DEMO",
        source_id=OPERATOR.source_id,
        expires_at=1.0,
    )
    expired_settle = service.settle_termination(
        application_id=application_id,
        principal=expired_operator,
        expected_lifecycle_revision=accepted_cancel["lifecycle_revision"],
        idempotency_key="s14-settle-expired-operator",
    )
    assert expired_settle["status"] == "rejected"
    assert expired_settle["reason_code"] == "FORBIDDEN"

    live_settle = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=accepted_cancel["lifecycle_revision"],
        idempotency_key="s14-settle-live-operator",
    )
    assert live_settle["status"] == "outstanding"


class _StubGovernance:
    """Minimal final-impact manifest loader for consumer-race tests."""

    def __init__(self, manifest: dict[str, object]) -> None:
        self._manifest = manifest

    def load_final_impact(self, digest: str, store: object = None):
        return self._manifest

    def has_governed_activation(self, scope: str) -> bool:
        return True


def test_policy_impact_race_records_stale_receipt_without_transition() -> None:
    from task4_consistency.controlled.s01 import ControlledScenarioTestDriver

    digest = "b" * 64
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=RULES,
        state_path=Path(tempfile.mkdtemp(prefix="xiaopeng-s14-impact-"))
        / "target.sqlite3",
    )

    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s14-impact-intake",
        principal=INTEGRATOR,
    )
    assert admitted.application_id is not None
    application_id = str(admitted.application_id)
    driver = ControlledScenarioTestDriver(service)
    assert driver.process_next_job().status == "complete"
    service.refresh_projection()
    # Attach the stub manifest loader only for consumption: the check run
    # above used the normal pinned release path without any governance.
    service._policy_governance = _StubGovernance(
        {
            "members": [
                {
                    "application_id": application_id,
                    "cycle": 1,
                    "partition": "open_cycle",
                    "target_generation": 2,
                    "hit_reasons": ["rules_change"],
                    "required_disposition": "operational_reevaluation",
                }
            ]
        }
    )

    # Stage the impact activation BEFORE cancellation (snapshot captured).
    driver.stage_impact_activation(final_impact_digest=digest)

    route = service.current_route_view(
        principal=REVIEWER, application_id=application_id
    )
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-impact-race",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted"
    phases_before = list(service._store.applications[application_id]["phase_history"])

    consumed = service.process_next_policy_impact()
    assert consumed == 1

    app_state = service._store.applications[application_id]
    assert app_state["phase"] == "Terminating"
    assert app_state["phase_history"] == phases_before

    receipts = [
        message
        for message in service._store.inbox
        if message.get("kind") == "s09_impact_disposition"
        and message.get("application_id") == application_id
    ]
    assert len(receipts) == 1
    assert receipts[0]["disposition"] == "stale_terminated_cycle"


def test_cancel_preserves_unresolved_check_outcomes() -> None:
    service = make_service()
    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s14-intake-unresolved",
        principal=INTEGRATOR,
    )
    assert admitted.application_id is not None
    application_id = str(admitted.application_id)
    driver = ControlledScenarioTestDriver(service)
    first = driver.process_next_job(operation_fault="checker_outcome_unknown")
    assert first.status == "blocked", (first.status, first.reason_code)
    job = next(
        (
            item
            for item in service._store.jobs
            if item.get("application_id") == application_id
        ),
        None,
    )
    assert job is not None and job.get("status") == "outcome_unknown", job

    route = service.current_route_view(
        principal=REVIEWER, application_id=application_id
    )
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-unresolved",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted"

    job_after = next(
        item
        for item in service._store.jobs
        if item.get("application_id") == application_id
    )
    assert job_after["status"] == "outcome_unknown"

    outstanding = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-settle-unresolved",
    )
    assert outstanding["status"] == "outstanding"
    details = {
        item["kind"]: item["detail"]
        for item in outstanding["unresolved_effects"]
    }
    assert details.get("check_job") == "outcome_unknown"


# --------------------------------------------------------------------------
# R2 fixes: notification protocol availability/duplicate, delivery boundary
# races, recovery visibility, cycle scoping, permission CAS/source
# --------------------------------------------------------------------------


def test_notification_worker_fails_closed_when_audit_or_storage_unavailable() -> None:
    service = make_service()
    application_id, _work_item_id, _review, route = _manual_review_state(service)
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-notif-outage",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-arm-notif-outage",
    )

    service.audit_available = False
    outage = service.process_termination_notification()
    assert outage == {
        "status": "unavailable",
        "reason_code": "AUDIT_UNAVAILABLE",
    }
    service.audit_available = True
    service.storage_available = False
    outage_storage = service.process_termination_notification()
    assert outage_storage == {
        "status": "unavailable",
        "reason_code": "STORAGE_UNAVAILABLE",
    }
    service.storage_available = True

    # The pending row remains replayable after the outages.
    recovered = _deliver_notification(service)
    assert recovered["status"] in {"delivered", "compensated"}


def test_notification_live_claim_is_unique_for_same_worker() -> None:
    service = make_service(clock=lambda: 100)
    application_id, _work_item_id, _review, route = _manual_review_state(service)
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-unique-claim",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    armed = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-arm-unique-claim",
    )
    assert armed["status"] == "outstanding"
    operation_id = next(
        item["operation_id"]
        for item in service._store.outbox
        if item.get("kind") == "termination_notification_requested"
    )
    service._store.attempts.append(
        {
            "attempt_id": "s14-live-claim",
            "job_id": f"termination_notification:{operation_id}",
            "application_id": application_id,
            "worker_id": "s14-notification-worker",
            "fence": 1,
            "attempt_no": 1,
            "started_at": 100,
            "status": "started",
            "outcome": "claimed",
            "lease_until": 130,
            "claim_cycle": 1,
            "claim_lifecycle_revision": int(cancel["lifecycle_revision"]),
            "operation_id": operation_id,
        }
    )
    service._store.persist()

    result = service.process_termination_notification(worker_id="s14-notification-worker")

    assert result["status"] == "claimed"
    assert result["reason_code"] == "S14_NOTIFICATION_LEASE_ACTIVE"
    assert service._store.outbox[0]["status"] == "pending"


def test_notification_dependency_loss_after_send_keeps_obligation_pending() -> None:
    from task4_consistency.controlled.s13 import (
        DownstreamRecipientRegistration,
        InMemoryDownstreamAdapter,
        RegisteredDownstreamRegistry,
    )

    class AvailabilityFlipAdapter:
        adapter_id = "s14-availability-flip"
        adapter_version = "1"

        def __init__(self, service):
            self.service = service
            self.inner = InMemoryDownstreamAdapter(
                adapter_id=self.adapter_id, adapter_version=self.adapter_version
            )

        def send(self, request):
            result = self.inner.send(request)
            self.service.storage_available = False
            return result

        def lookup(self, *, operation_id, recipient_id):
            return self.inner.lookup(operation_id=operation_id, recipient_id=recipient_id)

        def compensate(self, request):
            return self.inner.compensate(request)

    holder: dict[str, Any] = {}
    adapter = AvailabilityFlipAdapter(None)
    registry = RegisteredDownstreamRegistry(
        [
            DownstreamRecipientRegistration(
                scope="C-DEMO",
                recipient_registration_id="c-demo-downstream-review-default",
                recipient_id="downstream-review-desk",
                adapter_id=adapter.adapter_id,
                adapter_version=adapter.adapter_version,
            )
        ],
        {adapter.adapter_id: adapter},
    )
    service = make_service(downstream_registry=registry)
    adapter.service = service
    application_id, _work_item_id, _review, route = _manual_review_state(service)
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-send-outage",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    armed = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-arm-send-outage",
    )
    assert armed["status"] == "outstanding"

    result = service.process_termination_notification()

    assert result["status"] == "unavailable"
    assert result["reason_code"] == "STORAGE_UNAVAILABLE"
    assert next(
        item for item in service._store.outbox
        if item.get("kind") == "termination_notification_requested"
    )["status"] == "pending"
    assert not any(
        event.get("action") == "s14_notification_confirmed"
        for event in service._store.audit_events
    )


def test_notification_lease_expiry_after_send_records_unknown() -> None:
    from task4_consistency.controlled.s13 import (
        DownstreamRecipientRegistration,
        InMemoryDownstreamAdapter,
        RegisteredDownstreamRegistry,
    )

    clock = [100]

    class LeaseExpiryAdapter(InMemoryDownstreamAdapter):
        def send(self, request):
            result = super().send(request)
            clock[0] = 131
            return result

    adapter = LeaseExpiryAdapter(
        adapter_id="s14-lease-expiry", adapter_version="1"
    )
    registry = RegisteredDownstreamRegistry(
        [
            DownstreamRecipientRegistration(
                scope="C-DEMO",
                recipient_registration_id="c-demo-downstream-review-default",
                recipient_id="downstream-review-desk",
                adapter_id=adapter.adapter_id,
                adapter_version=adapter.adapter_version,
            )
        ],
        {adapter.adapter_id: adapter},
    )
    service = make_service(downstream_registry=registry, clock=lambda: clock[0])
    application_id, _work_item_id, _review, route = _manual_review_state(service)
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-lease-expiry",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-arm-lease-expiry",
    )

    result = service.process_termination_notification()

    assert result["status"] == "unknown"
    assert result["reason_code"] == "S13_STALE_DELIVERY_FENCE"
    assert next(
        item
        for item in service._store.outbox
        if item.get("kind") == "termination_notification_requested"
    )["status"] == "pending"
    assert any(
        event.get("action") == "s14_notification_unknown"
        and event.get("reason_code") == "S13_STALE_DELIVERY_FENCE"
        for event in service._store.audit_events
    )


def test_notification_new_fence_wins_after_lease_expiry() -> None:
    import threading

    from task4_consistency.controlled.s13 import (
        DownstreamRecipientRegistration,
        InMemoryDownstreamAdapter,
        RegisteredDownstreamRegistry,
    )

    clock = [100]
    first_send_entered = threading.Event()
    release_first_send = threading.Event()

    class LeaseRenewalAdapter(InMemoryDownstreamAdapter):
        send_count = 0

        def send(self, request):
            type(self).send_count += 1
            if type(self).send_count == 1:
                first_send_entered.set()
                assert release_first_send.wait(timeout=5)
            return super().send(request)

    adapter = LeaseRenewalAdapter(
        adapter_id="s14-lease-renewal", adapter_version="1"
    )
    registry = RegisteredDownstreamRegistry(
        [
            DownstreamRecipientRegistration(
                scope="C-DEMO",
                recipient_registration_id="c-demo-downstream-review-default",
                recipient_id="downstream-review-desk",
                adapter_id=adapter.adapter_id,
                adapter_version=adapter.adapter_version,
            )
        ],
        {adapter.adapter_id: adapter},
    )
    service = make_service(downstream_registry=registry, clock=lambda: clock[0])
    application_id, _work_item_id, _review, route = _manual_review_state(service)
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-lease-renewal",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    armed = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-arm-lease-renewal",
    )
    operation_id = str(armed["unresolved_effects"][-1]["id"])
    old_result: list[dict[str, Any]] = []

    def old_sender() -> None:
        old_result.append(service.process_termination_notification(worker_id="old"))

    sender = threading.Thread(target=old_sender)
    sender.start()
    assert first_send_entered.wait(timeout=5)
    clock[0] = 131

    renewed = service.process_termination_notification(worker_id="new")
    assert renewed["status"] == "delivered"
    assert renewed["operation_id"] == operation_id

    release_first_send.set()
    sender.join(timeout=5)
    assert old_result[0]["status"] in {"stale", "unknown"}
    assert old_result[0].get("reason_code") in {
        "S13_STALE_DELIVERY_FENCE",
        "S14_CANCEL_FENCED",
    }
    assert adapter.send_count == 2
    assert sum(
        1
        for event in service._store.audit_events
        if event.get("action") == "s14_notification_confirmed"
        and event.get("operation_id") == operation_id
    ) == 1


@pytest.mark.parametrize("effect", ["lookup", "compensate"])
def test_notification_follow_up_dependency_loss_keeps_obligation_pending(
    effect: str,
) -> None:
    from task4_consistency.controlled.s13 import (
        DownstreamRecipientRegistration,
        InMemoryDownstreamAdapter,
        RegisteredDownstreamRegistry,
    )

    class DependencyFlipAdapter(InMemoryDownstreamAdapter):
        service: ControlledScenarioService

        def lookup(self, *, operation_id, recipient_id):
            result = super().lookup(
                operation_id=operation_id, recipient_id=recipient_id
            )
            if effect == "lookup":
                self.service.storage_available = False
            return result

        def compensate(self, request):
            result = super().compensate(request)
            if effect == "compensate":
                self.service.audit_available = False
            return result

    adapter = DependencyFlipAdapter(
        adapter_id=f"s14-{effect}-outage",
        adapter_version="1",
        behavior=(
            "timeout_after_execute"
            if effect == "lookup"
            else "timeout_without_execute"
        ),
    )
    registry = RegisteredDownstreamRegistry(
        [
            DownstreamRecipientRegistration(
                scope="C-DEMO",
                recipient_registration_id="c-demo-downstream-review-default",
                recipient_id="downstream-review-desk",
                adapter_id=adapter.adapter_id,
                adapter_version=adapter.adapter_version,
            )
        ],
        {adapter.adapter_id: adapter},
    )
    service = make_service(downstream_registry=registry)
    adapter.service = service
    application_id, _work_item_id, _review, route = _manual_review_state(service)
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key=f"s14-cancel-{effect}-outage",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key=f"s14-arm-{effect}-outage",
    )
    unknown = service.process_termination_notification()
    assert unknown["status"] == "unknown"

    if effect == "lookup":
        result = service.reconcile_termination_notification(
            operation_id=unknown["operation_id"]
        )
        expected_reason = "STORAGE_UNAVAILABLE"
    else:
        result = service.compensate_termination_notification(
            operation_id=unknown["operation_id"]
        )
        expected_reason = "AUDIT_UNAVAILABLE"

    assert result["status"] == "unavailable"
    assert result["reason_code"] == expected_reason
    assert next(
        item
        for item in service._store.outbox
        if item.get("operation_id") == unknown["operation_id"]
    )["status"] == "pending"
    assert service._s14_notification_terminal_result(unknown["operation_id"]) is None


def test_s14_store_write_failures_leave_staged_facts_unpublished() -> None:
    def fail_at(point: str):
        def fail(write_point: str) -> None:
            if write_point == point:
                raise OSError(point)

        return fail

    cancel_service = make_service()
    application_id, _item, _review, route = _manual_review_state(cancel_service)
    phase_before = cancel_service._store.applications[application_id]["phase"]
    cancel_service._fault_injector = fail_at("s14.cancel.audit")
    failed_cancel = cancel_service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-failed-cancel",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert failed_cancel["status"] == "unavailable"
    assert failed_cancel["reason_code"] == "AUDIT_UNAVAILABLE"
    assert cancel_service._store.applications[application_id]["phase"] == phase_before

    settle_service = make_service()
    application_id, _item, _review, route = _manual_review_state(settle_service)
    cancel = settle_service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-before-failed-settle",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    settle_service._fault_injector = fail_at("s14.settle.outbox")
    failed_settle = settle_service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-failed-settle",
    )
    assert failed_settle["status"] == "unavailable"
    assert failed_settle["reason_code"] == "STORAGE_UNAVAILABLE"
    assert not any(
        item.get("kind") == "termination_notification_requested"
        for item in settle_service._store.outbox
    )

    reopen_service = make_service()
    application_id, _cancel, settled = _terminate_manual_review(reopen_service)
    reopen_service._fault_injector = fail_at("s14.grant.record")
    failed_grant = reopen_service.grant_reopen_permission(
        application_id=application_id,
        principal=OPERATOR,
        approver_subject=APPROVER.subject,
        permission_id="s14-failed-grant",
        expected_lifecycle_revision=settled["lifecycle_revision"],
        idempotency_key="s14-failed-grant",
    )
    assert failed_grant["status"] == "unavailable"
    assert not any(
        item.get("permission_id") == "s14-failed-grant"
        for item in reopen_service._store.review_records
    )
    reopen_service._fault_injector = None
    _grant_exact_permission(
        reopen_service,
        application_id,
        expected_lifecycle_revision=settled["lifecycle_revision"],
    )
    reopen_service._fault_injector = fail_at("s14.reopen.audit")
    failed_reopen = reopen_service.reopen_application(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=settled["lifecycle_revision"],
        idempotency_key="s14-failed-reopen",
        target_phase="Intake",
        reopen_policy=_reopen_policy(reopen_service),
    )
    assert failed_reopen["status"] == "unavailable"
    assert failed_reopen["reason_code"] == "AUDIT_UNAVAILABLE"
    assert reopen_service._store.applications[application_id]["cycle"] == 1
    assert reopen_service._store.applications[application_id]["phase"] == "Terminated"


def test_pre_run_cancel_can_reopen_with_admission_bound_release() -> None:
    service = make_service()
    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s14-pre-run-admission",
        principal=INTEGRATOR,
    )
    application_id = str(admitted.application_id)
    route = service.current_route_view(principal=REVIEWER, application_id=application_id)
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-pre-run-cancel",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted"
    settled = _settle_to_terminated(service, application_id, cancel["lifecycle_revision"])
    grant = _grant_exact_permission(
        service, application_id, expected_lifecycle_revision=settled["lifecycle_revision"]
    )
    assert grant["policy_release_digest"] != _release_digest(service)
    assert str(grant["policy_release_id"]).startswith("admission-bound:")

    reopened = service.reopen_application(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=settled["lifecycle_revision"],
        idempotency_key="s14-pre-run-reopen",
        target_phase="Intake",
        reopen_policy=_reopen_policy(service),
    )

    assert reopened["status"] == "accepted", reopened
    assert reopened["cycle"] == 2


class _BlockingAdapter:
    """Wraps the in-memory adapter and blocks inside ``send`` so a cancel
    can race the claim-to-send window deterministically."""

    def __init__(self, inner):
        self.inner = inner
        import threading

        self.entered_send = threading.Event()
        self.release_send = threading.Event()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def send(self, request):
        self.entered_send.set()
        if not self.release_send.wait(timeout=5):
            raise TimeoutError("blocked adapter was not released")
        return self.inner.send(request)


def _blocking_harness():
    from task4_consistency.controlled.s13 import (
        DownstreamRecipientRegistration,
        InMemoryDownstreamAdapter,
        RegisteredDownstreamRegistry,
    )

    inner = InMemoryDownstreamAdapter()
    blocking = _BlockingAdapter(inner)
    reg = DownstreamRecipientRegistration(
        scope="C-DEMO",
        recipient_registration_id="c-demo-downstream-review-default",
        recipient_id="downstream-review-desk",
        adapter_id=inner.adapter_id,
        adapter_version=inner.adapter_version,
    )
    registry = RegisteredDownstreamRegistry([reg], {inner.adapter_id: blocking})
    service = make_service(downstream_registry=registry)
    _auto_complete(service)
    return service, blocking, inner


def _completed_auto(service) -> str:
    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=f"s14-intake-auto-{id(service)}",
        principal=INTEGRATOR,
    )
    assert service.process_next_job().status == "complete"
    service.refresh_projection()
    return str(admitted.application_id)


def test_claim_cancel_race_does_not_call_adapter_after_fence() -> None:
    import threading

    service, blocking, inner = _blocking_harness()
    application_id = _completed_auto(service)

    results: list[dict] = []

    def run_sender() -> None:
        results.append(service.process_next_delivery(principal=OPERATOR))

    sender = threading.Thread(target=run_sender)
    sender.start()
    assert blocking.entered_send.wait(timeout=5), "adapter was not reached"

    route = service.current_route_view(
        principal=REVIEWER, application_id=application_id
    )
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-boundary-race",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted"
    blocking.release_send.set()
    sender.join(timeout=5)

    result = results[0]
    # The cancellation raced an already-started operation: the late result
    # is retained as unknown/stale and routed to reconciliation, never as a
    # fresh business receipt for a cancelled cycle.
    assert result["status"] in {"stale", "unknown"}
    assert result.get("reason_code") in {"S14_CANCEL_FENCED", None}

    obligation_id = service.delivery_view(
        principal=OPERATOR, application_id=application_id
    )["obligation"]["obligation_id"]
    reconciliations = [
        item
        for item in service._store.delivery_reconciliations
        if item.get("obligation_id") == obligation_id
        and item.get("reason_code") == "S14_CANCEL_FENCED"
    ]
    assert reconciliations, "fenced outcome must be recorded"


def test_delivery_outcome_requires_current_cycle_and_lifecycle_revision() -> None:
    """The claim-to-cancel race above is the observable form of this rule:
    the outcome transaction compares the attempt's claimed cycle and
    lifecycle revision against the current authority and refuses received."""
    service, blocking, _inner = _blocking_harness()
    application_id = _completed_auto(service)

    import threading

    results: list[dict] = []

    def run_sender() -> None:
        results.append(service.process_next_delivery(principal=OPERATOR))

    sender = threading.Thread(target=run_sender)
    sender.start()
    assert blocking.entered_send.wait(timeout=5)
    route = service.current_route_view(
        principal=REVIEWER, application_id=application_id
    )
    service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-revision-drift",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    blocking.release_send.set()
    sender.join(timeout=5)

    delivery = service.delivery_view(
        principal=OPERATOR, application_id=application_id
    )
    assert delivery["delivery_status"] != "received"
    assert any(
        item.get("reason_code") == "S14_CANCEL_FENCED"
        for item in service._store.delivery_reconciliations
    )


def test_delivery_adapter_requests_carry_lifecycle_fence() -> None:
    from task4_consistency.controlled.s13 import InMemoryDownstreamAdapter

    service, adapter = _delivery_harness(behavior="timeout_after_execute")
    application_id = _completed_with_obligation(service)
    sent: list[Any] = []
    compensated: list[Any] = []
    original_send = adapter.send
    original_compensate = adapter.compensate

    def capture_send(request: Any) -> Any:
        sent.append(request)
        return original_send(request)

    def capture_compensation(request: Any) -> Any:
        compensated.append(request)
        return original_compensate(request)

    adapter.send = capture_send  # type: ignore[method-assign]
    adapter.compensate = capture_compensation  # type: ignore[method-assign]
    result = service.process_next_delivery(principal=OPERATOR)
    assert result["status"] == "unknown"
    obligation = service.delivery_view(
        principal=OPERATOR, application_id=application_id
    )["obligation"]
    compensation = service.compensate_delivery(
        obligation_id=obligation["obligation_id"], principal=OPERATOR
    )
    assert compensation["status"] == "compensated"

    assert (
        sent[0].application_id,
        sent[0].cycle,
        sent[0].lifecycle_revision,
        sent[0].fence,
    ) == (
        application_id,
        obligation["cycle"],
        obligation["completion_lifecycle_revision"],
        1,
    )
    assert (
        compensated[0].application_id,
        compensated[0].cycle,
        compensated[0].lifecycle_revision,
        compensated[0].fence,
    ) == (
        application_id,
        obligation["cycle"],
        obligation["completion_lifecycle_revision"],
        1,
    )


def test_notification_reconcile_claim_fences_blocked_sender() -> None:
    import threading

    from task4_consistency.controlled.s13 import (
        DownstreamRecipientRegistration,
        InMemoryDownstreamAdapter,
        RegisteredDownstreamRegistry,
    )

    inner = InMemoryDownstreamAdapter(
        adapter_id="s14-reconcile-fence", behavior="timeout_without_execute"
    )
    blocking = _BlockingAdapter(inner)
    registry = RegisteredDownstreamRegistry(
        [
            DownstreamRecipientRegistration(
                scope="C-DEMO",
                recipient_registration_id="c-demo-downstream-review-default",
                recipient_id="downstream-review-desk",
                adapter_id=inner.adapter_id,
                adapter_version=inner.adapter_version,
            )
        ],
        {inner.adapter_id: blocking},
    )
    service = make_service(downstream_registry=registry)
    application_id, _item, _review, route = _manual_review_state(service)
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-reconcile-fence-cancel",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    armed = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=int(cancel["lifecycle_revision"]),
        idempotency_key="s14-reconcile-fence-arm",
    )
    operation_id = str(armed["unresolved_effects"][-1]["id"])
    results: list[dict[str, Any]] = []

    def send() -> None:
        results.append(service.process_termination_notification())

    sender = threading.Thread(target=send)
    sender.start()
    assert blocking.entered_send.wait(timeout=5)
    reconciled = service.reconcile_termination_notification(operation_id=operation_id)
    assert reconciled["status"] == "claimed"
    assert reconciled["reason_code"] == "S14_NOTIFICATION_LEASE_ACTIVE"
    blocking.release_send.set()
    sender.join(timeout=5)
    assert results[0]["status"] in {"stale", "unknown"}
    assert next(
        item
        for item in service._store.outbox
        if item.get("operation_id") == operation_id
    )["status"] == "pending"
    assert not any(
        event.get("action") == "s14_notification_confirmed"
        and event.get("operation_id") == operation_id
        for event in service._store.audit_events
    )


def test_cancelled_unknown_outcome_can_reconcile_then_settle() -> None:
    def verifier(work: dict[str, object]) -> dict[str, object]:
        criterion = work["criterion"]
        assert isinstance(criterion, dict)
        return {
            "verification_id": "s14-cancelled-unknown-verification",
            "observed_at": int(work["opened_at"]) + 1,  # type: ignore[arg-type]
            "evidence_kind": criterion["evidence_kind"],
            "scope": work["visibility_scope"],
            "recovery_work_id": work["recovery_work_id"],
            "criterion_digest": criterion["digest"],
            "conditions": [
                {
                    "condition_id": condition["condition_id"],
                    "verified": True,
                    "evidence_digest": "e" * 64,
                }
                for condition in criterion["conditions"]  # type: ignore[index]
            ],
        }

    service = make_service(recovery_verifier=verifier)
    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=f"s14-intake-unknown-{id(service)}",
        principal=INTEGRATOR,
    )
    application_id = str(admitted.application_id)
    driver = ControlledScenarioTestDriver(service)
    blocked = driver.process_next_job(operation_fault="checker_timeout")
    assert blocked.status == "blocked", blocked.status
    assert blocked.recovery_work_id

    view = service.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=str(blocked.recovery_work_id),
    )
    route = service.current_route_view(
        principal=REVIEWER, application_id=application_id
    )
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-unknown-reconcile",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted"

    verified = service.verify_recovery(
        principal=OPERATOR,
        recovery_work_id=str(blocked.recovery_work_id),
        expected_lifecycle_revision=int(view["lifecycle_revision"]),
        expected_criterion_digest=str(view["criterion"]["digest"]),
        idempotency_key="s14-recover-cancelled-unknown",
    )
    assert verified["status"] == "accepted", verified
    assert verified.get("recovery_mode") == "cancelled_cycle"

    current = service.current_route_view(
        principal=REVIEWER, application_id=application_id
    )
    assert current["phase"] == "Terminating"

    settled = _settle_to_terminated(
        service, application_id, cancel["lifecycle_revision"]
    )
    kinds = {item["kind"]: item["result"] for item in settled["settled_effects"]}
    assert kinds.get("recovery_work") == "closed"


def test_cancelled_compensation_failure_stays_outstanding() -> None:
    service = make_service()
    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=f"s14-intake-compfail-{id(service)}",
        principal=INTEGRATOR,
    )
    application_id = str(admitted.application_id)
    driver = ControlledScenarioTestDriver(service)
    failed = driver.process_next_job(operation_fault="compensation_failed")
    assert failed.status in {"blocked", "failed", "stopped"}, failed.status

    job = next(
        item
        for item in service._store.jobs
        if item.get("application_id") == application_id
    )
    assert job["status"] == "compensation_failed", job

    route = service.current_route_view(
        principal=REVIEWER, application_id=application_id
    )
    cancel = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-comp-failed-job",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted"

    job_after = next(
        item
        for item in service._store.jobs
        if item.get("application_id") == application_id
    )
    assert job_after["status"] == "compensation_failed"

    outstanding = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=cancel["lifecycle_revision"],
        idempotency_key="s14-settle-comp-failed-job",
    )
    assert outstanding["status"] == "outstanding"
    details = {
        item["kind"]: item["detail"]
        for item in outstanding["unresolved_effects"]
    }
    assert details.get("check_job") == "compensation_failed"


def test_new_cycle_settlement_ignores_predecessor_effects() -> None:
    service = make_service()
    application_id, _cancel1, settled1 = _terminate_manual_review(service)
    _grant_exact_permission(
        service,
        application_id,
        viewer=REVIEWER,
        expected_lifecycle_revision=int(settled1["lifecycle_revision"]),
    )
    reopened = service.reopen_application(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=int(settled1["lifecycle_revision"]),
        idempotency_key="s14-reopen-cycle-scope",
        target_phase="Intake",
        reopen_policy=_reopen_policy(service),
    )
    assert reopened["status"] == "accepted"

    jobs_before = len(
        [
            job
            for job in service._store.jobs
            if job.get("application_id") == application_id
        ]
    )
    items_before = len(
        [
            item
            for item in service._store.work_items
            if item.get("application_id") == application_id
        ]
    )

    cancel2 = service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(reopened["lifecycle_revision"]),
        idempotency_key="s14-cancel-cycle-two",
        reason_code="UPSTREAM_WITHDRAWN_AGAIN",
    )
    assert cancel2["status"] == "accepted"
    assert cancel2["cycle"] == 2
    # No predecessor effect may be refenced by the successor cancellation.
    assert cancel2["fenced_effects"]["jobs"] == 0
    assert cancel2["fenced_effects"]["review_work_items"] == 0
    assert len(
        [
            job
            for job in service._store.jobs
            if job.get("application_id") == application_id
        ]
    ) == jobs_before
    assert len(
        [
            item
            for item in service._store.work_items
            if item.get("application_id") == application_id
        ]
    ) == items_before

    armed = service.settle_termination(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=int(cancel2["lifecycle_revision"]),
        idempotency_key="s14-settle-cycle-two",
    )
    assert armed["status"] == "outstanding"
    kinds = {item["kind"] for item in armed["unresolved_effects"]}
    assert kinds == {"termination_notification"}
    assert "check_job" not in kinds and "review_work_item" not in kinds


def test_grant_permission_stale_revision_is_rejected() -> None:
    service = make_service()
    application_id, _c, settled = _terminate_manual_review(service)
    stale = service.grant_reopen_permission(
        application_id=application_id,
        principal=OPERATOR,
        approver_subject=APPROVER.subject,
        permission_id="perm-stale-grant",
        expected_lifecycle_revision=int(settled["lifecycle_revision"]) - 1,
        idempotency_key="s14-grant-stale-rev",
    )
    assert stale["status"] == "stale"
    assert stale["reason_code"] == "lifecycle.permission_stale_revision"


def test_direct_grant_and_reopen_source_mismatch_is_rejected() -> None:
    service = make_service()
    application_id, _c, settled = _terminate_manual_review(service)
    rogue_operator = S01CommandPrincipal(
        subject="rogue-operator",
        role="operator",
        scope="C-DEMO",
        source_id="rogue-control-plane",
    )
    granted = service.grant_reopen_permission(
        application_id=application_id,
        principal=rogue_operator,
        approver_subject=APPROVER.subject,
        permission_id="perm-rogue-source",
        expected_lifecycle_revision=int(settled["lifecycle_revision"]),
        idempotency_key="s14-grant-rogue-source",
    )
    assert granted["status"] == "accepted"

    mismatch = service.reopen_application(
        application_id=application_id,
        principal=OPERATOR,
        expected_lifecycle_revision=int(settled["lifecycle_revision"]),
        idempotency_key="s14-reopen-source-mismatch",
        target_phase="Intake",
        reopen_policy={
            "permission_id": "perm-rogue-source",
            "release_digest": _release_digest(service),
        },
    )
    assert mismatch["status"] == "rejected"
    assert mismatch["reason_code"] == "lifecycle.reopen_permission_source_mismatch"


def test_expired_operator_cannot_grant_or_reopen() -> None:
    service = make_service()
    application_id, _c, settled = _terminate_manual_review(service)
    expired = S01CommandPrincipal(
        subject=OPERATOR.subject,
        role="operator",
        scope="C-DEMO",
        source_id=OPERATOR.source_id,
        expires_at=1.0,
    )
    grant = service.grant_reopen_permission(
        application_id=application_id,
        principal=expired,
        approver_subject=APPROVER.subject,
        permission_id="perm-expired-op",
        expected_lifecycle_revision=int(settled["lifecycle_revision"]),
        idempotency_key="s14-grant-expired-op",
    )
    assert grant["status"] == "rejected"
    assert grant["reason_code"] == "FORBIDDEN"

    reopen = service.reopen_application(
        application_id=application_id,
        principal=expired,
        expected_lifecycle_revision=int(settled["lifecycle_revision"]),
        idempotency_key="s14-reopen-expired-op",
        target_phase="Intake",
        reopen_policy={
            "permission_id": "institutional-reopen-permission/1",
            "release_digest": _release_digest(service),
        },
    )
    assert reopen["status"] == "rejected"
    assert reopen["reason_code"] == "lifecycle.reopen_forbidden"


def test_policy_impact_cancel_race_is_idempotent_on_replay() -> None:
    from task4_consistency.controlled.s01 import ControlledScenarioTestDriver

    digest = "c" * 64
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=RULES,
        state_path=Path(tempfile.mkdtemp(prefix="xiaopeng-s14-impact2-"))
        / "target.sqlite3",
    )
    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s14-impact-idem-intake",
        principal=INTEGRATOR,
    )
    application_id = str(admitted.application_id)
    driver = ControlledScenarioTestDriver(service)
    assert driver.process_next_job().status == "complete"
    service.refresh_projection()
    service._policy_governance = _StubGovernance(
        {
            "members": [
                {
                    "application_id": application_id,
                    "cycle": 1,
                    "partition": "open_cycle",
                    "target_generation": 2,
                    "hit_reasons": ["rules_change"],
                    "required_disposition": "operational_reevaluation",
                }
            ]
        }
    )
    driver.stage_impact_activation(final_impact_digest=digest)
    route = service.current_route_view(
        principal=REVIEWER, application_id=application_id
    )
    service.cancel_application(
        application_id=application_id,
        principal=INTEGRATOR,
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="s14-cancel-impact-idem",
        reason_code="UPSTREAM_WITHDRAWN",
    )

    assert service.process_next_policy_impact() == 1
    # Duplicate delivery of the same impact message is a no-op.
    assert service.process_next_policy_impact() == 0
    receipts = [
        m
        for m in service._store.inbox
        if m.get("kind") == "s09_impact_disposition"
        and m.get("application_id") == application_id
    ]
    assert len(receipts) == 1
    assert receipts[0]["disposition"] == "stale_terminated_cycle"
