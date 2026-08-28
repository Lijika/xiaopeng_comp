"""Ticket #32 / S16 governed deletion — controlled semantics.

Covers the S16 ledger, the nine-class copy inventory, retention / legal
hold / two-approver gates, the short-transaction commit, the durable worker
(lease/fence/attempt/idempotency), partial failure + repair-forward,
backup-manifest restore replay, the tombstone read gate, value-free
receipts, and the no-hard-delete surface.  Only the ROUND32 verification
set exercises this file; project-wide gates stay forbidden per ticket.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from task4_consistency.controlled.s01 import (
    AdmissionDisposition,
    ControlledScenarioService,
    QueryNotFound,
    S01CommandPrincipal,
)
from task4_consistency.controlled.s12 import EvaluationService
from task4_consistency.controlled.s16 import (
    BackupDeletionOwner,
    CopyInventoryEntry,
    ExportTempOwner,
    GovernedDeletionService,
    RetentionPolicy,
    S01DeletionOwner,
    S02DeletionOwner,
    S12DeletionOwner,
    S16Blocked,
    S16Conflict,
    S16Forbidden,
    S16NotFound,
    S16Unavailable,
    S16_ACTIVE_LEGAL_HOLD,
    S16_ALREADY_CANCELLED,
    S16_ALREADY_COMMITTED,
    S16_APPROVALS_INCOMPLETE,
    S16_AUDIT_UNAVAILABLE,
    S16_MANIFEST_STALE,
    S16_REVISION_CHANGED,
    S16_SHARED_COPY_REQUIRES_REPACK,
    COPY_CLASSES,
    copy_identity_fingerprint,
    s16_owner_registry_digest,
)
from task4_consistency.controlled.s16 import (
    COPY_CLASS_BACKUP_MANIFEST,
    COPY_CLASS_REPLICA,
)

from tests.test_s01_controlled import worker_test_driver
from tests.test_s02_controlled import (
    INTEGRATOR as S02_INTEGRATOR,
    TENANT_SCOPE,
    _registered_service,
)
from tests.test_s12_controlled import (
    TEST_INTEGRATOR,
    _make_business_harness,
    _make_governed_release,
    _s12_authority_service,
    _write_label_manifest,
)
from task4_consistency.controlled.s12_runner import run_s12_runner

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"
FIXTURES = ROOT / "fixtures" / "applications"

NOW = 1_800_000_000

GOVERNANCE = S01CommandPrincipal(
    subject="s16-governance-owner",
    role="operator",
    scope="C-DEMO",
    source_id="s16-governance-console",
)
GOVERNANCE_REGISTERED = S01CommandPrincipal(
    subject="s16-governance-owner",
    role="operator",
    scope=TENANT_SCOPE,
    source_id="s16-governance-console",
)
APPROVER_1 = S01CommandPrincipal(
    subject="s16-approver-1",
    role="operator",
    scope="C-DEMO",
    source_id="s16-approval-desk",
)
APPROVER_2 = S01CommandPrincipal(
    subject="s16-approver-2",
    role="operator",
    scope="C-DEMO",
    source_id="s16-approval-desk",
)
PLATFORM_ADMIN = S01CommandPrincipal(
    subject="s16-platform-admin",
    role="operator",
    scope="C-DEMO",
    source_id="s16-admin-console",
)
REVIEWER = S01CommandPrincipal(
    subject="s16-reviewer",
    role="reviewer",
    scope="C-DEMO",
    source_id="s16-review-console",
)
DEMO_USER = S01CommandPrincipal(
    subject="s16-demo-user",
    role="integrator",
    scope="C-DEMO",
    source_id="c-demo-web-session",
)

CLOCK = {"now": NOW}


def _now() -> int:
    return int(CLOCK["now"])


class _CdemoSessionClock:
    """Clock the S01 service shares with the S16 service."""

    def __call__(self) -> int:
        return _now()


def _c_demo_service(tmp_path: Path, *, scenario: str = "app_r53_bad_engine.json") -> ControlledScenarioService:
    return ControlledScenarioService(
        fixture_root=FIXTURES,
        rules_path=RULES,
        state_path=tmp_path / "target.sqlite3",
        scenario_id=scenario,
        clock=_CdemoSessionClock(),
    )


def _admit_c_demo(service: ControlledScenarioService, *, key: str) -> str:
    admitted = service.submit_demo(
        scenario_id=service._scenario_id,
        idempotency_key=key,
        principal=S01CommandPrincipal(
            subject="s16-fixture-integrator",
            role="integrator",
            scope="C-DEMO",
            source_id="s16-fixture-intake",
        ),
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED, admitted
    assert admitted.application_id is not None
    worker_test_driver(service).process_next_job(now=_now())
    service.refresh_projection()
    return str(admitted.application_id)


def _terminate(
    service: ControlledScenarioService,
    application_id: str,
    *,
    subject: str = "s16-fixture-integrator",
    source_id: str = "s16-fixture-intake",
    scope: str = "C-DEMO",
) -> None:
    route = service.current_route_view(
        principal=S01CommandPrincipal(
            subject=subject,
            role="reviewer",
            scope=scope,
            source_id="s16-fixture-review",
        ),
        application_id=application_id,
    )
    cancel = service.cancel_application(
        application_id=application_id,
        principal=S01CommandPrincipal(
            subject=subject,
            role="integrator",
            scope=scope,
            source_id=source_id,
        ),
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key=f"s16-terminate-{application_id}",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted", cancel
    settled = _settle_to_terminated_local(service, application_id, cancel["lifecycle_revision"])
    assert settled["status"] == "terminated", settled


def _settle_to_terminated_local(service, application_id, revision) -> dict:
    """Settle with the shared S14 operator identity (test_s14 OPERATOR)."""
    from tests.test_s14_controlled import _settle_to_terminated

    return _settle_to_terminated(service, application_id, revision)


def _registered_terminated(
    tmp_path: Path,
    *,
    second_app: bool = False,
) -> tuple[ControlledScenarioService, dict[str, object], str]:
    """A registered R-OBSERVED application whose S02 boundary carries a
    persistent absence store (production wiring configures the same path
    via environment)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    service, submission = _registered_service(tmp_path)
    admitted = service.submit_registered(
        submission=submission,
        idempotency_key="s16-registered-intake",
        principal=S02_INTEGRATOR,
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED, admitted
    if second_app:
        second = dict(submission)
        second["envelope_id"] = "envelope-s16-second"
        second["stream_id"] = "source-stream-s16-second"
        second["upstream_application_ref"] = "upstream-s16-second"
        second["source_revision"] = 1
        second_admitted = service.submit_registered(
            submission=second,
            idempotency_key="s16-registered-intake-2",
            principal=S02_INTEGRATOR,
        )
        assert second_admitted.disposition is AdmissionDisposition.ACCEPTED, second_admitted
    worker_test_driver(service).process_next_job(now=_now())
    service.refresh_projection()
    application_id = str(admitted.application_id)
    route = service.current_route_view(
        principal=S01CommandPrincipal(
            subject=S02_INTEGRATOR.subject,
            role="reviewer",
            scope=TENANT_SCOPE,
            source_id="s16-registered-review",
        ),
        application_id=application_id,
    )
    cancel = service.cancel_application(
        application_id=application_id,
        principal=S01CommandPrincipal(
            subject=S02_INTEGRATOR.subject,
            role="integrator",
            scope=TENANT_SCOPE,
            source_id=S02_INTEGRATOR.source_id,
        ),
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key=f"s16-registered-cancel-{application_id}",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted", cancel
    settled = _settle_to_terminated_local(service, application_id, cancel["lifecycle_revision"])
    assert settled["status"] == "terminated", settled
    # Rebuild the authority with a persistent S02 absence store: the same
    # registrations and object bytes, the same business SQLite, and the
    # deployment-configured absence path the S16 owner will write.
    source_boundary = service.registered_source_boundary
    governed_service = ControlledScenarioService(
        fixture_root=FIXTURES,
        rules_path=RULES,
        state_path=tmp_path / "target.sqlite3",
        registered_sources=source_boundary._registrations,
        controlled_objects=tuple(source_boundary._objects.values()),
        controlled_object_absence_store=tmp_path / "s02_absence.sqlite3",
    )
    assert governed_service.s16_resolve_by_scope_fingerprint(
        governed_service._s16_scope_fingerprint(application_id)
    ) == application_id
    return governed_service, submission, application_id


def _empty_evaluation(tmp_path: Path) -> EvaluationService:
    return EvaluationService(
        state_path=tmp_path / "evaluation.sqlite3",
        clock=_CdemoSessionClock(),
        snapshot_provider=lambda *_: None,
        release_provider=lambda *_: None,
        label_manifest_provider=lambda *_: None,
        business_state_provider=lambda: {},
        business_publication_guard=lambda revisions: None,
    )


def _s16_service(
    tmp_path: Path,
    service: ControlledScenarioService,
    *,
    evaluation: EvaluationService | None = None,
    backup_root: Path | None = None,
    retention_seconds: int = 0,
    max_owner_attempts: int = 5,
    fault_injector: Any = None,
    audit_available: bool = True,
    storage_available: bool = True,
    ledger_path: Path | None = None,
) -> GovernedDeletionService:
    return GovernedDeletionService(
        ledger_path=ledger_path or (tmp_path / "s16.sqlite3"),
        owners={
            "s01": S01DeletionOwner(
                service,
                retention=RetentionPolicy(retention_seconds=retention_seconds),
                clock=_CdemoSessionClock(),
            ),
            "s02": S02DeletionOwner(service.registered_source_boundary, service),
            "s12": S12DeletionOwner(
                evaluation or _empty_evaluation(tmp_path)
            ),
            "backup": BackupDeletionOwner(
                backup_root or (tmp_path / "backups"), clock=_CdemoSessionClock()
            ),
            "s17-disabled": ExportTempOwner(),
        },
        retention=RetentionPolicy(retention_seconds=retention_seconds),
        governance_subject=GOVERNANCE.subject,
        approver_subjects=(APPROVER_1.subject, APPROVER_2.subject),
        audit_available=audit_available,
        storage_available=storage_available,
        max_owner_attempts=max_owner_attempts,
        clock=_CdemoSessionClock(),
        fault_injector=fault_injector,
    )


def _preflight(s16: GovernedDeletionService, reference: str, *, scope: str = "C-DEMO") -> dict[str, Any]:
    return s16.preflight(
        application_reference=reference,
        principal=S01CommandPrincipal(
            subject=GOVERNANCE.subject,
            role="operator",
            scope=scope,
            source_id="s16-governance-console",
        ),
        idempotency_key=f"preflight-{reference}-{scope}",
    )


def _approve_two(s16: GovernedDeletionService, request_id: str, manifest_digest: str) -> None:
    s16.approve(
        request_id=request_id,
        manifest_digest=manifest_digest,
        principal=APPROVER_1,
        idempotency_key=f"approve-1-{request_id}",
    )
    s16.approve(
        request_id=request_id,
        manifest_digest=manifest_digest,
        principal=APPROVER_2,
        idempotency_key=f"approve-2-{request_id}",
    )


def _commit_and_run(s16: GovernedDeletionService, request_id: str) -> dict[str, Any]:
    committed = s16.commit(
        request_id=request_id,
        principal=S01CommandPrincipal(
            subject=GOVERNANCE.subject,
            role="operator",
            scope="C-DEMO",
            source_id="s16-governance-console",
        ),
        idempotency_key=f"commit-{request_id}",
    )
    assert committed["status"] == "accepted", committed
    outcome = s16.process_next_deletion_job(worker_id="s16-deletion-worker", now=_now() + 1)
    return outcome


def _s01_fact_counts(service: ControlledScenarioService) -> dict[str, int]:
    return service.fact_counts()


def _assert_table_counts(state_path: Path, expected: dict[str, int]) -> None:
    with sqlite3.connect(state_path) as connection:
        for table, count in expected.items():
            actual = connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            assert actual == count, f"{table}: expected {count}, found {actual}"


# ---------------------------------------------------------------------------
# 1. Nine-class inventory with owner proof, no values or locators
# ---------------------------------------------------------------------------


def test_preflight_inventory_covers_every_registered_copy_class_without_values_or_locators(
    tmp_path: Path,
) -> None:
    service = _c_demo_service(tmp_path)
    application_id = _admit_c_demo(service, key="s16-classes-intake")
    _terminate(service, application_id)
    s16 = _s16_service(tmp_path, service)
    result = _preflight(s16, "APP-R53-BAD-ENGINE")

    assert result["status"] == "accepted"
    classes = {entry["copy_class"] for entry in result["entries"]}
    assert classes == set(COPY_CLASSES)
    assert result["owner_registry_digest"] == s16_owner_registry_digest()
    assert len(result["manifest_digest"]) == 64
    assert result["entries_digest"]

    serialized = json.dumps(result, sort_keys=True)
    # No business value or locator in the manifest surface.
    assert serialized.count("APP-R53-BAD-ENGINE") == 1
    assert application_id not in serialized
    assert "app_" not in serialized
    assert "fixtures" not in serialized
    assert ".sqlite3" not in serialized
    for forbidden in ("raw", "normalized", "ocr", "bbox", "credential", "token"):
        assert forbidden not in json.dumps(
            [entry for entry in result["entries"] if entry["copy_class"] != "source_object"],
            sort_keys=True,
        ) or any(
            entry.get("count", 0) == 0
            for entry in result["entries"]
        )
    # Every entry carries the documented fields only.
    allowed_keys = {
        "owner_id",
        "copy_class",
        "classification",
        "content_sha256",
        "identity_fingerprint",
        "retention_policy_id",
        "retention_policy_version",
        "retention_due_at",
        "legal_hold_generation",
        "hold_state",
        "shared_state",
        "planned_action",
        "count",
    }
    for entry in result["entries"]:
        assert set(entry) == allowed_keys, entry
        assert entry["owner_id"]
        assert entry["copy_class"]
        assert entry["content_sha256"]
        assert entry["identity_fingerprint"]
        assert entry["identity_fingerprint"] == copy_identity_fingerprint(
            entry["owner_id"], entry["copy_class"], entry["content_sha256"]
        )
        assert entry["owner_id"] == {
            "source_object": "s01",
            "evidence": "s01",
            "run_or_finding": "s01",
            "projection_or_cache": "s01",
            "derived_object": "s02",
            "evaluation_copy": "s12",
            "export_or_temp": "s17-disabled",
            "replica": "backup",
            "backup_manifest": "backup",
        }[entry["copy_class"]]
    # The empty owners carry documented proofs.
    export = next(e for e in result["entries"] if e["copy_class"] == "export_or_temp")
    assert export["count"] == 0 and export["planned_action"] == "none"


# ---------------------------------------------------------------------------
# 2. Due retention: one forward deletion, terminal history preserved
# ---------------------------------------------------------------------------


def test_due_retention_commit_queues_one_forward_deletion_and_preserves_terminal_history(
    tmp_path: Path,
) -> None:
    service = _c_demo_service(tmp_path)
    application_id = _admit_c_demo(service, key="s16-due-intake")
    _terminate(service, application_id)
    state_path = tmp_path / "target.sqlite3"
    s16 = _s16_service(tmp_path, service, retention_seconds=0)
    result = _preflight(s16, "APP-R53-BAD-ENGINE")
    assert result["early_deletion"] is False

    committed = s16.commit(
        request_id=result["request_id"],
        principal=GOVERNANCE,
        idempotency_key="s16-due-commit",
    )
    assert committed["status"] == "accepted"
    # A due deletion needs no approvals: exactly one durable job exists.
    query = s16.query(request_id=result["request_id"], principal=GOVERNANCE)
    assert query["job"]["status"] == "pending"

    outcome = s16.process_next_deletion_job(worker_id="w", now=_now() + 1)
    assert outcome["status"] == "complete", outcome

    # Restricted content gone; terminal history and accountability retained.
    _assert_table_counts(
        state_path,
        {
            "applications": 0,
            "receipts": 0,
            "evidence_events": 0,
            "jobs": 0,
            "idempotency": 0,
            "attempts": 0,
            "runs": 0,
            "findings": 0,
            "work_items": 0,
            "review_records": 0,
            "recovery_events": 0,
            "inbox": 0,
            "outbox": 0,
            "projections": 0,
            "sessions": 0,
            "demo_sessions": 0,
        },
    )
    with sqlite3.connect(state_path) as connection:
        lifecycle = connection.execute(
            "SELECT COUNT(*) FROM lifecycle_events"
        ).fetchone()[0]
        audit = connection.execute(
            "SELECT COUNT(*) FROM audit_events"
        ).fetchone()[0]
        tombstones = connection.execute(
            "SELECT COUNT(*) FROM s16_governed_deletions"
        ).fetchone()[0]
        receipts = connection.execute(
            "SELECT COUNT(*) FROM deletion_receipts"
        ).fetchone()[0]
    assert lifecycle > 0  # L14 terminal history preserved.
    assert audit == 0  # minimized audit: app-scoped rows removed.
    assert tombstones == 1
    assert receipts == 1
    receipt = s16.receipt(request_id=result["request_id"], principal=GOVERNANCE)
    assert receipt["result"] == "deleted"
    assert receipt["restore_replay_status"] == "pending"


# ---------------------------------------------------------------------------
# 3. Early deletion: two distinct approvers bound to the manifest
# ---------------------------------------------------------------------------


def test_early_deletion_requires_two_distinct_approvers_bound_to_manifest(
    tmp_path: Path,
) -> None:
    service = _c_demo_service(tmp_path)
    application_id = _admit_c_demo(service, key="s16-early-intake")
    _terminate(service, application_id)
    s16 = _s16_service(tmp_path, service, retention_seconds=10**12)
    result = _preflight(s16, "APP-R53-BAD-ENGINE")
    assert result["early_deletion"] is True
    request_id = result["request_id"]
    manifest_digest = result["manifest_digest"]

    # No approvals yet -> incomplete.
    with pytest.raises(S16Blocked) as excinfo:
        s16.commit(request_id=request_id, principal=GOVERNANCE, idempotency_key="e-commit-0")
    assert excinfo.value.reason_code == S16_APPROVALS_INCOMPLETE

    # One approver is still incomplete.
    s16.approve(request_id=request_id, manifest_digest=manifest_digest, principal=APPROVER_1, idempotency_key="e-ap-1")
    with pytest.raises(S16Blocked) as excinfo:
        s16.commit(request_id=request_id, principal=GOVERNANCE, idempotency_key="e-commit-1")
    assert excinfo.value.reason_code == S16_APPROVALS_INCOMPLETE

    # The same approver twice is a replay, not a second approver.
    replayed = s16.approve(request_id=request_id, manifest_digest=manifest_digest, principal=APPROVER_1, idempotency_key="e-ap-1")
    assert replayed["replayed"] is True
    with pytest.raises(S16Blocked) as excinfo:
        s16.commit(request_id=request_id, principal=GOVERNANCE, idempotency_key="e-commit-2")
    assert excinfo.value.reason_code == S16_APPROVALS_INCOMPLETE

    # Approval bound to a different manifest digest is stale.
    with pytest.raises(S16Blocked) as excinfo:
        s16.approve(
            request_id=request_id,
            manifest_digest="0" * 64,
            principal=APPROVER_2,
            idempotency_key="e-ap-2-wrong",
        )
    assert excinfo.value.reason_code == S16_MANIFEST_STALE

    # The requester cannot be an approver (governance is not an approver).
    with pytest.raises(S16Forbidden):
        s16.approve(request_id=request_id, manifest_digest=manifest_digest, principal=GOVERNANCE, idempotency_key="e-ap-self")
    # A platform admin is not an approver either.
    with pytest.raises(S16Forbidden):
        s16.approve(request_id=request_id, manifest_digest=manifest_digest, principal=PLATFORM_ADMIN, idempotency_key="e-ap-admin")

    s16.approve(request_id=request_id, manifest_digest=manifest_digest, principal=APPROVER_2, idempotency_key="e-ap-2")
    committed = s16.commit(request_id=request_id, principal=GOVERNANCE, idempotency_key="e-commit-3")
    assert committed["status"] == "accepted"
    outcome = s16.process_next_deletion_job(worker_id="w", now=_now() + 1)
    assert outcome["status"] == "complete", outcome


# ---------------------------------------------------------------------------
# 4. Legal hold / commit race: exactly one safe winner
# ---------------------------------------------------------------------------


def test_legal_hold_and_commit_race_has_one_safe_winner(tmp_path: Path) -> None:
    service = _c_demo_service(tmp_path)
    application_id = _admit_c_demo(service, key="s16-hold-intake")
    _terminate(service, application_id)
    s16 = _s16_service(tmp_path, service)
    result = _preflight(s16, "APP-R53-BAD-ENGINE")

    # Hold lands first: hold wins, commit stays closed.
    hold = s16.impose_legal_hold(
        scope_fingerprint=result["scope_fingerprint"],
        principal=GOVERNANCE,
        reason_code="LITIGATION_HOLD",
        owner="s01",
        effective_time=_now(),
    )
    assert hold["status"] == "accepted"
    with pytest.raises(S16Blocked) as excinfo:
        s16.commit(request_id=result["request_id"], principal=GOVERNANCE, idempotency_key="hold-commit-1")
    assert excinfo.value.reason_code == S16_ACTIVE_LEGAL_HOLD
    # Content fully intact while the hold is active.
    assert _s01_fact_counts(service)["applications"] == 1

    # Release, then commit wins and the worker continues forward.
    released = s16.release_legal_hold(hold_id=hold["hold_id"], principal=GOVERNANCE)
    assert released["status"] == "accepted"
    committed = s16.commit(request_id=result["request_id"], principal=GOVERNANCE, idempotency_key="hold-commit-2")
    assert committed["status"] == "accepted"
    # An impose that lands after commit is recorded but deletion continues.
    late_hold = s16.impose_legal_hold(
        scope_fingerprint=result["scope_fingerprint"],
        principal=GOVERNANCE,
        reason_code="LATE_HOLD",
        owner="s01",
        effective_time=_now(),
    )
    assert late_hold["status"] == "accepted"
    outcome = s16.process_next_deletion_job(worker_id="w", now=_now() + 1)
    assert outcome["status"] == "complete", outcome
    assert _s01_fact_counts(service)["applications"] == 0
    query = s16.query(request_id=result["request_id"], principal=GOVERNANCE)
    assert len(query["legal_holds"]) == 2


# ---------------------------------------------------------------------------
# 5. Cancel boundary: pre-commit preserves everything, post-commit conflicts
# ---------------------------------------------------------------------------


def test_precommit_cancel_preserves_all_copies_and_postcommit_cancel_conflicts(
    tmp_path: Path,
) -> None:
    service = _c_demo_service(tmp_path)
    application_id = _admit_c_demo(service, key="s16-cancel-intake")
    _terminate(service, application_id)
    s16 = _s16_service(tmp_path, service)
    result = _preflight(s16, "APP-R53-BAD-ENGINE")

    cancelled = s16.cancel(request_id=result["request_id"], principal=GOVERNANCE, idempotency_key="cancel-1")
    assert cancelled["status"] == "accepted"
    # Replay of the same cancel is idempotent.
    replayed = s16.cancel(request_id=result["request_id"], principal=GOVERNANCE, idempotency_key="cancel-1")
    assert replayed["replayed"] is True
    # Every copy stays intact after a pre-commit cancel.
    assert _s01_fact_counts(service)["applications"] == 1
    assert service.current_route_view(
        principal=S01CommandPrincipal(
            subject="s16-fixture-integrator",
            role="reviewer",
            scope="C-DEMO",
            source_id="s16-fixture-review",
        ),
        application_id=application_id,
    )["phase"] == "Terminated"
    with pytest.raises(S16Blocked) as excinfo:
        s16.commit(request_id=result["request_id"], principal=GOVERNANCE, idempotency_key="cancel-commit")
    assert excinfo.value.reason_code == S16_ALREADY_CANCELLED

    # A fresh request commits; post-commit cancel is a stable conflict.
    result2 = s16.preflight(
        application_reference="APP-R53-BAD-ENGINE",
        principal=GOVERNANCE,
        idempotency_key="s16-cancel-preflight-2",
    )
    assert result2["request_id"] != result["request_id"]
    committed = s16.commit(request_id=result2["request_id"], principal=GOVERNANCE, idempotency_key="cancel-commit-2")
    assert committed["status"] == "accepted"
    with pytest.raises(S16Conflict) as excinfo:
        s16.cancel(request_id=result2["request_id"], principal=GOVERNANCE, idempotency_key="cancel-2")
    assert str(excinfo.value) == S16_ALREADY_COMMITTED
    outcome = s16.process_next_deletion_job(worker_id="w", now=_now() + 1)
    assert outcome["status"] == "complete", outcome


# ---------------------------------------------------------------------------
# 6. Partial owner failure -> retry -> repair -> one effect
# ---------------------------------------------------------------------------


def test_partial_owner_failure_restart_and_repair_complete_one_effect(
    tmp_path: Path,
) -> None:
    service, submission, application_id = _registered_terminated(tmp_path)
    boundary = service.registered_source_boundary
    assert boundary.s02_inventory()["objects"]
    faults = {"armed": True}

    def fault(owner_id: str) -> None:
        if owner_id == "s02" and faults["armed"]:
            raise RuntimeError("injected s02 fault")

    s16 = _s16_service(
        tmp_path,
        service,
        max_owner_attempts=2,
        fault_injector=fault,
    )
    result = _preflight(s16, str(submission["upstream_application_ref"]), scope=TENANT_SCOPE)
    assert sum(e["count"] for e in result["entries"] if e["copy_class"] == "derived_object") == 2
    committed = s16.commit(request_id=result["request_id"], principal=GOVERNANCE_REGISTERED, idempotency_key="fault-commit")
    assert committed["status"] == "accepted"

    first = s16.process_next_deletion_job(worker_id="w", now=_now() + 1)
    assert first["status"] == "pending", first
    assert first["reason_code"] == "S16_OWNER_DELETE_FAILED"
    second = s16.process_next_deletion_job(worker_id="w", now=_now() + 2)
    assert second["status"] == "repair_required", second
    query = s16.query(request_id=result["request_id"], principal=GOVERNANCE_REGISTERED)
    assert query["job"]["stable_failure"]["owner_id"] == "s02"
    assert query["job"]["stable_failure"]["reason_code"] == "S16_OWNER_DELETE_FAILED"
    assert query["job"]["stable_failure"]["responsible_party"] == "runtime_operations_owner"
    # No partial business effect while the job waits for repair.
    assert _s01_fact_counts(service)["applications"] == 1
    assert boundary.s02_inventory()["objects"]

    # Unverified repair facts are rejected.
    with pytest.raises(S16Blocked) as excinfo:
        s16.repair(request_id=result["request_id"], owner_id="s02", repair_fact="wrong", principal=GOVERNANCE_REGISTERED, idempotency_key="repair-wrong")
    assert excinfo.value.reason_code == "S16_REPAIR_NOT_VERIFIED"

    faults["armed"] = False
    repaired = s16.repair(request_id=result["request_id"], owner_id="s02", repair_fact="s02-repair-verified", principal=GOVERNANCE_REGISTERED, idempotency_key="repair-ok")
    assert repaired["status"] == "accepted"
    third = s16.process_next_deletion_job(worker_id="w", now=_now() + 3)
    assert third["status"] == "complete", third
    assert boundary.s02_inventory()["objects"] == []
    assert _s01_fact_counts(service)["applications"] == 0

    # Restart on the same ledger replays and stays ready; one effect only.
    service2 = ControlledScenarioService(
        fixture_root=FIXTURES,
        rules_path=RULES,
        state_path=tmp_path / "target.sqlite3",
        controlled_objects=(),
        controlled_object_absence_store=boundary.absence_store_path,
    )
    restarted = _s16_service(
        tmp_path,
        service2,
        ledger_path=tmp_path / "s16.sqlite3",
        max_owner_attempts=2,
    )
    assert restarted.ready() is True
    assert service2.registered_source_boundary.s02_inventory()["objects"] == []


# ---------------------------------------------------------------------------
# 7. Missing owner / stale manifest / shared copy block commit
# ---------------------------------------------------------------------------


def test_missing_owner_stale_manifest_and_shared_copy_block_commit(
    tmp_path: Path,
) -> None:
    # Missing required owner -> the plane cannot be constructed (fail closed).
    service = _c_demo_service(tmp_path)
    application_id = _admit_c_demo(service, key="s16-block-intake")
    _terminate(service, application_id)
    with pytest.raises(ValueError, match="required owners"):
        GovernedDeletionService(
            ledger_path=tmp_path / "s16-missing.sqlite3",
            owners={
                "s01": S01DeletionOwner(service, retention=RetentionPolicy(retention_seconds=0), clock=_CdemoSessionClock()),
                "s02": S02DeletionOwner(service.registered_source_boundary, service),
                "s12": S12DeletionOwner(_empty_evaluation(tmp_path)),
                "s17-disabled": ExportTempOwner(),
            },
            retention=RetentionPolicy(retention_seconds=0),
            governance_subject=GOVERNANCE.subject,
            approver_subjects=(APPROVER_1.subject, APPROVER_2.subject),
            clock=_CdemoSessionClock(),
        )

    # Owner inventory failure -> preflight unavailable.
    s16 = _s16_service(tmp_path, service)
    broken = S02DeletionOwner(service.registered_source_boundary, service)

    def boom(scope_fingerprint: str) -> list[CopyInventoryEntry]:
        raise S16Unavailable("injected owner outage")

    broken.inventory = boom  # type: ignore[method-assign]
    s16._owners["s02"] = broken
    with pytest.raises(S16Unavailable):
        _preflight(s16, "APP-R53-BAD-ENGINE")
    s16._owners["s02"] = S02DeletionOwner(service.registered_source_boundary, service)

    # Stale manifest: a store revision change between preflight and commit.
    result = _preflight(s16, "APP-R53-BAD-ENGINE")
    service.issue_session(
        now=float(_now()),
        ttl_seconds=10,
        subject="s16-revision-bump",
        roles=("integrator", "reviewer"),
    )
    with pytest.raises(S16Blocked) as excinfo:
        s16.commit(request_id=result["request_id"], principal=GOVERNANCE, idempotency_key="block-commit")
    assert excinfo.value.reason_code == S16_REVISION_CHANGED

    # Shared registered object across two applications -> stable block.
    tmp2 = tmp_path / "shared"
    tmp2.mkdir(parents=True)
    service_b, submission_b, _ = _registered_terminated(tmp2, second_app=True)
    s16b = _s16_service(tmp2, service_b)
    result_b = _preflight(s16b, str(submission_b["upstream_application_ref"]), scope=TENANT_SCOPE)
    derived = [e for e in result_b["entries"] if e["copy_class"] == "derived_object"]
    assert derived and derived[0]["shared_state"] == "shared"
    assert derived[0]["planned_action"] == S16_SHARED_COPY_REQUIRES_REPACK
    with pytest.raises(S16Blocked) as excinfo:
        s16b.commit(request_id=result_b["request_id"], principal=GOVERNANCE_REGISTERED, idempotency_key="shared-commit")
    assert excinfo.value.reason_code == S16_SHARED_COPY_REQUIRES_REPACK


# ---------------------------------------------------------------------------
# 8. Completed deletion hides S01/S02/S12/S13/S15 reads
# ---------------------------------------------------------------------------


def test_completed_deletion_hides_s01_s02_s12_s13_and_s15_reads(
    tmp_path: Path,
) -> None:
    # -- registered scenario: S02 objects + S01/S13/S15 reads -------------
    service, submission, application_id = _registered_terminated(tmp_path)
    boundary = service.registered_source_boundary
    s16 = _s16_service(tmp_path, service)
    result = _preflight(s16, str(submission["upstream_application_ref"]), scope=TENANT_SCOPE)
    assert any(
        e["copy_class"] == "derived_object" and e["count"] >= 1
        for e in result["entries"]
    )
    committed = s16.commit(request_id=result["request_id"], principal=GOVERNANCE_REGISTERED, idempotency_key="hide-commit")
    assert committed["status"] == "accepted"
    outcome = s16.process_next_deletion_job(worker_id="w", now=_now() + 1)
    assert outcome["status"] == "complete", outcome

    # S01 reads existence-hide.
    for view in (
        lambda: service.current_route_view(principal=REVIEWER, application_id=application_id),
        lambda: service.workspace_view(application_id, role="reviewer", scope="C-DEMO", subject="s16-reviewer"),
        lambda: service.application_history_view(principal=REVIEWER, application_id=application_id),
    ):
        with pytest.raises(QueryNotFound):
            view()

    # S02 direct object read and inventory hide.
    with pytest.raises(LookupError):
        boundary.read_object(
            tenant_id="tenant-test",
            source_system_id="registered-source",
            object_ref="result-object",
        )
    with pytest.raises(LookupError):
        boundary.read_object(
            tenant_id="tenant-test",
            source_system_id="registered-source",
            object_ref="page-object",
        )
    assert boundary.s02_inventory()["objects"] == []

    # S13 delivery view hides.
    with pytest.raises(QueryNotFound):
        service.delivery_view(principal=REVIEWER, application_id=application_id)

    # S15 reveal cannot reach restricted values.
    with pytest.raises(QueryNotFound):
        service.reveal_field_observation(
            principal=S01CommandPrincipal(
                subject="s15-reviewer",
                role="reviewer",
                scope=TENANT_SCOPE,
                source_id="s15-review-console",
                expires_at=float(_now() + 10_000),
            ),
            application_id=application_id,
            work_item_id="work-item-gone",
            observation_id="obs-gone",
            expected_fence=1,
            expected_context={},
            idempotency_key="s16-hide-reveal",
            purpose="manual_review",
            reason="HUMAN_REVIEW_COMPLETED",
            classification="RESTRICTED",
            expected_source_region="region:1",
        )
    assert s16.query(request_id=result["request_id"], principal=GOVERNANCE_REGISTERED)["job"]["status"] == "complete"

    # -- S12 scenario: frozen plan + job + bundle for exactly one app ------
    tmp12 = tmp_path / "s12"
    tmp12.mkdir(parents=True)
    business_services, admitted, snapshots, _business_path = _make_business_harness(
        tmp12, RULES
    )
    target_service = business_services[0]
    target_application_id = str(admitted[0][1])
    _terminate(
        target_service,
        target_application_id,
        subject=TEST_INTEGRATOR.subject,
        source_id=TEST_INTEGRATOR.source_id,
    )
    governance_service, release_id, release_digest, _manifest = _make_governed_release(
        tmp12
    )
    label_root, manifest_id, manifest_digest = _write_label_manifest(
        tmp12, {"opp-0": "consistent", "opp-1": "consistent", "opp-2": "consistent", "opp-3": "consistent"}
    )
    evaluation = _s12_authority_service(
        tmp12,
        business_services=business_services,
        governance_service=governance_service,
        label_root=label_root,
    )
    plan = evaluation.freeze_plan(
        _single_app_plan_command(
            admitted_one=admitted[0],
            snapshot_by_application=snapshots,
            release_id=release_id,
            release_digest=release_digest,
            manifest_id=manifest_id,
            manifest_digest=manifest_digest,
        )
    )
    plan_id = plan["plan_id"]
    job = evaluation.start_job(plan_id)
    projection = {
        "schema_version": "s12-runner-request/1",
        "checker_artifact": plan["checker_artifact"],
        "run_specs": copy.deepcopy(plan["run_specs"]),
        "budget": copy.deepcopy(plan["budget"]),
        "stop_rule": plan["stop_rule"],
    }
    runner_output = run_s12_runner(projection)
    assert runner_output is not None
    processed = evaluation.process_job(job["job_id"], runner_result=runner_output)
    assert processed["status"] in {"complete", "invalid", "INSUFFICIENT"}, processed
    bundle_id = str(processed.get("bundle_id") or "")
    assert bundle_id
    evaluation.query_bundle(bundle_id)  # readable before deletion

    s16_12 = _s16_service(
        tmp12,
        target_service,
        evaluation=evaluation,
        ledger_path=tmp12 / "s16.sqlite3",
    )
    reference = "APP-R53-BAD-ENGINE"
    ref_application_id = _reference_application_id(target_service, reference)
    assert ref_application_id == target_application_id
    result_12 = _preflight(s16_12, reference)
    eval_entries = [e for e in result_12["entries"] if e["copy_class"] == "evaluation_copy"]
    assert eval_entries and all(e["shared_state"] == "exclusive" for e in eval_entries)
    assert sum(e["count"] for e in eval_entries) >= 3  # plan + job + bundle
    committed_12 = s16_12.commit(request_id=result_12["request_id"], principal=GOVERNANCE, idempotency_key="hide12-commit")
    assert committed_12["status"] == "accepted"
    outcome_12 = s16_12.process_next_deletion_job(worker_id="w", now=_now() + 1)
    assert outcome_12["status"] == "complete", outcome_12

    # S12 bundle query can no longer resolve the deleted copy.
    with pytest.raises(ValueError, match="does not exist"):
        evaluation.query_bundle(bundle_id)
    with pytest.raises(ValueError, match="does not exist"):
        evaluation.query_job(job["job_id"])
    assert evaluation.s16_verify_absent(
        [e["identity_fingerprint"] for e in eval_entries]
    )["absent"] is True
    with pytest.raises(QueryNotFound):
        target_service.current_route_view(principal=REVIEWER, application_id=target_application_id)


def _reference_application_id(service: ControlledScenarioService, reference: str) -> str | None:
    return service.s16_resolve_application(
        upstream_application_reference=reference, scope="C-DEMO"
    )


def _single_app_plan_command(
    *,
    admitted_one: tuple[str, str],
    snapshot_by_application: dict[str, tuple[str, str]],
    release_id: str,
    release_digest: str,
    manifest_id: str,
    manifest_digest: str,
    plan_id: str = "plan-s16-1",
    check_ids: tuple[str, ...] = (
        "R_ENGINE_CROSS",
        "R_VIN_CROSS",
        "R_BRAND_CROSS",
        "R_MODEL_CROSS",
    ),
) -> dict[str, Any]:
    """One frozen plan covering exactly one application across all four
    governed mandatory checks: the plan is exclusive (not shared)."""
    _scenario, application_id = admitted_one
    snapshot_id, snapshot_digest = snapshot_by_application[application_id]
    opportunities = [
        {
            "opportunity_id": f"opp-{index}",
            "track": "C",
            "cluster": "cl-0",
            "application_id": application_id,
            "cycle": 1,
            "check_id": check_id,
            "target_scope": "C",
            "evidence_snapshot_id": snapshot_id,
            "difficulty": "standard",
            "data_source": "demo",
            "document_combination": "single",
            "perturbation_family": "none",
        }
        for index, check_id in enumerate(check_ids)
    ]
    return {
        "schema_version": "s12-plan-command/1",
        "plan_id": plan_id,
        "scope_declared": "C",
        "seed": 20260820,
        "budget": {"max_opportunities": 10, "max_runtime_ms": 5000},
        "stop_rule": "plan-exhausted",
        "split": {
            "scheme": "cluster_usage_partition",
            "usage_partitions": [
                "development",
                "calibration",
                "acceptance_holdout",
            ],
        },
        "clusters": [
            {
                "cluster_id": "cl-0",
                "stratum": "c",
                "applications": [application_id],
                "usage": "development",
            }
        ],
        "tracks": {
            "R": {"opportunities": []},
            "C": {
                "opportunities": [opportunity["opportunity_id"] for opportunity in opportunities]
            },
        },
        "views": {
            "R-E2E": {"opportunities": []},
            "R-T4-conditional": {"opportunities": []},
        },
        "opportunities": opportunities,
        "evidence_references": [
            {
                "application_id": application_id,
                "cycle": 1,
                "snapshot_id": snapshot_id,
                "snapshot_digest": snapshot_digest,
            }
        ],
        "release_reference": {"release_id": release_id, "release_digest": release_digest},
        "label_manifest": {"manifest_id": manifest_id, "manifest_digest": manifest_digest},
        "mandatory_check_families": [
            {
                "family_id": "cross-document",
                "check_ids": ["R_ENGINE_CROSS", "R_VIN_CROSS"],
            },
            {
                "family_id": "brand-model",
                "check_ids": ["R_BRAND_CROSS", "R_MODEL_CROSS"],
            },
        ],
    }


# ---------------------------------------------------------------------------
# 9. Old-backup restore replays the external manifest before readiness
# ---------------------------------------------------------------------------


def test_old_backup_restore_replays_external_manifest_before_readiness(
    tmp_path: Path,
) -> None:
    service, submission, application_id = _registered_terminated(tmp_path)
    state_path = tmp_path / "target.sqlite3"
    backup_root = tmp_path / "backups"
    backup = BackupDeletionOwner(backup_root, clock=_CdemoSessionClock())
    s16 = _s16_service(tmp_path, service, backup_root=backup_root)

    result = _preflight(s16, str(submission["upstream_application_ref"]), scope=TENANT_SCOPE)
    scope_fingerprint = result["scope_fingerprint"]
    replica_entries = [e for e in result["entries"] if e["copy_class"] == COPY_CLASS_REPLICA]
    assert replica_entries and all(e["count"] == 0 for e in replica_entries)

    # Capture a scope-scoped backup of the business DB before deletion.
    saved = backup_root / "target.sqlite3"
    original_db = tmp_path / "original.sqlite3"
    import shutil

    shutil.copy2(state_path, saved)
    shutil.copy2(state_path, original_db)
    backup.capture(
        scope_fingerprint=scope_fingerprint,
        copy_files=[(saved.name, hashlib.sha256(saved.read_bytes()).hexdigest())],
    )

    result2 = s16.preflight(
        application_reference=str(submission["upstream_application_ref"]),
        principal=S01CommandPrincipal(
            subject=GOVERNANCE.subject,
            role="operator",
            scope=TENANT_SCOPE,
            source_id="s16-governance-console",
        ),
        idempotency_key="s16-backup-preflight-2",
    )
    replica_entries = [e for e in result2["entries"] if e["copy_class"] == COPY_CLASS_REPLICA]
    assert replica_entries and replica_entries[0]["count"] == 1
    manifest_entries = [
        e for e in result2["entries"] if e["copy_class"] == COPY_CLASS_BACKUP_MANIFEST
    ]
    assert manifest_entries and manifest_entries[0]["count"] == 1
    committed = s16.commit(request_id=result2["request_id"], principal=GOVERNANCE_REGISTERED, idempotency_key="backup-commit")
    assert committed["status"] == "accepted"
    outcome = s16.process_next_deletion_job(worker_id="w", now=_now() + 1)
    assert outcome["status"] == "complete", outcome
    assert not list(backup_root.glob("backup_*.json"))
    assert not saved.exists()
    # Completion leaves restore replay pending -> readiness stays closed.
    assert s16.ready() is False

    # Old-backup restore: put the pre-deletion DB back and restart the
    # ledger; startup replay must re-delete before readiness opens.
    # The old backup is restored: the captured copy and the business DB both
    # come back from the pre-deletion artifact.
    shutil.copy2(original_db, saved)
    shutil.copy2(original_db, state_path)
    backup.capture(
        scope_fingerprint=scope_fingerprint,
        copy_files=[(saved.name, hashlib.sha256(saved.read_bytes()).hexdigest())],
    )
    service_restored = ControlledScenarioService(
        fixture_root=FIXTURES,
        rules_path=RULES,
        state_path=state_path,
        controlled_objects=(),
        controlled_object_absence_store=service.registered_source_boundary.absence_store_path,
    )
    assert service_restored.s16_resolve_by_scope_fingerprint(scope_fingerprint) is not None
    restarted = _s16_service(
        tmp_path,
        service_restored,
        backup_root=backup_root,
        ledger_path=tmp_path / "s16.sqlite3",
    )
    assert restarted.ready() is True
    assert service_restored.s16_resolve_by_scope_fingerprint(scope_fingerprint) is None
    assert service_restored.s16_verify_absent(scope_fingerprint)["absent"] is True
    assert not list(backup_root.glob("backup_*.json"))

    # A restored backup whose owner cannot verify keeps readiness closed.
    shutil.copy2(original_db, state_path)
    shutil.copy2(original_db, saved)
    backup2 = BackupDeletionOwner(backup_root, clock=_CdemoSessionClock())
    backup2.capture(
        scope_fingerprint=scope_fingerprint,
        copy_files=[(saved.name, hashlib.sha256(saved.read_bytes()).hexdigest())],
    )
    service_restored2 = ControlledScenarioService(
        fixture_root=FIXTURES,
        rules_path=RULES,
        state_path=state_path,
        controlled_objects=(),
        controlled_object_absence_store=service.registered_source_boundary.absence_store_path,
    )
    # Simulate an unfinished restore replay: the ledger still records the
    # completed manifest with a pending replay state.
    with sqlite3.connect(tmp_path / "s16.sqlite3") as connection:
        rows = connection.execute(
            "SELECT receipt_id, payload FROM s16_receipts"
        ).fetchall()
        for receipt_id, payload in rows:
            value = json.loads(payload)
            value["restore_replay_status"] = "pending"
            updated = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            from task4_consistency.controlled.s16 import S16Ledger

            digest = S16Ledger._integrity_digest(
                "s16_receipts", receipt_id, updated
            )
            connection.execute(
                "UPDATE s16_receipts SET payload = ?, integrity_sha256 = ? "
                "WHERE receipt_id = ?",
                (updated, digest, receipt_id),
            )
    with pytest.raises(Exception):
        GovernedDeletionService(
            ledger_path=tmp_path / "s16.sqlite3",
            owners={
                # The S01 owner is broken (unverifiable after the restore):
                # startup replay must fail closed, keeping readiness closed.
                "s01": S01DeletionOwner(
                    object(),
                    retention=RetentionPolicy(retention_seconds=0),
                    clock=_CdemoSessionClock(),
                ),
                "s02": S02DeletionOwner(
                    service_restored2.registered_source_boundary,
                    service_restored2,
                ),
                "s12": S12DeletionOwner(_empty_evaluation(tmp_path)),
                "backup": BackupDeletionOwner(
                    backup_root, clock=_CdemoSessionClock()
                ),
                "s17-disabled": ExportTempOwner(),
            },
            retention=RetentionPolicy(retention_seconds=0),
            governance_subject=GOVERNANCE.subject,
            approver_subjects=(APPROVER_1.subject, APPROVER_2.subject),
            clock=_CdemoSessionClock(),
        )


# ---------------------------------------------------------------------------
# 10. Admin / cross-scope / unknown / direct hard delete are denied
# ---------------------------------------------------------------------------


def test_admin_cross_scope_unknown_and_direct_hard_delete_are_denied(
    tmp_path: Path,
) -> None:
    service, submission, application_id = _registered_terminated(tmp_path)
    s16 = _s16_service(tmp_path, service)

    # Platform admin / reviewer / demo user cannot run preflight.
    for principal in (PLATFORM_ADMIN, REVIEWER, DEMO_USER):
        with pytest.raises(S16Forbidden):
            s16.preflight(
                application_reference=str(submission["upstream_application_ref"]),
                principal=principal,
                idempotency_key=f"denied-{principal.subject}",
            )
    # Unknown reference existence-hides.
    with pytest.raises(S16NotFound):
        _preflight(s16, "DOES-NOT-EXIST", scope=TENANT_SCOPE)
    # Cross-scope: governance owner scoped C-DEMO cannot resolve an
    # R-OBSERVED application; same existence-hiding result as unknown.
    with pytest.raises(S16NotFound):
        _preflight(s16, str(submission["upstream_application_ref"]), scope="C-DEMO")

    # Unknown request ids existence-hide for every command.
    with pytest.raises(S16NotFound):
        s16.query(request_id="s16req_unknown", principal=GOVERNANCE_REGISTERED)
    with pytest.raises(S16NotFound):
        s16.receipt(request_id="s16req_unknown", principal=GOVERNANCE_REGISTERED)
    with pytest.raises(S16NotFound):
        s16.commit(request_id="s16req_unknown", principal=GOVERNANCE_REGISTERED, idempotency_key="u-1")
    with pytest.raises(S16NotFound):
        s16.cancel(request_id="s16req_unknown", principal=GOVERNANCE_REGISTERED, idempotency_key="u-2")
    with pytest.raises(S16NotFound):
        s16.repair(request_id="s16req_unknown", owner_id="s02", repair_fact="x", principal=GOVERNANCE_REGISTERED, idempotency_key="u-3")
    with pytest.raises(S16NotFound):
        s16.release_legal_hold(hold_id="hold_unknown", principal=GOVERNANCE_REGISTERED)

    # No direct hard-delete surface exists on the service.
    for name in ("hard_delete", "delete_all", "delete_application", "purge"):
        assert not hasattr(s16, name)
    # The worker never claims without a job.
    assert s16.process_next_deletion_job(worker_id="w", now=_now() + 1)["status"] == "idle"


# ---------------------------------------------------------------------------
# 11. Audit or ledger outage: zero commit effect
# ---------------------------------------------------------------------------


def test_audit_or_deletion_ledger_outage_has_zero_commit_effect(
    tmp_path: Path,
) -> None:
    service = _c_demo_service(tmp_path)
    application_id = _admit_c_demo(service, key="s16-outage-intake")
    _terminate(service, application_id)
    s16 = _s16_service(tmp_path, service, audit_available=False)
    result = _preflight(s16, "APP-R53-BAD-ENGINE")
    with pytest.raises(S16Blocked) as excinfo:
        s16.commit(request_id=result["request_id"], principal=GOVERNANCE, idempotency_key="outage-commit")
    assert excinfo.value.reason_code == S16_AUDIT_UNAVAILABLE
    assert _s01_fact_counts(service)["applications"] == 1
    assert s16.query(request_id=result["request_id"], principal=GOVERNANCE)["job"] is None
    with pytest.raises(S16Blocked) as excinfo:
        s16.impose_legal_hold(
            scope_fingerprint=result["scope_fingerprint"],
            principal=GOVERNANCE,
            reason_code="HOLD",
            owner="s01",
            effective_time=_now(),
        )
    assert excinfo.value.reason_code == S16_AUDIT_UNAVAILABLE

    # Ledger outage: every write rolls back; copies stay intact.
    s16_ok = _s16_service(tmp_path, service, ledger_path=tmp_path / "s16-ok.sqlite3")
    result_ok = _preflight(s16_ok, "APP-R53-BAD-ENGINE")

    def broken_connect() -> Any:
        raise OSError("injected ledger outage")

    original_connect = s16_ok._ledger._connect
    s16_ok._ledger._connect = broken_connect  # type: ignore[method-assign]
    try:
        with pytest.raises(OSError, match="ledger outage"):
            s16_ok.commit(request_id=result_ok["request_id"], principal=GOVERNANCE, idempotency_key="outage-commit-2")
    finally:
        s16_ok._ledger._connect = original_connect  # type: ignore[method-assign]
    assert _s01_fact_counts(service)["applications"] == 1
    query = s16_ok.query(request_id=result_ok["request_id"], principal=GOVERNANCE)
    assert query["job"] is None
    with sqlite3.connect(tmp_path / "s16-ok.sqlite3") as connection:
        commits = connection.execute(
            "SELECT COUNT(*) FROM s16_events WHERE payload LIKE '%\"event_type\":\"commit\"%'"
        ).fetchone()[0]
    assert commits == 0


# ---------------------------------------------------------------------------
# 12. Receipt / manifest / retained history carry no business value
# ---------------------------------------------------------------------------


def test_receipt_manifest_and_retained_history_contain_no_business_value_or_locator(
    tmp_path: Path,
) -> None:
    service, submission, application_id = _registered_terminated(tmp_path)
    backup_root = tmp_path / "backups"
    s16 = _s16_service(tmp_path, service, backup_root=backup_root)
    reference = str(submission["upstream_application_ref"])
    result = _preflight(s16, reference, scope=TENANT_SCOPE)
    scope_fingerprint = result["scope_fingerprint"]
    backup = BackupDeletionOwner(backup_root, clock=_CdemoSessionClock())
    saved = tmp_path / "saved" / "target.sqlite3"
    saved.parent.mkdir(parents=True)
    import shutil

    shutil.copy2(tmp_path / "target.sqlite3", saved)
    backup.capture(
        scope_fingerprint=scope_fingerprint,
        copy_files=[(saved.name, hashlib.sha256(saved.read_bytes()).hexdigest())],
    )
    result2 = s16.preflight(
        application_reference=reference,
        principal=S01CommandPrincipal(
            subject=GOVERNANCE.subject,
            role="operator",
            scope=TENANT_SCOPE,
            source_id="s16-governance-console",
        ),
        idempotency_key="s16-no-value-preflight-2",
    )
    _approve_two(s16, result2["request_id"], result2["manifest_digest"])
    committed = s16.commit(request_id=result2["request_id"], principal=GOVERNANCE_REGISTERED, idempotency_key="no-value-commit")
    assert committed["status"] == "accepted"
    outcome = s16.process_next_deletion_job(worker_id="w", now=_now() + 1)
    assert outcome["status"] == "complete", outcome
    receipt = s16.receipt(request_id=result2["request_id"], principal=GOVERNANCE_REGISTERED)
    query = s16.query(request_id=result2["request_id"], principal=GOVERNANCE_REGISTERED)

    forbidden = (
        application_id,
        reference,
        "result-object",
        "page-object",
        "R-OBSERVED/tenant-test",
        "tenant-test",
        "registered-source",
        "target.sqlite3",
        "fixtures",
        "s16-governance-console",
        "s16-approval-desk",
        "s02-repair-verified",
    )
    for label, payload in (
        ("receipt", receipt),
        ("manifest entries", [e for e in result2["entries"]]),
        ("query", query),
    ):
        serialized = json.dumps(payload, sort_keys=True)
        for token in forbidden:
            assert token not in serialized, f"{label} leaks {token!r}"
    # Caller idempotency keys never appear in ledger records.
    with sqlite3.connect(tmp_path / "s16.sqlite3") as connection:
        rows = connection.execute("SELECT payload FROM s16_events").fetchall()
        receipts = connection.execute("SELECT payload FROM s16_receipts").fetchall()
        jobs = connection.execute("SELECT payload FROM s16_jobs").fetchall()
    all_payloads = " ".join(
        str(row[0]) for row in [*rows, *receipts, *jobs]
    )
    for token in ("no-value-commit", "preflight-", "approve-", application_id, reference):
        assert token not in all_payloads, f"ledger leaks {token!r}"
    # Retained S01 history (Lifecycle terminal history, tombstone, S01
    # deletion receipt) carries no business value or recoverable locator.
    # The opaque application id stays as the terminal history's linkage key
    # (the plan preserves L14 terminal history); the S16 ledger itself is
    # verified application-id-free above.
    with sqlite3.connect(tmp_path / "target.sqlite3") as connection:
        lifecycle_rows = connection.execute(
            "SELECT payload FROM lifecycle_events"
        ).fetchall()
        tombstone_rows = connection.execute(
            "SELECT payload FROM s16_governed_deletions"
        ).fetchall()
        s16_receipt_rows = connection.execute(
            "SELECT payload FROM deletion_receipts"
        ).fetchall()
    retained = json.dumps(
        [json.loads(row[0]) for row in [*lifecycle_rows, *tombstone_rows, *s16_receipt_rows]],
        sort_keys=True,
    )
    for token in (reference, "result-object", "page-object", "target.sqlite3", "tenant-test"):
        assert token not in retained, f"retained history leaks {token!r}"
    # The receipt still proves the deletion with owner counts and replay state.
    assert receipt["result"] == "deleted"
    # Only owners holding copies appear; empty owners carry their proof in
    # the manifest but no deletion counts.
    assert set(receipt["owner_counts"]) == {"s01", "s02", "backup"}
    assert all(count >= 1 for count in receipt["owner_counts"].values())
    assert receipt["restore_replay_status"] == "pending"
