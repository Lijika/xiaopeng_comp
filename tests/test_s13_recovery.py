"""Ticket #29 S13 — recovery, unknown, reconciliation, compensation."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from task4_consistency.controlled.s01 import ControlledScenarioService, S01CommandPrincipal
from task4_consistency.controlled.s01_store import SQLiteTargetStore
from task4_consistency.controlled.s13 import (
    DownstreamRecipientRegistration,
    InMemoryDownstreamAdapter,
    RegisteredDownstreamRegistry,
)

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"

INTEGRATOR = S01CommandPrincipal(
    subject="registered-test-integrator",
    role="integrator",
    scope="C-DEMO",
    source_id="s01-test-client",
)


def _service_with_adapter(
    tmp_path: Path, adapter: InMemoryDownstreamAdapter
) -> tuple[ControlledScenarioService, Any, InMemoryDownstreamAdapter]:
    reg = DownstreamRecipientRegistration(
        scope="C-DEMO",
        recipient_registration_id="c-demo-downstream-review-default",
        recipient_id="downstream-review-desk",
        adapter_id=adapter.adapter_id,
        adapter_version=adapter.adapter_version,
    )
    registry = RegisteredDownstreamRegistry([reg], {adapter.adapter_id: adapter})
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=RULES,
        state_path=tmp_path / "target.sqlite3",
        downstream_registry=registry,
    )
    return service, reg, adapter


def _admit_and_complete(service: ControlledScenarioService) -> str:
    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s13-recovery-intake",
        principal=INTEGRATOR,
    )
    assert admitted.application_id is not None
    orig = service.verification_route_for_checks
    service.verification_route_for_checks = lambda checks, findings: "auto_complete"  # type: ignore[assignment,method-assign]
    result = service.process_next_job()
    service.verification_route_for_checks = orig  # type: ignore[assignment]
    service.refresh_projection()
    assert result.status == "complete"
    return str(admitted.application_id)


def test_sender_claims_before_send_and_duplicate_claim_is_idle(
    tmp_path: Path,
) -> None:
    adapter = InMemoryDownstreamAdapter()
    service, _, adapter = _service_with_adapter(tmp_path, adapter)
    app_id = _admit_and_complete(service)
    first = service.process_next_delivery()
    assert first["status"] == "received"
    # The claim transaction already marked outbox published; a second sender
    # invocation must be idle and must not create a second inbox entry.
    second = service.process_next_delivery()
    assert second["status"] == "idle"
    store = SQLiteTargetStore(tmp_path / "target.sqlite3")
    inbox = [e for e in store.inbox if e.get("kind") == "s13_delivery_receipt"]
    assert len(inbox) == 1
    assert len(store.delivery_attempts) == 1


def test_timeout_after_remote_execution_becomes_unknown_and_same_operation_reconcile_confirms(
    tmp_path: Path,
) -> None:
    adapter = InMemoryDownstreamAdapter(behavior="timeout_after_execute")
    service, _, adapter = _service_with_adapter(tmp_path, adapter)
    app_id = _admit_and_complete(service)
    send = service.process_next_delivery()
    assert send["status"] == "unknown"
    assert service.delivery_view(application_id=app_id)["delivery_status"] == "unknown"
    # The adapter DID execute remotely but returned timeout; the caller must
    # reconcile with the original operation id — not blindly retry.
    op_before = service.delivery_view(application_id=app_id)["obligation"]["operation_id"]
    assert op_before in adapter.executed_operations
    # Obligation is not yet received.
    assert not any(e.get("kind") == "s13_delivery_receipt" for e in service._store.inbox)
    # Reconciliation via same operation id proves confirmed.
    ob = service.delivery_view(application_id=app_id)["obligation"]
    recon = service.reconcile_delivery(obligation_id=ob["obligation_id"])
    assert recon["status"] == "received"
    view = service.delivery_view(application_id=app_id)
    assert view["delivery_status"] == "received"
    # One business effect despite the timeout side effect.
    assert len([e for e in service._store.inbox if e.get("kind") == "s13_delivery_receipt"]) == 1
    # The operation id remained stable throughout.
    assert view["obligation"]["operation_id"] == op_before


def test_timeout_without_execution_reconcile_not_executed_allows_same_operation_retry(
    tmp_path: Path,
) -> None:
    adapter = InMemoryDownstreamAdapter(behavior="timeout_without_execute")
    service, _, adapter = _service_with_adapter(tmp_path, adapter)
    app_id = _admit_and_complete(service)
    send = service.process_next_delivery()
    assert send["status"] == "unknown"
    ob = service.delivery_view(application_id=app_id)["obligation"]
    op = ob["operation_id"]
    recon = service.reconcile_delivery(obligation_id=ob["obligation_id"])
    assert recon["status"] == "retry_scheduled"
    # Retry is only allowed after proven not_executed; switching adapter to
    # confirm must then succeed with the same operation id.
    adapter.behavior = "confirm"
    retry = service.process_next_delivery()
    assert retry["status"] == "received"
    assert retry["operation_id"] == op
    view = service.delivery_view(application_id=app_id)
    assert view["delivery_status"] == "received"
    assert len(service._store.delivery_attempts) == 2
    # Inbox still one effect (the not_executed path produced no effect, the
    # retry produced one).
    assert len([e for e in service._store.inbox if e.get("kind") == "s13_delivery_receipt"]) == 1


def test_unknown_without_reconciliation_cannot_be_blindly_retried(tmp_path: Path) -> None:
    adapter = InMemoryDownstreamAdapter(behavior="timeout_without_execute")
    service, _, adapter = _service_with_adapter(tmp_path, adapter)
    app_id = _admit_and_complete(service)
    service.process_next_delivery()
    assert service.delivery_view(application_id=app_id)["delivery_status"] == "unknown"
    # A blind retry without reconciliation must be idle — the obligation's
    # status is unknown, not retry_scheduled, so the sender does not pick it.
    blind = service.process_next_delivery()
    assert blind["status"] == "idle"


def test_duplicate_adapter_response_one_business_effect(tmp_path: Path) -> None:
    """Duplicate delivery confirms with same operation id append at most one inbox."""
    adapter = InMemoryDownstreamAdapter()
    service, _, adapter = _service_with_adapter(tmp_path, adapter)
    app_id = _admit_and_complete(service)
    first = service.process_next_delivery()
    assert first["status"] == "received"
    # Simulate a duplicate downstream response by calling the sender again
    # (which would be a duplicate outbox claim, but outbox is already
    # published).  Explicitly invoke a second reconcile on the same operation
    # to simulate duplicate receipt; inbox dedupe must keep one effect.
    ob = service.delivery_view(application_id=app_id)["obligation"]
    inbox_before = len([e for e in service._store.inbox if e.get("kind") == "s13_delivery_receipt"])
    # A second reconcile after received is stale and has no effect.
    recon2 = service.reconcile_delivery(obligation_id=ob["obligation_id"])
    assert recon2["status"] == "stale"
    assert len([e for e in service._store.inbox if e.get("kind") == "s13_delivery_receipt"]) == inbox_before


def test_restart_reload_preserves_delivery_attempt_and_inbox(tmp_path: Path) -> None:
    adapter = InMemoryDownstreamAdapter(behavior="timeout_after_execute")
    service, _, adapter = _service_with_adapter(tmp_path, adapter)
    app_id = _admit_and_complete(service)
    service.process_next_delivery()
    assert service.delivery_view(application_id=app_id)["delivery_status"] == "unknown"
    state_path = tmp_path / "target.sqlite3"
    reloaded = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=RULES,
        state_path=state_path,
        downstream_registry=service._downstream_registry,
    )
    assert reloaded.delivery_view(application_id=app_id)["delivery_status"] == "unknown"
    # Same-operation reconciliation still works after restart.
    ob = reloaded.delivery_view(application_id=app_id)["obligation"]
    recon = reloaded.reconcile_delivery(obligation_id=ob["obligation_id"])
    assert recon["status"] == "received"
    assert reloaded.delivery_view(application_id=app_id)["delivery_status"] == "received"


def test_compensation_failure_creates_recovery_work_and_keeps_obligation_unresolved(
    tmp_path: Path,
) -> None:
    adapter = InMemoryDownstreamAdapter(compensation_behavior="fail")
    service, _, adapter = _service_with_adapter(tmp_path, adapter)
    app_id = _admit_and_complete(service)
    service.process_next_delivery()
    assert service.delivery_view(application_id=app_id)["delivery_status"] == "received"
    ob = service.delivery_view(application_id=app_id)["obligation"]
    comp = service.compensate_delivery(obligation_id=ob["obligation_id"])
    assert comp["status"] == "failed"
    assert service.delivery_view(application_id=app_id)["delivery_status"] == "compensation_failed"
    # A recovery work item and a compensation_failed audit remain visible.
    assert any(
        event.get("obligation_id") == ob["obligation_id"]
        and "comp" in str(event.get("kind") or "")
        for event in service._store.recovery_events
    ) or any(
        event.get("obligation_id") == ob["obligation_id"]
        for event in service._store.delivery_compensations
    )
    assert any(
        record.get("action") == "s13_compensation_failed"
        for record in service._store.audit_events
    )
    # Forward compensation: the original obligation is still present and not deleted.
    assert service._s13_obligation(ob["obligation_id"]) is not None


def test_compensation_succeeds_marks_obligation_compensated(tmp_path: Path) -> None:
    adapter = InMemoryDownstreamAdapter(compensation_behavior="succeed")
    service, _, adapter = _service_with_adapter(tmp_path, adapter)
    app_id = _admit_and_complete(service)
    service.process_next_delivery()
    ob = service.delivery_view(application_id=app_id)["obligation"]
    comp = service.compensate_delivery(obligation_id=ob["obligation_id"])
    assert comp["status"] == "compensated"
    assert service.delivery_view(application_id=app_id)["delivery_status"] == "compensated"


def test_sender_disable_stops_new_claims_and_obligations_remain_queryable(
    tmp_path: Path,
) -> None:
    adapter = InMemoryDownstreamAdapter()
    service, _, adapter = _service_with_adapter(tmp_path, adapter)
    app_id = _admit_and_complete(service)
    service._delivery_sender_enabled = False
    disabled = service.process_next_delivery()
    assert disabled["status"] == "disabled"
    # Obligations remain durable and queryable while the sender is stopped.
    view = service.delivery_view(application_id=app_id)
    assert view["obligation"] is not None
    assert view["delivery_status"] == "pending"
    # Re-enable replays the pending obligation.
    service._delivery_sender_enabled = True
    sent = service.process_next_delivery()
    assert sent["status"] == "received"


def test_attempt_fence_and_lease_are_monotonic_and_visible(tmp_path: Path) -> None:
    adapter = InMemoryDownstreamAdapter(behavior="timeout_without_execute")
    service, _, adapter = _service_with_adapter(tmp_path, adapter)
    app_id = _admit_and_complete(service)
    service.process_next_delivery()
    # Reconcile to not_executed to allow retry.
    ob = service.delivery_view(application_id=app_id)["obligation"]
    service.reconcile_delivery(obligation_id=ob["obligation_id"])
    adapter.behavior = "confirm"
    service.process_next_delivery()
    attempts = sorted(
        service._store.delivery_attempts, key=lambda item: int(item.get("attempt_no") or 0)
    )
    assert len(attempts) == 2
    assert attempts[0]["attempt_no"] == 1
    assert attempts[1]["attempt_no"] == 2
    assert attempts[1]["fence"] > attempts[0]["fence"]
    assert attempts[0]["lease_until"] > attempts[0]["started_at"]
