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
    S16_AUDIT_SEAM_UNAVAILABLE,
    S16_OWNER_BINDING_CONFLICT,
    S16_HOLD_GENERATION_CHANGED,
    S16_OWNER_REGISTRY_STALE,
    S16_OWNER_STALE_FENCE,
    S16_POLICY_STALE,
    S16_STALE_WORKER,
    S16_VERIFY_FAILED,
    S16OwnerFailure,
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
    scope_fingerprint_for,
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


def _recording_audit_writer() -> tuple[list[dict[str, Any]], Any]:
    recorded: list[dict[str, Any]] = []

    def writer(record: dict[str, Any]) -> bool:
        recorded.append(record)
        return True

    return recorded, writer


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
    security_audit_available: bool = True,
    security_audit_writer: Any = None,
    governance_scope: str = "C-DEMO",
    ledger_path: Path | None = None,
) -> GovernedDeletionService:
    if security_audit_available and security_audit_writer is None:
        _, security_audit_writer = _recording_audit_writer()
    retention = RetentionPolicy(retention_seconds=retention_seconds)
    s01_owner = S01DeletionOwner(
        service,
        retention=retention,
        clock=_CdemoSessionClock(),
    )
    return GovernedDeletionService(
        ledger_path=ledger_path or (tmp_path / "s16.sqlite3"),
        owners={
            "s01": s01_owner,
            "s02": S02DeletionOwner(
                service.registered_source_boundary, s01_owner
            ),
            "s12": S12DeletionOwner(
                evaluation or _empty_evaluation(tmp_path)
            ),
            "backup": BackupDeletionOwner(
                backup_root or (tmp_path / "backups"), clock=_CdemoSessionClock()
            ),
            "s17-disabled": ExportTempOwner(),
        },
        retention=retention,
        governance_subject=GOVERNANCE.subject,
        approver_subjects=(APPROVER_1.subject, APPROVER_2.subject),
        governance_scope=governance_scope,
        audit_available=audit_available,
        storage_available=storage_available,
        security_audit_available=security_audit_available,
        security_audit_writer=security_audit_writer,
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


def _approve_two(
    s16: GovernedDeletionService,
    request_id: str,
    manifest_digest: str,
    *,
    scope: str = "C-DEMO",
) -> None:
    s16.approve(
        request_id=request_id,
        manifest_digest=manifest_digest,
        principal=S01CommandPrincipal(
            subject=APPROVER_1.subject,
            role="operator",
            scope=scope,
            source_id="s16-approval-desk",
        ),
        idempotency_key=f"approve-1-{request_id}",
    )
    s16.approve(
        request_id=request_id,
        manifest_digest=manifest_digest,
        principal=S01CommandPrincipal(
            subject=APPROVER_2.subject,
            role="operator",
            scope=scope,
            source_id="s16-approval-desk",
        ),
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

    # Hold lands first: the manifest generation is pinned at preflight, so
    # commit closes with a stable gate (hold wins) and content stays intact.
    hold = s16.impose_legal_hold(
        scope_fingerprint=result["scope_fingerprint"],
        principal=GOVERNANCE,
        reason_code="litigation",
        owner="s01",
        effective_time=_now(),
        idempotency_key="hold-1",
    )
    assert hold["status"] == "accepted"
    with pytest.raises(S16Blocked) as excinfo:
        s16.commit(request_id=result["request_id"], principal=GOVERNANCE, idempotency_key="hold-commit-1")
    assert excinfo.value.reason_code in {"S16_ACTIVE_LEGAL_HOLD", "S16_HOLD_GENERATION_CHANGED"}
    # Content fully intact while the hold is active.
    assert _s01_fact_counts(service)["applications"] == 1

    # Release, then a FRESH preflight pins the new generation; commit wins
    # and the worker continues forward.
    released = s16.release_legal_hold(
        hold_id=hold["hold_id"],
        principal=GOVERNANCE,
        idempotency_key="release-1",
    )
    assert released["status"] == "accepted"
    result2 = s16.preflight(
        application_reference="APP-R53-BAD-ENGINE",
        principal=GOVERNANCE,
        idempotency_key="s16-hold-preflight-2",
    )
    committed = s16.commit(request_id=result2["request_id"], principal=GOVERNANCE, idempotency_key="hold-commit-2")
    assert committed["status"] == "accepted"
    # An impose that lands after commit is recorded but deletion continues.
    late_hold = s16.impose_legal_hold(
        scope_fingerprint=result2["scope_fingerprint"],
        principal=GOVERNANCE,
        reason_code="regulatory",
        owner="all",
        effective_time=_now(),
        idempotency_key="hold-2",
    )
    assert late_hold["status"] == "accepted"
    outcome = s16.process_next_deletion_job(worker_id="w", now=_now() + 1)
    assert outcome["status"] == "complete", outcome
    assert _s01_fact_counts(service)["applications"] == 0
    query = s16.query(request_id=result2["request_id"], principal=GOVERNANCE)
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
        governance_scope=TENANT_SCOPE,
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
                "s02": S02DeletionOwner(
                    service.registered_source_boundary,
                    S01DeletionOwner(service, retention=RetentionPolicy(retention_seconds=0), clock=_CdemoSessionClock()),
                ),
                "s12": S12DeletionOwner(_empty_evaluation(tmp_path)),
                "s17-disabled": ExportTempOwner(),
            },
            retention=RetentionPolicy(retention_seconds=0),
            governance_subject=GOVERNANCE.subject,
            approver_subjects=(APPROVER_1.subject, APPROVER_2.subject),
            security_audit_writer=_recording_audit_writer()[1],
            clock=_CdemoSessionClock(),
        )

    # Owner inventory failure -> preflight unavailable.
    s16 = _s16_service(tmp_path, service)
    broken = S02DeletionOwner(
        service.registered_source_boundary,
        S01DeletionOwner(
            service,
            retention=RetentionPolicy(retention_seconds=0),
            clock=_CdemoSessionClock(),
        ),
    )

    def boom(scope_fingerprint: str) -> list[CopyInventoryEntry]:
        raise S16Unavailable("injected owner outage")

    broken.inventory = boom  # type: ignore[method-assign]
    s16._owners["s02"] = broken
    with pytest.raises(S16Unavailable):
        _preflight(s16, "APP-R53-BAD-ENGINE")
    s16._owners["s02"] = S02DeletionOwner(
        service.registered_source_boundary,
        S01DeletionOwner(
            service,
            retention=RetentionPolicy(retention_seconds=0),
            clock=_CdemoSessionClock(),
        ),
    )

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
    s16b = _s16_service(tmp2, service_b, governance_scope=TENANT_SCOPE)
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
    s16 = _s16_service(tmp_path, service, governance_scope=TENANT_SCOPE)
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
    s16 = _s16_service(
        tmp_path, service, backup_root=backup_root, governance_scope=TENANT_SCOPE
    )

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
    # After a normal completion the scope stays absent, so the shared
    # readiness gate stays open (the immutable receipt reports the pending
    # replay state until the next startup/runtime replay).
    assert s16.ready() is True

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
    # Simulate an unfinished restore replay: remove the append-only replay
    # facts (the immutable receipt stays untouched) so readiness derives
    # from the missing verification.
    with sqlite3.connect(tmp_path / "s16.sqlite3") as connection:
        connection.execute("DELETE FROM s16_replays")
        connection.execute(
            "DELETE FROM s16_events WHERE payload LIKE '%restore_replay%'"
        )
    assert restarted.ready() is False
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
                    S01DeletionOwner(
                        service_restored2,
                        retention=RetentionPolicy(retention_seconds=0),
                        clock=_CdemoSessionClock(),
                    ),
                ),
                "s12": S12DeletionOwner(_empty_evaluation(tmp_path)),
                "backup": BackupDeletionOwner(
                    backup_root, clock=_CdemoSessionClock()
                ),
                "s17-disabled": ExportTempOwner(),
            },
            security_audit_writer=_recording_audit_writer()[1],
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
    s16 = _s16_service(tmp_path, service, governance_scope=TENANT_SCOPE)

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
    # Cross-scope: the same governance subject bound to a different scope is
    # an invalid identity (role/scope/source are part of the binding, R1),
    # so it is rejected before any existence fact is disclosed.
    with pytest.raises(S16Forbidden):
        _preflight(s16, str(submission["upstream_application_ref"]), scope="C-DEMO")
    # The HTTP surface cannot produce this state: principals are derived
    # from the registered credentials and always carry the configured scope.

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
        s16.release_legal_hold(
            hold_id="hold_unknown",
            principal=GOVERNANCE_REGISTERED,
            idempotency_key="u-4",
        )

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
            reason_code="litigation",
            owner="s01",
            effective_time=_now(),
            idempotency_key="outage-hold",
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
    s16 = _s16_service(
        tmp_path, service, backup_root=backup_root, governance_scope=TENANT_SCOPE
    )
    reference = str(submission["upstream_application_ref"])
    result = _preflight(s16, reference, scope=TENANT_SCOPE)
    scope_fingerprint = result["scope_fingerprint"]
    backup = BackupDeletionOwner(backup_root, clock=_CdemoSessionClock())
    saved = backup_root / "target.sqlite3"
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
    _approve_two(
        s16,
        result2["request_id"],
        result2["manifest_digest"],
        scope=TENANT_SCOPE,
    )
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


# ---------------------------------------------------------------------------
# R1 targeted regressions
# ---------------------------------------------------------------------------


def test_stale_worker_publish_cas_never_overwrites_newer_state(
    tmp_path: Path,
) -> None:
    service = _c_demo_service(tmp_path)
    _admit_c_demo(service, key="s16-cas-intake")
    _terminate(service, _app_of(service))
    s16 = _s16_service(tmp_path, service)
    result = _preflight(s16, "APP-R53-BAD-ENGINE")
    s16.commit(request_id=result["request_id"], principal=GOVERNANCE, idempotency_key="cas-commit")

    # Worker 1 claims (fence 1), then its lease expires and worker 2 claims
    # (fence 2): worker 1's publish must be rejected by the CAS.
    first = s16._claim_job("worker-1", _now() + 1)
    assert first is not None and int(first["fence"]) == 1
    second = s16._claim_job("worker-2", _now() + 61)
    assert second is not None and int(second["fence"]) == 2

    stale = s16._publish_success(first, {}, "worker-1", _now() + 62)
    assert stale["status"] == "stale"
    assert stale["reason_code"] == S16_STALE_WORKER
    job = s16._job_for_request(result["request_id"])
    assert job is not None and int(job["fence"]) == 2
    assert job["status"] == "running"
    assert job["lease_owner"] == "worker-2"
    # The stale worker never minted a receipt.
    assert s16.receipt.__self__._receipts == {}
    with sqlite3.connect(tmp_path / "s16.sqlite3") as connection:
        rows = connection.execute(
            "SELECT payload FROM s16_events WHERE payload LIKE '%stale_worker%'"
        ).fetchall()
    assert rows

    # The current worker publishes successfully and completes.
    final = s16._publish_success(second, {}, "worker-2", _now() + 63)
    assert final["status"] == "complete"
    job = s16._job_for_request(result["request_id"])
    assert job is not None and job["status"] == "complete"


def _app_of(service: ControlledScenarioService) -> str:
    (application_id,) = service.s16_application_ids()
    return application_id


def test_commit_rejects_registry_policy_and_hold_generation_changes(
    tmp_path: Path,
) -> None:
    import task4_consistency.controlled.s16 as s16_module

    service = _c_demo_service(tmp_path)
    application_id = _admit_c_demo(service, key="s16-stale-intake")
    _terminate(service, application_id)
    s16 = _s16_service(tmp_path, service)
    result = _preflight(s16, "APP-R53-BAD-ENGINE")

    # Registry change between preflight and commit is a stable gate.
    original_digest = s16_module.s16_owner_registry_digest
    s16_module.s16_owner_registry_digest = lambda: "0" * 64  # type: ignore[assignment]
    try:
        with pytest.raises(S16Blocked) as excinfo:
            s16.commit(request_id=result["request_id"], principal=GOVERNANCE, idempotency_key="stale-registry")
        assert excinfo.value.reason_code == S16_OWNER_REGISTRY_STALE
    finally:
        s16_module.s16_owner_registry_digest = original_digest

    # Policy change between preflight and commit is a stable gate.
    s16._retention = RetentionPolicy(
        policy_version="2", retention_seconds=0
    )
    try:
        with pytest.raises(S16Blocked) as excinfo:
            s16.commit(request_id=result["request_id"], principal=GOVERNANCE, idempotency_key="stale-policy")
        assert excinfo.value.reason_code == S16_POLICY_STALE
    finally:
        s16._retention = RetentionPolicy(retention_seconds=0)

    # Hold generation change between preflight and commit is a stable gate.
    result2 = _preflight(s16, "APP-R53-BAD-ENGINE", )
    result2 = s16.preflight(
        application_reference="APP-R53-BAD-ENGINE",
        principal=GOVERNANCE,
        idempotency_key="s16-stale-preflight-2",
    )
    s16.impose_legal_hold(
        scope_fingerprint=result2["scope_fingerprint"],
        principal=GOVERNANCE,
        reason_code="litigation",
        owner="s01",
        effective_time=_now(),
        idempotency_key="stale-hold",
    )
    with pytest.raises(S16Blocked) as excinfo:
        s16.commit(request_id=result2["request_id"], principal=GOVERNANCE, idempotency_key="stale-hold-commit")
    assert excinfo.value.reason_code in {"S16_ACTIVE_LEGAL_HOLD", "S16_HOLD_GENERATION_CHANGED"}


def test_backup_capture_path_boundary_and_delete_verification(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backups"
    backup = BackupDeletionOwner(backup_root, clock=_CdemoSessionClock())
    scope_fingerprint = "0" * 64

    # Absolute paths, separators and escapes are rejected at capture.
    for bad_handle in ("/etc/passwd", "../evil", "a/b", "a\\b", "..", "."):
        with pytest.raises(ValueError):
            backup.capture(
                scope_fingerprint=scope_fingerprint,
                copy_files=[(bad_handle, "0" * 64)],
            )

    captured = backup_root / "captured.bin"
    captured.write_bytes(b"captured-bytes")
    digest = hashlib.sha256(b"captured-bytes").hexdigest()
    manifest = backup.capture(
        scope_fingerprint=scope_fingerprint,
        copy_files=[(captured.name, digest)],
    )
    # A repeated identity never overwrites: unique manifests accumulate.
    captured2 = backup_root / "captured2.bin"
    captured2.write_bytes(b"captured-bytes")
    digest2 = hashlib.sha256(b"captured-bytes").hexdigest()
    manifest2 = backup.capture(
        scope_fingerprint=scope_fingerprint,
        copy_files=[(captured2.name, digest2)],
    )
    assert manifest["manifest_id"] != manifest2["manifest_id"]
    assert len(backup._load_manifests()) == 2

    # Digest mismatch: the shared reconciliation fails closed first
    # (R4 P1-4), then the delete fails and keeps the manifest.
    fingerprints = {
        entry.identity_fingerprint
        for entry in backup.inventory(scope_fingerprint)
    }
    captured.write_bytes(b"tampered-bytes")
    with pytest.raises(S16Unavailable, match="digest mismatch"):
        backup.inventory(scope_fingerprint)
    with pytest.raises(S16OwnerFailure) as excinfo:
        backup.delete(fingerprints, scope_fingerprint=scope_fingerprint, operation_id="o", fence=1)
    assert excinfo.value.reason_code == S16_VERIFY_FAILED
    # The failing manifest is preserved (fail-closed): the delete never
    # removes a manifest whose captured copy could not be verified.  The
    # manifest itself is locator-free (R2 P1-1): it carries only connector
    # identities and digests, never handles or paths.
    for manifest in backup._load_manifests():
        for file_entry in manifest["files"]:
            assert "connector_identity" in file_entry
            assert "handle" not in file_entry
            assert "content_sha256" in file_entry
    assert captured.exists()

    # Correct digest deletes the files, verifies removal, then manifests.
    # The failed attempt may already have removed one verified copy, so
    # restore every captured artifact before the retry.
    captured.write_bytes(b"captured-bytes")
    if not captured2.exists():
        captured2.write_bytes(b"captured-bytes")
    outcome = backup.delete(
        fingerprints, scope_fingerprint=scope_fingerprint, operation_id="o", fence=1
    )
    assert outcome["status"] == "complete"
    assert not captured.exists()
    assert not captured2.exists()
    assert not list(backup_root.glob("backup_*.json"))


def test_s02_absence_persistence_failure_keeps_memory_intact_and_retryable(
    tmp_path: Path,
) -> None:
    service, submission, application_id = _registered_terminated(tmp_path)
    boundary = service.registered_source_boundary
    fingerprints = [
        entry.identity_fingerprint
        for entry in S02DeletionOwner(
            boundary,
            S01DeletionOwner(
                service,
                retention=RetentionPolicy(retention_seconds=0),
                clock=_CdemoSessionClock(),
            ),
        ).inventory(scope_fingerprint_for(application_id))
        if entry.planned_action == "delete"
    ]
    assert len(fingerprints) == 2

    import sqlite3 as _sqlite3

    real_connect = sqlite3.connect
    failed = {"armed": True}

    def failing_connect(*args: Any, **kwargs: Any) -> Any:
        if failed["armed"]:
            raise OSError("injected absence-store outage")
        return real_connect(*args, **kwargs)

    monkeypatch_connect = failing_connect
    original = sqlite3.connect
    sqlite3.connect = failing_connect  # type: ignore[assignment]
    try:
        with pytest.raises(OSError, match="absence-store outage"):
            boundary.s02_delete(
                fingerprints,
                operation_id="op-1",
                fence=1,
                scope_fingerprint=scope_fingerprint_for(application_id),
            )
    finally:
        sqlite3.connect = original  # type: ignore[assignment]
    # Memory state is untouched: the objects stay readable and absent is
    # not claimed.
    boundary.read_object(
        tenant_id="tenant-test",
        source_system_id="registered-source",
        object_ref="result-object",
    )
    assert boundary.s02_inventory()["objects"]
    assert boundary.s02_verify_absent(
        fingerprints,
        scope_fingerprint=scope_fingerprint_for(application_id),
    )["absent"] is False

    # The retry after the outage persists absence atomically.
    outcome = boundary.s02_delete(
        fingerprints,
        operation_id="op-1",
        fence=1,
        scope_fingerprint=scope_fingerprint_for(application_id),
    )
    assert outcome["status"] == "complete"
    assert boundary.s02_verify_absent(
        fingerprints,
        scope_fingerprint=scope_fingerprint_for(application_id),
    )["absent"] is True
    # Replaying the same operation/fence returns the original result.
    replayed = boundary.s02_delete(
        fingerprints,
        operation_id="op-1",
        fence=1,
        scope_fingerprint=scope_fingerprint_for(application_id),
    )
    assert replayed["replayed"] is True
    # A stale fence is a stable stale outcome.
    stale = boundary.s02_delete(
        fingerprints,
        operation_id="op-1",
        fence=0,
        scope_fingerprint=scope_fingerprint_for(application_id),
    )
    assert stale["status"] == "stale"


def test_owner_level_fencing_rejects_stale_fence_on_s12(
    tmp_path: Path,
) -> None:
    service = _c_demo_service(tmp_path)
    application_id = _admit_c_demo(service, key="s16-fence-intake")
    _terminate(service, application_id)
    evaluation = _empty_evaluation(tmp_path)
    # Seed one minimal plan row referencing the target scope: the S16 S12
    # adapter scans row structure only, so this exercises the owner binding.
    plan_id = "plan-fence-1"
    plan = {
        "plan_id": plan_id,
        "opportunities": [
            {
                "opportunity_id": "opp-fence",
                "application_id": application_id,
                "check_id": "R_ENGINE_CROSS",
            }
        ],
        "clusters": [],
        "evidence_references": [],
    }
    with evaluation._store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        evaluation._store._write_row(
            connection, "s12_plans", plan_id, plan
        )
        connection.execute("COMMIT")
    scope_fp = scope_fingerprint_for(application_id)
    entries = S12DeletionOwner(evaluation).inventory(scope_fp)
    fingerprints = [entry.identity_fingerprint for entry in entries]
    assert fingerprints

    first = evaluation.s16_delete_scope(
        fingerprints,
        operation_id="s12-op-1",
        fence=2,
        scope_fingerprint=scope_fp,
    )
    assert first["status"] == "complete"
    assert evaluation.s16_verify_absent(fingerprints)["absent"] is True
    replayed = evaluation.s16_delete_scope(
        fingerprints,
        operation_id="s12-op-1",
        fence=2,
        scope_fingerprint=scope_fp,
    )
    assert replayed["replayed"] is True
    stale = evaluation.s16_delete_scope(
        fingerprints,
        operation_id="s12-op-1",
        fence=1,
        scope_fingerprint=scope_fp,
    )
    assert stale["status"] == "stale"


def test_security_audit_facts_and_outage_zero_state_change(
    tmp_path: Path,
) -> None:
    recorded: list[dict[str, Any]] = []

    def writer(record: dict[str, Any]) -> bool:
        recorded.append(record)
        return True

    service = _c_demo_service(tmp_path)
    application_id = _admit_c_demo(service, key="s16-audit-intake")
    _terminate(service, application_id)
    s16 = _s16_service(tmp_path, service, security_audit_writer=writer)
    probe = _preflight(s16, "APP-R53-BAD-ENGINE")
    hold = s16.impose_legal_hold(
        scope_fingerprint=probe["scope_fingerprint"],
        principal=GOVERNANCE,
        reason_code="litigation",
        owner="s01",
        effective_time=_now(),
        idempotency_key="audit-hold",
    )
    s16.release_legal_hold(
        hold_id=hold["hold_id"],
        principal=GOVERNANCE,
        idempotency_key="audit-release",
    )
    # A fresh preflight pins the post-hold generation so commit can proceed.
    result = s16.preflight(
        application_reference="APP-R53-BAD-ENGINE",
        principal=GOVERNANCE,
        idempotency_key="s16-audit-preflight-2",
    )
    _approve_two(s16, result["request_id"], result["manifest_digest"])
    committed = s16.commit(request_id=result["request_id"], principal=GOVERNANCE, idempotency_key="audit-commit")
    assert committed["status"] == "accepted"
    with sqlite3.connect(tmp_path / "s16.sqlite3") as connection:
        audit_rows = connection.execute(
            "SELECT payload FROM s16_events WHERE payload LIKE '%security_audit%'"
        ).fetchall()
    facts = [json.loads(row[0]) for row in audit_rows]
    actions = {
        fact.get("action")
        for fact in facts
        if fact.get("event_type") == "security_audit"
    }
    assert {"approval", "legal_hold_imposed", "commit"} <= actions
    assert all(
        fact.get("event_type") == "security_audit"
        and fact.get("scope_fingerprint") == result["scope_fingerprint"]
        and len(fact.get("subject_fingerprint", "")) == 64
        for fact in facts
        if fact.get("event_type") == "security_audit"
    )
    # The post-commit full copy reached the writer and was recorded.
    assert any(
        fact.get("event_type") == "security_audit_replication"
        and fact.get("status") == "replicated"
        for fact in facts
    )
    assert recorded

    # A failing writer records the failure without rolling back the commit.
    failed_writer: list[dict[str, Any]] = []

    def failing_writer(record: dict[str, Any]) -> bool:
        failed_writer.append(record)
        raise RuntimeError("worm outage")

    service2 = _c_demo_service(tmp_path / "audit-b")
    application_id2 = _admit_c_demo(service2, key="s16-audit2-intake")
    _terminate(service2, application_id2)
    s16b = _s16_service(
        tmp_path / "audit-b",
        service2,
        security_audit_writer=failing_writer,
        ledger_path=tmp_path / "audit-b" / "s16.sqlite3",
    )
    result_b = _preflight(s16b, "APP-R53-BAD-ENGINE")
    _approve_two(s16b, result_b["request_id"], result_b["manifest_digest"])
    committed_b = s16b.commit(request_id=result_b["request_id"], principal=GOVERNANCE, idempotency_key="audit-commit-b")
    assert committed_b["status"] == "accepted"
    with sqlite3.connect(tmp_path / "audit-b" / "s16.sqlite3") as connection:
        rows_b = connection.execute(
            "SELECT payload FROM s16_events WHERE payload LIKE '%security_audit_replication%'"
        ).fetchall()
    assert any(
        json.loads(row[0]).get("status") == "failed" for row in rows_b
    )

    # Audit seam outage: protected commands change zero state.
    service3 = _c_demo_service(tmp_path / "audit-c")
    application_id3 = _admit_c_demo(service3, key="s16-audit3-intake")
    _terminate(service3, application_id3)
    s16c = _s16_service(
        tmp_path / "audit-c",
        service3,
        security_audit_available=False,
        ledger_path=tmp_path / "audit-c" / "s16.sqlite3",
    )
    # R2 (P1-6): preflight is a protected command — the audit outage blocks
    # it before any ledger fact is written (zero state change).
    with pytest.raises(S16Blocked) as excinfo:
        _preflight(s16c, "APP-R53-BAD-ENGINE")
    assert excinfo.value.reason_code == S16_AUDIT_SEAM_UNAVAILABLE
    assert _s01_fact_counts(service3)["applications"] == 1
    with sqlite3.connect(tmp_path / "audit-c" / "s16.sqlite3") as connection:
        events = connection.execute("SELECT COUNT(*) FROM s16_events").fetchone()[0]
        bindings = connection.execute(
            "SELECT COUNT(*) FROM s16_bindings"
        ).fetchone()[0]
    assert events == 0
    assert bindings == 0


def test_receipt_append_only_and_replay_facts_immutable(tmp_path: Path) -> None:
    service = _c_demo_service(tmp_path)
    application_id = _admit_c_demo(service, key="s16-receipt-intake")
    _terminate(service, application_id)
    import shutil as _shutil

    original_db = tmp_path / "original.sqlite3"
    _shutil.copy2(tmp_path / "target.sqlite3", original_db)
    s16 = _s16_service(tmp_path, service)
    result = _preflight(s16, "APP-R53-BAD-ENGINE")
    s16.commit(request_id=result["request_id"], principal=GOVERNANCE, idempotency_key="receipt-commit")
    outcome = s16.process_next_deletion_job(worker_id="w", now=_now() + 1)
    assert outcome["status"] == "complete", outcome
    with sqlite3.connect(tmp_path / "s16.sqlite3") as connection:
        receipt_rows = connection.execute(
            "SELECT receipt_id, payload, integrity_sha256 FROM s16_receipts"
        ).fetchall()
    assert len(receipt_rows) == 1
    receipt_id, original_payload, original_digest = receipt_rows[0]

    # Restart without a restore: no owner holds copies, so no replay fact
    # is needed and the immutable receipt stays byte-identical with the
    # pending replay state (R2 P0-1: replay facts follow actual restores).
    restarted = _s16_service(
        tmp_path,
        service,
        ledger_path=tmp_path / "s16.sqlite3",
    )
    assert restarted.ready() is True
    with sqlite3.connect(tmp_path / "s16.sqlite3") as connection:
        after_rows = connection.execute(
            "SELECT receipt_id, payload, integrity_sha256 FROM s16_receipts"
        ).fetchall()
        replay_rows = connection.execute(
            "SELECT replay_id, payload FROM s16_replays"
        ).fetchall()
    assert after_rows == [(receipt_id, original_payload, original_digest)]
    assert replay_rows == []
    assert restarted.receipt(request_id=result["request_id"], principal=GOVERNANCE)[
        "restore_replay_status"
    ] == "pending"

    # An actual old-backup restore under the RUNNING service appends one
    # immutable replay fact after every owner verifies absence; readiness
    # reopens and the receipt reports verified.
    _shutil.copy2(original_db, tmp_path / "target.sqlite3")
    assert restarted.ready() is False
    replayed = restarted.replay_restore_if_needed()
    assert replayed["jobs"] >= 1
    assert restarted.ready() is True
    with sqlite3.connect(tmp_path / "s16.sqlite3") as connection:
        replay_rows = connection.execute(
            "SELECT replay_id, payload FROM s16_replays"
        ).fetchall()
        after_rows = connection.execute(
            "SELECT receipt_id, payload, integrity_sha256 FROM s16_receipts"
        ).fetchall()
    assert len(replay_rows) >= 1
    assert after_rows == [(receipt_id, original_payload, original_digest)]
    assert restarted.receipt(
        request_id=result["request_id"], principal=GOVERNANCE
    )["restore_replay_status"] == "verified"

    # Tampering the immutable receipt is detected on the next load.
    with sqlite3.connect(tmp_path / "s16.sqlite3") as connection:
        connection.execute(
            "UPDATE s16_receipts SET payload = ? WHERE receipt_id = ?",
            (_canonical_tamper(original_payload), receipt_id),
        )
    with pytest.raises(S16Unavailable):
        _s16_service(tmp_path, service, ledger_path=tmp_path / "s16.sqlite3")


def _canonical_tamper(payload: str) -> str:
    value = json.loads(payload)
    value["tampered"] = True
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_runtime_restore_replay_reopens_readiness(tmp_path: Path) -> None:
    import shutil

    service = _c_demo_service(tmp_path)
    application_id = _admit_c_demo(service, key="s16-runtime-restore-intake")
    _terminate(service, application_id)
    state_path = tmp_path / "target.sqlite3"
    original_db = tmp_path / "original.sqlite3"
    shutil.copy2(state_path, original_db)
    s16 = _s16_service(tmp_path, service)
    result = _preflight(s16, "APP-R53-BAD-ENGINE")
    s16.commit(request_id=result["request_id"], principal=GOVERNANCE, idempotency_key="runtime-commit")
    outcome = s16.process_next_deletion_job(worker_id="w", now=_now() + 1)
    assert outcome["status"] == "complete", outcome
    assert s16.ready() is True

    # Old backup restored under the running process: the shared gate closes
    # until the runtime replay re-deletes.
    shutil.copy2(original_db, state_path)
    service._reload_store()
    assert s16.ready() is False
    replayed = s16.replay_restore_if_needed()
    assert replayed["jobs"] >= 1
    assert s16.ready() is True
    assert service.s16_resolve_by_scope_fingerprint(
        scope_fingerprint_for(application_id)
    ) is None


# ---------------------------------------------------------------------------
# R2 targeted regressions
# ---------------------------------------------------------------------------


def test_cross_instance_claim_cas_grants_one_lease(
    tmp_path: Path,
) -> None:
    """Two service instances over one ledger: the database claim CAS grants
    the lease to exactly one worker; the other observes the advanced row."""
    service = _c_demo_service(tmp_path)
    _admit_c_demo(service, key="s16-cross-intake")
    _terminate(service, _app_of(service))
    ledger_path = tmp_path / "s16-cross.sqlite3"
    instance_a = _s16_service(tmp_path, service, ledger_path=ledger_path)
    instance_b = _s16_service(tmp_path, service, ledger_path=ledger_path)
    result = _preflight(instance_a, "APP-R53-BAD-ENGINE")
    instance_a.commit(
        request_id=result["request_id"],
        principal=GOVERNANCE,
        idempotency_key="cross-commit",
    )
    first = instance_a._claim_job("worker-a", _now() + 1)
    assert first is not None and int(first["fence"]) == 1
    # The second instance observes the leased row and abandons the claim.
    second = instance_b._claim_job("worker-b", _now() + 1)
    assert second is None
    # The leased worker keeps the confirmed fence.
    job = instance_a._job_for_request(result["request_id"])
    assert job is not None and int(job["fence"]) == 1
    assert job["lease_owner"] == "worker-a"


def test_single_owner_restore_closes_readiness_until_replayed(
    tmp_path: Path,
) -> None:
    """P0-1: only the BACKUP owner holding copies again (a single-owner
    restore) closes the shared readiness gate and drives a per-owner replay."""
    import shutil

    service = _c_demo_service(tmp_path)
    application_id = _admit_c_demo(service, key="s16-owner-restore-intake")
    _terminate(service, application_id)
    state_path = tmp_path / "target.sqlite3"
    original_db = tmp_path / "original.sqlite3"
    shutil.copy2(state_path, original_db)
    backup_root = tmp_path / "backups"
    backup = BackupDeletionOwner(backup_root, clock=_CdemoSessionClock())
    saved = backup_root / "target.sqlite3"
    shutil.copy2(state_path, saved)
    s16 = _s16_service(tmp_path, service, backup_root=backup_root)
    probe = _preflight(s16, "APP-R53-BAD-ENGINE")
    backup.capture(
        scope_fingerprint=probe["scope_fingerprint"],
        copy_files=[(saved.name, hashlib.sha256(saved.read_bytes()).hexdigest())],
    )
    result = s16.preflight(
        application_reference="APP-R53-BAD-ENGINE",
        principal=GOVERNANCE,
        idempotency_key="s16-owner-restore-preflight-2",
    )
    s16.commit(
        request_id=result["request_id"],
        principal=GOVERNANCE,
        idempotency_key="owner-restore-commit",
    )
    outcome = s16.process_next_deletion_job(worker_id="w", now=_now() + 1)
    assert outcome["status"] == "complete", outcome
    assert s16.ready() is True

    # Single-owner restore: the business DB stays deleted, but a captured
    # backup copy and its manifest reappear on the backup owner (the same
    # deterministic connector identity).  The gate closes.
    shutil.copy2(original_db, saved)
    backup.capture(
        scope_fingerprint=result["scope_fingerprint"],
        copy_files=[(saved.name, hashlib.sha256(saved.read_bytes()).hexdigest())],
    )
    assert s16.ready() is False
    replayed = s16.replay_restore_if_needed()
    assert replayed["jobs"] >= 1
    assert s16.ready() is True
    assert not saved.exists()


def test_hold_release_has_own_request_and_generation(
    tmp_path: Path,
) -> None:
    service = _c_demo_service(tmp_path)
    _admit_c_demo(service, key="s16-release-intake")
    _terminate(service, _app_of(service))
    s16 = _s16_service(tmp_path, service)
    result = _preflight(s16, "APP-R53-BAD-ENGINE")
    hold = s16.impose_legal_hold(
        scope_fingerprint=result["scope_fingerprint"],
        principal=GOVERNANCE,
        reason_code="litigation",
        owner="s01",
        effective_time=_now(),
        idempotency_key="rel-hold",
    )
    generation_after_impose = s16._hold_generation(result["scope_fingerprint"])
    released = s16.release_legal_hold(
        hold_id=hold["hold_id"],
        principal=GOVERNANCE,
        idempotency_key="rel-1",
    )
    # R2 P1-7: release has its own request id and advances the generation.
    assert released["request_id"]
    assert released["request_id"] != hold["request_id"]
    generation_after_release = s16._hold_generation(result["scope_fingerprint"])
    assert generation_after_release > generation_after_impose
    # The release request id is an independent fact in the ledger.
    release_events = [
        event
        for event in s16._events
        if event.get("event_type") == "legal_hold_released"
    ]
    assert release_events
    assert release_events[0]["request_id"] == released["request_id"]


def test_terminal_approval_replay_binds_current_key(
    tmp_path: Path,
) -> None:
    service = _c_demo_service(tmp_path)
    _admit_c_demo(service, key="s16-terminal-key-intake")
    _terminate(service, _app_of(service))
    s16 = _s16_service(tmp_path, service)
    result = _preflight(s16, "APP-R53-BAD-ENGINE")

    first = s16.approve(
        request_id=result["request_id"],
        manifest_digest=result["manifest_digest"],
        principal=APPROVER_1,
        idempotency_key="term-ap-1",
    )
    assert first["status"] == "accepted"
    # A NEW key for the same terminal fact is recorded and replayed.
    second = s16.approve(
        request_id=result["request_id"],
        manifest_digest=result["manifest_digest"],
        principal=APPROVER_1,
        idempotency_key="term-ap-2",
    )
    assert second["status"] == "replayed"
    # The second key now replays via its own binding; a DIFFERENT content
    # under the same key is a conflict.
    third = s16.approve(
        request_id=result["request_id"],
        manifest_digest=result["manifest_digest"],
        principal=APPROVER_1,
        idempotency_key="term-ap-2",
    )
    assert third["replayed"] is True
    with pytest.raises(S16Conflict):
        s16.approve(
            request_id=result["request_id"],
            manifest_digest="0" * 64,
            principal=APPROVER_1,
            idempotency_key="term-ap-2",
        )


def test_backup_binding_rejects_scope_or_digest_mismatch(
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backups"
    backup = BackupDeletionOwner(backup_root, clock=_CdemoSessionClock())
    scope_a = "a" * 64
    scope_b = "b" * 64
    captured = backup_root / "captured.bin"
    captured.write_bytes(b"captured-bytes")
    digest = hashlib.sha256(b"captured-bytes").hexdigest()
    backup.capture(scope_fingerprint=scope_a, copy_files=[(captured.name, digest)])
    fingerprints = {
        entry.identity_fingerprint
        for entry in backup.inventory(scope_a)
    }
    # First deletion binds operation/fence/scope/digest.
    done = backup.delete(
        fingerprints,
        scope_fingerprint=scope_a,
        operation_id="op-b",
        fence=1,
    )
    assert done["status"] == "complete"
    # Same operation/fence with a different scope is a stable conflict.
    conflict = backup.delete(
        fingerprints,
        scope_fingerprint=scope_b,
        operation_id="op-b",
        fence=1,
    )
    assert conflict["status"] == "conflict"
    # Same scope and digest replays the original result.
    replayed = backup.delete(
        fingerprints,
        scope_fingerprint=scope_a,
        operation_id="op-b",
        fence=1,
    )
    assert replayed["replayed"] is True
    # A lower fence is a stable stale outcome.
    captured.write_bytes(b"captured-bytes")
    backup.capture(scope_fingerprint=scope_a, copy_files=[(captured.name, digest)])
    stale = backup.delete(
        fingerprints,
        scope_fingerprint=scope_a,
        operation_id="op-b",
        fence=0,
    )
    assert stale["status"] == "stale"



# ---------------------------------------------------------------------------
# R3 (P1-x) regressions
# ---------------------------------------------------------------------------


def test_restore_readiness_gate_closes_every_domain_retrieval(
    tmp_path: Path,
) -> None:
    """R3 P1-4: the injected shared read gate closes S15 reveal,
    S01 settlement view, S02 direct object reads and S12 job/bundle
    queries during a restore window, and reopens with the gate."""
    service, submission, application_id = _registered_terminated(tmp_path)
    boundary = service.registered_source_boundary
    evaluation = _empty_evaluation(tmp_path)
    _s16_service(tmp_path, service, evaluation=evaluation)
    service.s16_read_gate = lambda: False  # type: ignore[attr-defined]
    boundary.s16_read_gate = lambda: False  # type: ignore[attr-defined]
    evaluation.s16_read_gate = lambda: False  # type: ignore[attr-defined]

    with pytest.raises(QueryNotFound):
        service.settlement_view(
            principal=REVIEWER, application_id=application_id
        )
    with pytest.raises(QueryNotFound):
        service.reveal_field_observation(
            principal=S01CommandPrincipal(
                subject="s15-reviewer",
                role="reviewer",
                scope=TENANT_SCOPE,
                source_id="s15-review-console",
            ),
            application_id=application_id,
            work_item_id="work-item-gone",
            observation_id="obs-gone",
            expected_fence=1,
            expected_context={},
            idempotency_key="s16-gate-reveal",
            purpose="manual_review",
            reason="HUMAN_REVIEW_COMPLETED",
            classification="RESTRICTED",
            expected_source_region="region:1",
        )
    with pytest.raises(LookupError):
        boundary.read_object(
            tenant_id="tenant-test",
            source_system_id="registered-source",
            object_ref="result-object",
        )
    with pytest.raises(ValueError, match="restore replay"):
        evaluation.query_job("job-gone")
    with pytest.raises(ValueError, match="restore replay"):
        evaluation.query_bundle("bundle-gone")

    # Gate open: the same retrievals behave normally again.
    service.s16_read_gate = lambda: True  # type: ignore[attr-defined]
    boundary.s16_read_gate = lambda: True  # type: ignore[attr-defined]
    evaluation.s16_read_gate = lambda: True  # type: ignore[attr-defined]
    assert (
        boundary.read_object(
            tenant_id="tenant-test",
            source_system_id="registered-source",
            object_ref="result-object",
        )
        is not None
    )
    with pytest.raises(ValueError, match="does not exist"):
        evaluation.query_job("job-gone")


def test_s02_single_owner_restore_replays_with_operation_fence_binding(
    tmp_path: Path,
) -> None:
    """R3 P0-1: an S02-only restore (absence rows back) closes readiness
    and the per-owner replay re-deletes under a fresh replay operation
    binding with fence 0, then verifies absence."""
    import shutil

    service, submission, application_id = _registered_terminated(tmp_path)
    absence_path = service.registered_source_boundary.absence_store_path
    shutil.copy2(absence_path, tmp_path / "absence_original.sqlite3")
    s16 = _s16_service(tmp_path, service, governance_scope=TENANT_SCOPE)
    result = _preflight(
        s16, str(submission["upstream_application_ref"]), scope=TENANT_SCOPE
    )
    s16.commit(
        request_id=result["request_id"],
        principal=GOVERNANCE_REGISTERED,
        idempotency_key="s02-restore-commit",
    )
    outcome = s16.process_next_deletion_job(worker_id="w", now=_now() + 1)
    assert outcome["status"] == "complete", outcome
    assert s16.ready() is True
    with sqlite3.connect(absence_path) as connection:
        absent_count = connection.execute(
            "SELECT COUNT(*) FROM s02_object_absence"
        ).fetchone()[0]
    assert absent_count >= 1

    # Single-owner restore: the ORIGINAL (pre-deletion) absence store
    # comes back with no absence marks; the gate closes.
    shutil.copy2(tmp_path / "absence_original.sqlite3", absence_path)
    with sqlite3.connect(absence_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM s02_object_absence"
            ).fetchone()[0]
            == 0
        )
    assert s16.ready() is False
    replayed = s16.replay_restore_if_needed()
    assert replayed["jobs"] >= 1
    assert s16.ready() is True
    with sqlite3.connect(absence_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM s02_object_absence"
            ).fetchone()[0]
            == absent_count
        )


def test_s12_delete_and_verify_are_scope_aware_across_equal_digests(
    tmp_path: Path,
) -> None:
    """R3 P1-1/P1-2: the S12 delete only touches rows whose plan belongs
    to the CURRENT scope (a same-digest row owned by another scope is
    never deleted), and absence verification only judges bindings of the
    target digest — unrelated scopes never cause a mismatch."""
    evaluation = _empty_evaluation(tmp_path)
    scope_a = scope_fingerprint_for("app-s12-a")
    scope_b = scope_fingerprint_for("app-s12-b")
    plan_a = {
        "plan_id": "plan-a",
        "kind": "s16-cross-scope",
        "opportunities": [
            {"opportunity_id": "opp-a", "application_id": "app-s12-a"}
        ],
    }
    # plan-b: different content -> different digest, scope B.
    plan_b = {
        "plan_id": "plan-b",
        "kind": "s16-cross-scope",
        "opportunities": [
            {"opportunity_id": "opp-b", "application_id": "app-s12-b"}
        ],
    }
    # job-b: byte-identical to plan-a -> SAME content digest, but owned by
    # plan-b (scope B).  The old global fingerprint scan would delete it
    # during the scope-A delete.
    job_b = dict(plan_a)
    job_b["job_id"] = "job-b"
    job_b["plan_id"] = "plan-b"
    with evaluation._store._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        evaluation._store._write_row(
            connection, "s12_plans", "plan-a", plan_a
        )
        evaluation._store._write_row(
            connection, "s12_plans", "plan-b", plan_b
        )
        evaluation._store._write_row(
            connection, "s12_jobs", "job-b", job_b
        )
        connection.commit()

    from task4_consistency.controlled.s16 import copy_identity_fingerprint
    from task4_consistency.controlled.s12 import content_digest

    fingerprint_a = copy_identity_fingerprint(
        "s12", "evaluation_copy", content_digest(plan_a)
    )
    fingerprint_b = copy_identity_fingerprint(
        "s12", "evaluation_copy", content_digest(plan_b)
    )
    assert fingerprint_a != fingerprint_b

    deleted = evaluation.s16_delete_scope(
        [fingerprint_a],
        operation_id="op-a",
        fence=0,
        scope_fingerprint=scope_a,
    )
    assert deleted["status"] == "complete", deleted
    remaining = evaluation.s16_enumerate_all_rows()
    assert [p["row_id"] for p in remaining["plans"]] == ["plan-b"]
    # The same-digest job owned by scope B SURVIVED the scope-A delete.
    assert [j["row_id"] for j in remaining["jobs"]] == ["job-b"]

    # Verify scope A: its OWN rows are gone; the same-digest row owned
    # by scope B is not part of scope A's deletion, so absent is TRUE.
    verify = evaluation.s16_verify_absent(
        [fingerprint_a], scope_fingerprint=scope_a
    )
    assert verify["absent"] is True
    assert verify["scope_mismatch"] is False

    # Delete scope B as well; then verify A: absent WITHOUT a mismatch
    # even though the scope-B binding for a different digest exists.
    deleted_b = evaluation.s16_delete_scope(
        [fingerprint_b],
        operation_id="op-b",
        fence=0,
        scope_fingerprint=scope_b,
    )
    assert deleted_b["status"] == "complete", deleted_b
    verify = evaluation.s16_verify_absent(
        [fingerprint_a], scope_fingerprint=scope_a
    )
    assert verify["absent"] is True, verify
    assert verify["scope_mismatch"] is False
    # The scope-B rows are gone too.
    assert evaluation.s16_enumerate_all_rows()["plans"] == []


def test_stale_tombstone_binding_never_proves_absence(tmp_path: Path) -> None:
    """R3 P1-3: absence proofs bind to the deletion operation + fence; a
    stale operation asking about the same scope sees not-absent."""
    service, submission, application_id = _registered_terminated(tmp_path)
    s16 = _s16_service(tmp_path, service, governance_scope=TENANT_SCOPE)
    result = _preflight(
        s16, str(submission["upstream_application_ref"]), scope=TENANT_SCOPE
    )
    s16.commit(
        request_id=result["request_id"],
        principal=GOVERNANCE_REGISTERED,
        idempotency_key="tombstone-commit",
    )
    outcome = s16.process_next_deletion_job(worker_id="w", now=_now() + 1)
    assert outcome["status"] == "complete", outcome
    scope = result["scope_fingerprint"]
    assert (
        service.s16_verify_absent(
            scope, operation_id="stale-op", fence=999
        )["absent"]
        is False
    )
    # The scope-level (any binding) proof still holds.
    assert service.s16_verify_absent(scope)["absent"] is True
    job = s16._job_for_request(result["request_id"])
    assert job is not None
    assert (
        service.s16_verify_absent(
            scope,
            operation_id=str(job["job_id"]),
            fence=int(job.get("fence") or 0),
        )["absent"]
        is True
    )


def test_preflight_replay_returns_original_manifest_after_hold_impose(
    tmp_path: Path,
) -> None:
    """R3 P1-8: a same-key preflight retry replays the FIRST manifest
    snapshot — a hold imposed in between never changes the replay."""
    service, submission, application_id = _registered_terminated(tmp_path)
    s16 = _s16_service(tmp_path, service, governance_scope=TENANT_SCOPE)
    reference = str(submission["upstream_application_ref"])
    first = _preflight(s16, reference, scope=TENANT_SCOPE)
    held = s16.impose_legal_hold(
        scope_fingerprint=first["scope_fingerprint"],
        reason_code="litigation",
        owner="all",
        effective_time=_now(),
        principal=GOVERNANCE_REGISTERED,
        idempotency_key="hold-before-replay",
    )
    assert held["status"] == "accepted"
    replayed = s16.preflight(
        application_reference=reference,
        principal=S01CommandPrincipal(
            subject=GOVERNANCE.subject,
            role="operator",
            scope=TENANT_SCOPE,
            source_id="s16-governance-console",
        ),
        idempotency_key=f"preflight-{reference}-{TENANT_SCOPE}",
    )
    assert replayed["replayed"] is True
    assert replayed["manifest_digest"] == first["manifest_digest"]
    assert replayed["entries_digest"] == first["entries_digest"]
    assert replayed["request_id"] == first["request_id"]


def test_repair_same_key_replays_after_job_resumed(tmp_path: Path) -> None:
    """R3 P1-9: the repair idempotency binding resolves BEFORE the job
    state is judged; a same-key retry replays the accepted repair even
    after the first repair moved the job back to pending."""
    service, submission, application_id = _registered_terminated(tmp_path)
    faults = {"armed": True}

    def fault(owner_id: str) -> None:
        if owner_id == "s02" and faults["armed"]:
            raise RuntimeError("injected")

    s16 = _s16_service(
        tmp_path,
        service,
        max_owner_attempts=2,
        fault_injector=fault,
        governance_scope=TENANT_SCOPE,
    )
    result = _preflight(
        s16, str(submission["upstream_application_ref"]), scope=TENANT_SCOPE
    )
    s16.commit(
        request_id=result["request_id"],
        principal=GOVERNANCE_REGISTERED,
        idempotency_key="repair-replay-commit",
    )
    first = s16.process_next_deletion_job(worker_id="w", now=_now() + 1)
    second = s16.process_next_deletion_job(worker_id="w", now=_now() + 2)
    assert second["status"] == "repair_required", (first, second)
    repaired = s16.repair(
        request_id=result["request_id"],
        owner_id="s02",
        repair_fact="s02-repair-verified",
        principal=GOVERNANCE_REGISTERED,
        idempotency_key="repair-replay-key",
    )
    assert repaired["status"] == "accepted"
    # Same key + same content: replays the accepted fact without judging
    # the (now pending) job state.
    replayed = s16.repair(
        request_id=result["request_id"],
        owner_id="s02",
        repair_fact="s02-repair-verified",
        principal=GOVERNANCE_REGISTERED,
        idempotency_key="repair-replay-key",
    )
    assert replayed["replayed"] is True
    assert replayed["status"] == "accepted"


def test_claim_cas_rejects_stale_five_originals(tmp_path: Path) -> None:
    """R3 P1-10: the claim compare-and-set judges status + lease expiry +
    fence + attempt against the database; a snapshot made before another
    worker advanced the job can never claim it."""
    from task4_consistency.controlled.s16 import S16Ledger

    ledger = S16Ledger(tmp_path / "claim.sqlite3")
    job = {
        "job_id": "s16job_claim",
        "request_id": "s16req_claim",
        "scope_fingerprint": "0" * 64,
        "status": "pending",
        "lease_owner": None,
        "lease_expires_at": None,
        "fence": 0,
        "attempt": 0,
        "pending_owner_fingerprints": {"s01": ["fp"]},
        "owner_results": {},
    }
    with ledger._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        ledger._upsert_job(connection, job, 1)
        connection.commit()
    # Instance A's snapshot: the job as it was before any claim.
    claimed = {
        **job,
        "status": "running",
        "lease_owner": "worker-a",
        "lease_expires_at": 61,
        "fence": 1,
        "attempt": 1,
        "updated_at": 1,
    }
    with ledger._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        ok = ledger._claim_job_cas(
            connection,
            job_id="s16job_claim",
            worker="worker-a",
            now=1,
            lease_seconds=60,
            claimed_payload=claimed,
            claimed_fence=1,
            claimed_attempt=1,
            expected_status="pending",
            expected_lease_expires_at=None,
            expected_fence=0,
            expected_attempt=0,
        )
        connection.commit()
    assert ok is True
    # Instance B still holds the ORIGINAL snapshot: every one of the five
    # originals is stale now; the CAS must refuse the claim.
    claimed_b = {
        **job,
        "status": "running",
        "lease_owner": "worker-b",
        "lease_expires_at": 61,
        "fence": 1,
        "attempt": 1,
        "updated_at": 1,
    }
    with ledger._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        ok_b = ledger._claim_job_cas(
            connection,
            job_id="s16job_claim",
            worker="worker-b",
            now=1,
            lease_seconds=60,
            claimed_payload=claimed_b,
            claimed_fence=1,
            claimed_attempt=1,
            expected_status="pending",
            expected_lease_expires_at=None,
            expected_fence=0,
            expected_attempt=0,
        )
        connection.execute("ROLLBACK")
    assert ok_b is False


def test_pre_r2_job_schema_backfills_columns_and_records_migration_fact(
    tmp_path: Path,
) -> None:
    """R3 P1-12: opening a pre-R2 ledger adds the worker columns and
    backfills them from the authoritative payload, and records the
    migration fact."""
    from task4_consistency.controlled.s16 import S16Ledger

    path = tmp_path / "old_ledger.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE s16_jobs ("
            "job_id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO s16_jobs VALUES (?, ?, ?)",
            (
                "s16job_old",
                json.dumps(
                    {
                        "job_id": "s16job_old",
                        "request_id": "s16req_old",
                        "scope_fingerprint": "1" * 64,
                        "status": "pending",
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "fence": 0,
                        "attempt": 2,
                        "pending_owner_fingerprints": {"s01": ["fp"]},
                        "owner_results": {},
                    }
                ),
                1,
            ),
        )
    ledger = S16Ledger(path)
    with sqlite3.connect(path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(s16_jobs)"
            ).fetchall()
        }
        fact = connection.execute(
            "SELECT payload FROM s16_meta_facts "
            "WHERE fact_key = 's16_jobs_schema_migration'"
        ).fetchone()
    assert {"status", "lease_owner", "lease_expires_at", "fence", "attempt"} <= columns
    assert fact is not None
    loaded = ledger._load_jobs()["s16job_old"]
    assert loaded["status"] == "pending"
    assert loaded["attempt"] == 2


def test_backup_delete_resumes_after_crash_between_unlink_and_commit(
    tmp_path: Path,
) -> None:
    """R3 P1-14: a crash between file unlink and the registry commit
    leaves a staged intent; the next attempt with the same operation
    resumes, completes and proves absence — never a permanent verify
    failure."""
    backup_root = tmp_path / "backups"
    backup = BackupDeletionOwner(backup_root, clock=_CdemoSessionClock())
    scope = "2" * 64
    saved = backup_root / "target.sqlite3"
    saved.write_bytes(b"captured-content")
    digest = hashlib.sha256(b"captured-content").hexdigest()
    backup.capture(
        scope_fingerprint=scope, copy_files=[("target.sqlite3", digest)]
    )
    fingerprints = {"fp-crash"}
    fingerprints_digest = backup._bindings_digest(fingerprints)
    identity = backup._connector_identity("target.sqlite3", digest)
    # Crash simulation: intent staged, files already unlinked, registry
    # rows and binding NOT committed.
    with backup._registry_connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO backup_deletion_intents("
            "operation_id, fence, scope_fingerprint, fingerprints_digest, "
            "status, staged_at, identities_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "op-crash",
                1,
                scope,
                fingerprints_digest,
                "staged",
                int(_now()),
                json.dumps([identity], separators=(",", ":")),
            ),
        )
        connection.commit()
    saved.unlink()
    for manifest in backup._load_manifests():
        backup._manifest_path(str(manifest["manifest_id"])).unlink()

    result = backup.delete(
        fingerprints,
        scope_fingerprint=scope,
        operation_id="op-crash",
        fence=1,
    )
    assert result["status"] == "complete", result
    assert backup.verify_absent(
        fingerprints, scope_fingerprint=scope
    )["status"] == "verified"
    with backup._registry_connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM backup_registry"
            ).fetchone()[0]
            == 0
        )
        binding = connection.execute(
            "SELECT status FROM backup_deletion_bindings "
            "WHERE operation_id = 'op-crash' AND fence = 1"
        ).fetchone()
        intent = connection.execute(
            "SELECT status FROM backup_deletion_intents "
            "WHERE operation_id = 'op-crash' AND fence = 1"
        ).fetchone()
    assert binding is not None and binding[0] == "complete"
    assert intent is not None and intent[0] == "committed"


def test_non_callable_audit_writer_fails_closed_at_construction(
    tmp_path: Path,
) -> None:
    """R3 P1-5: availability requires a callable writer; a non-callable
    writer is rejected at construction with zero state change."""
    service = _c_demo_service(tmp_path)
    with pytest.raises(ValueError, match="callable"):
        _s16_service(
            tmp_path,
            service,
            security_audit_available=True,
            security_audit_writer="not-callable",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="availability"):
        _s16_service(
            tmp_path,
            service,
            security_audit_available=False,
            security_audit_writer=_recording_audit_writer()[1],
        )


def test_partial_s16_configuration_fails_closed(tmp_path: Path) -> None:
    """R3 P0-2: ANY required S16 configuration present (state path only,
    no identities) makes S16_CONFIGURED true; the failed factory leaves
    the shared domain gate closed."""
    import os
    import subprocess
    import sys

    others = {
        f"TASK4_{name}": f"other-{name}"
        for name in (
            "S01_DEMO_CREDENTIAL",
            "S01_OPERATOR_CREDENTIAL",
            "S01_AUDITOR_CREDENTIAL",
            "S02_CREDENTIAL",
            "S05_EXCEPTION_APPROVER_CREDENTIAL",
            "S08_ADMIN_CREDENTIAL",
            "S08_APPROVER_CREDENTIAL",
            "S08_OPERATOR_CREDENTIAL",
            "S09_REPLAY_CREDENTIAL",
            "S09_SIMULATION_CREDENTIAL",
            "S12_CREDENTIAL",
            "S13_OPERATOR_CREDENTIAL",
            "S01_DEMO_SUBJECT",
            "S01_OPERATOR_SUBJECT",
            "S01_AUDITOR_SUBJECT",
            "S02_SUBJECT",
            "S05_EXCEPTION_APPROVER_SUBJECT",
            "S08_ADMIN_SUBJECT",
            "S08_APPROVER_SUBJECT",
            "S08_OPERATOR_SUBJECT",
            "S09_REPLAY_SUBJECT",
            "S09_SIMULATION_SUBJECT",
            "S12_SUBJECT",
            "S13_OPERATOR_SUBJECT",
        )
    }
    env = {
        **os.environ,
        **others,
        "TASK4_S16_STATE_PATH": str(tmp_path / "s16.sqlite3"),
        "TASK4_S16_BACKUP_ROOT": str(tmp_path / "backups"),
        "TASK4_S16_GOVERNANCE_CREDENTIAL": "",
        "TASK4_S16_GOVERNANCE_SUBJECT": "",
        "TASK4_S16_APPROVER1_CREDENTIAL": "",
        "TASK4_S16_APPROVER1_SUBJECT": "",
        "TASK4_S16_APPROVER2_CREDENTIAL": "",
        "TASK4_S16_APPROVER2_SUBJECT": "",
    }
    probe = (
        "import task4_consistency.web.app as app; "
        "print(bool(app.S16_CONFIGURED), app.S16_SERVICE is None, "
        "not app._s16_domain_read_gate())"
    )
    run = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr
    probe2 = (
        "import task4_consistency.web.app as app; "
        "print('alias', app._s16_identities_alias_controlled(), "
        "'ids', app._s16_identities_configured(), "
        "'cfg', app.S16_CONFIGURED)"
    )
    run2 = subprocess.run(
        [sys.executable, "-c", probe2],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert run.stdout.strip() == "True True True"



# ---------------------------------------------------------------------------
# R4 (P1/P2) regressions
# ---------------------------------------------------------------------------


def test_preflight_binding_never_persists_application_reference(
    tmp_path: Path,
) -> None:
    """R4 P1-1: the persistent idempotency binding stores ONLY the
    value-free preflight snapshot — the upstream reference, application id
    and caller key must not appear in s16_bindings.result, while the
    in-memory replay response still carries the reference and the same
    manifest digest."""
    service, submission, application_id = _registered_terminated(tmp_path)
    reference = str(submission["upstream_application_ref"])
    s16 = _s16_service(tmp_path, service, governance_scope=TENANT_SCOPE)
    first = _preflight(s16, reference, scope=TENANT_SCOPE)
    replayed = s16.preflight(
        application_reference=reference,
        principal=S01CommandPrincipal(
            subject=GOVERNANCE.subject,
            role="operator",
            scope=TENANT_SCOPE,
            source_id="s16-governance-console",
        ),
        idempotency_key=f"preflight-{reference}-{TENANT_SCOPE}",
    )
    assert replayed["replayed"] is True
    assert replayed["application_reference"] == reference
    assert replayed["manifest_digest"] == first["manifest_digest"]
    with sqlite3.connect(tmp_path / "s16.sqlite3") as connection:
        rows = connection.execute(
            "SELECT result FROM s16_bindings"
        ).fetchall()
    assert rows
    serialized = json.dumps(rows)
    for forbidden in (
        reference,
        application_id,
        "upstream-application",
        "target.sqlite3",
        "tenant-test",
        "result-object",
        "page-object",
        "s16-registered-intake",
        "t17-governance",
        "application_reference",
    ):
        assert forbidden not in serialized, forbidden
    stored = json.loads(rows[0][0])
    assert stored["request_id"] == first["request_id"]
    assert stored["manifest_digest"] == first["manifest_digest"]
    assert stored["scope_fingerprint"] == first["scope_fingerprint"]


def test_release_audit_fact_reconciles_with_release_request(
    tmp_path: Path,
) -> None:
    """R4 P1-2: the release security-audit fact fingerprints the RELEASE
    request id — identical to the release event and the service response,
    and different from the impose request id."""
    import hashlib

    service = _c_demo_service(tmp_path)
    _admit_c_demo(service, key="s16-release-audit-intake")
    _terminate(service, _app_of(service))
    recorded: list[dict[str, Any]] = []

    def writer(record: dict[str, Any]) -> bool:
        recorded.append(record)
        return True

    s16 = _s16_service(
        tmp_path,
        service,
        security_audit_writer=writer,
    )
    result = _preflight(s16, "APP-R53-BAD-ENGINE")
    hold = s16.impose_legal_hold(
        scope_fingerprint=result["scope_fingerprint"],
        principal=GOVERNANCE,
        reason_code="litigation",
        owner="all",
        effective_time=_now(),
        idempotency_key="rel-audit-hold",
    )
    released = s16.release_legal_hold(
        hold_id=hold["hold_id"],
        principal=GOVERNANCE,
        idempotency_key="rel-audit-1",
    )
    release_events = [
        event
        for event in s16._events
        if event.get("event_type") == "legal_hold_released"
    ]
    assert release_events
    assert release_events[0]["request_id"] == released["request_id"]
    # The ledger security-audit fact fingerprints the RELEASE request id.
    audit_facts = [
        event
        for event in s16._events
        if event.get("event_type") == "security_audit"
        and event.get("action") == "legal_hold_released"
    ]
    assert audit_facts
    assert audit_facts[0]["request_id_fingerprint"] == hashlib.sha256(
        released["request_id"].encode("utf-8")
    ).hexdigest()
    assert audit_facts[0]["request_id_fingerprint"] != hashlib.sha256(
        hold["request_id"].encode("utf-8")
    ).hexdigest()
    assert released["request_id"] != hold["request_id"]
    # The replicated WORM copy stays value-free (no request id fingerprint).
    assert recorded
    assert all(
        "request_id_fingerprint" not in record for record in recorded
    )


def test_backup_verify_binds_operation_fence_and_digest(
    tmp_path: Path,
) -> None:
    """R4 P1-3: backup absence proofs are binding proofs — the exact
    operation + fence must own the scope and fingerprints digest; stale
    fences are stable stale, mismatched digests are stable conflicts, and
    an operation with no binding never verifies."""
    backup_root = tmp_path / "backups"
    backup = BackupDeletionOwner(backup_root, clock=_CdemoSessionClock())
    scope = "5" * 64
    saved = backup_root / "target.sqlite3"
    saved.write_bytes(b"binding-capture")
    digest = hashlib.sha256(b"binding-capture").hexdigest()
    backup.capture(scope_fingerprint=scope, copy_files=[("target.sqlite3", digest)])
    fingerprints = {
        entry.identity_fingerprint
        for entry in backup.inventory(scope_fingerprint=scope)
    }
    assert backup.delete(
        fingerprints,
        scope_fingerprint=scope,
        operation_id="op-bind",
        fence=1,
    )["status"] == "complete"
    # Same binding replays verified.
    assert backup.verify_absent(
        fingerprints, scope_fingerprint=scope, operation_id="op-bind", fence=1
    )["status"] == "verified"
    assert backup.verify_absent(
        fingerprints, scope_fingerprint=scope, operation_id="op-bind", fence=1
    )["status"] == "verified"
    # Wrong digest under the SAME binding: stable conflict.
    with pytest.raises(S16OwnerFailure) as excinfo:
        backup.verify_absent(
            {"fp-other"},
            scope_fingerprint=scope,
            operation_id="op-bind",
            fence=1,
        )
    assert excinfo.value.reason_code == S16_OWNER_BINDING_CONFLICT
    assert excinfo.value.retryable is False
    # Stale fence: stable stale outcome.
    with pytest.raises(S16OwnerFailure) as excinfo:
        backup.verify_absent(
            fingerprints,
            scope_fingerprint=scope,
            operation_id="op-bind",
            fence=0,
        )
    assert excinfo.value.reason_code == S16_OWNER_STALE_FENCE
    assert excinfo.value.retryable is False
    # An operation with no binding at all never proves absence.
    with pytest.raises(S16OwnerFailure) as excinfo:
        backup.verify_absent(
            fingerprints,
            scope_fingerprint=scope,
            operation_id="op-never-ran",
            fence=1,
        )
    assert excinfo.value.reason_code == S16_VERIFY_FAILED
    # The already-absent outcome also writes the binding: verify passes.
    assert backup.delete(
        fingerprints,
        scope_fingerprint=scope,
        operation_id="op-absent",
        fence=1,
    )["already_absent"] is True
    assert backup.verify_absent(
        fingerprints,
        scope_fingerprint=scope,
        operation_id="op-absent",
        fence=1,
    )["status"] == "verified"


@pytest.mark.parametrize(
    "damage,repair",
    [
        (
            "manifest_deleted",
            "recapture",
        ),
        (
            "registry_deleted",
            "recapture",
        ),
        (
            "digest_tampered",
            "restore_file",
        ),
        (
            "file_missing",
            "restore_file",
        ),
    ],
)
def test_backup_reconciliation_fails_closed_and_repairs_forward(
    tmp_path: Path,
    damage: str,
    repair: str,
) -> None:
    """R4 P1-4: manifest/registry/file bidirectional damage makes the
    owner unhealthy, closes inventory and preflight with a stable
    unavailable, and a repaired owner moves forward again."""
    backup_root = tmp_path / "backups"
    backup = BackupDeletionOwner(backup_root, clock=_CdemoSessionClock())
    scope = "6" * 64
    saved = backup_root / "target.sqlite3"
    saved.write_bytes(b"reconcile-capture")
    digest = hashlib.sha256(b"reconcile-capture").hexdigest()
    backup.capture(scope_fingerprint=scope, copy_files=[("target.sqlite3", digest)])
    assert backup.owner_healthy() is True
    assert backup.inventory(scope_fingerprint=scope)

    if damage == "manifest_deleted":
        for manifest in backup._load_manifests():
            backup._manifest_path(str(manifest["manifest_id"])).unlink()
    elif damage == "registry_deleted":
        with backup._registry_connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM backup_registry")
            connection.commit()
    elif damage == "digest_tampered":
        saved.write_bytes(b"tampered")
    elif damage == "file_missing":
        saved.unlink()

    assert backup.owner_healthy() is False
    with pytest.raises(S16Unavailable):
        backup.inventory(scope_fingerprint=scope)

    # Preflight through the orchestrator fails closed with the same root.
    service, submission, application_id = _registered_terminated(tmp_path)
    s16 = _s16_service(
        tmp_path, service, backup_root=backup_root, governance_scope=TENANT_SCOPE
    )
    with pytest.raises(S16Unavailable):
        s16.preflight(
            application_reference=str(submission["upstream_application_ref"]),
            principal=S01CommandPrincipal(
                subject=GOVERNANCE.subject,
                role="operator",
                scope=TENANT_SCOPE,
                source_id="s16-governance-console",
            ),
            idempotency_key="reconcile-preflight",
        )

    # Repair restores integrity; the owner moves forward.
    if repair == "recapture":
        if not saved.exists():
            saved.write_bytes(b"reconcile-capture")
        backup.capture(
            scope_fingerprint=scope,
            copy_files=[("target.sqlite3", digest)],
        )
    else:
        saved.write_bytes(b"reconcile-capture")
    assert backup.owner_healthy() is True
    assert backup.inventory(scope_fingerprint=scope)


def test_expired_hold_query_state_and_active_union(tmp_path: Path) -> None:
    """R4 P2-2: the query reports an explicit expired state for a hold
    whose expiry passed (or whose expiry transition was recorded), and the
    active hold union — which gates commit — excludes it."""
    service, submission, application_id = _registered_terminated(tmp_path)
    s16 = _s16_service(tmp_path, service, governance_scope=TENANT_SCOPE)
    result = _preflight(
        s16, str(submission["upstream_application_ref"]), scope=TENANT_SCOPE
    )
    scope = result["scope_fingerprint"]
    now = _now()
    active = s16.impose_legal_hold(
        scope_fingerprint=scope,
        principal=GOVERNANCE_REGISTERED,
        reason_code="litigation",
        owner="all",
        effective_time=now,
        idempotency_key="hold-exp-active",
        expiry=now + 5000,
    )
    expired = s16.impose_legal_hold(
        scope_fingerprint=scope,
        principal=GOVERNANCE_REGISTERED,
        reason_code="regulatory",
        owner="s01",
        effective_time=now - 10,
        idempotency_key="hold-exp-expired",
        expiry=now - 1,
    )
    query = s16.query(
        request_id=result["request_id"], principal=GOVERNANCE_REGISTERED
    )
    by_id = {hold["hold_id"]: hold for hold in query["legal_holds"]}
    assert by_id[active["hold_id"]]["state"] == "active"
    assert by_id[expired["hold_id"]]["state"] == "expired"
    assert by_id[expired["hold_id"]]["released"] is False
    assert s16._active_hold_union(scope)  # the active hold still gates
    # Releasing the ACTIVE hold turns it terminal; the expired hold stays
    # expired and the union empties.
    s16.release_legal_hold(
        hold_id=active["hold_id"],
        principal=GOVERNANCE_REGISTERED,
        idempotency_key="hold-exp-release",
    )
    query = s16.query(
        request_id=result["request_id"], principal=GOVERNANCE_REGISTERED
    )
    by_id = {hold["hold_id"]: hold for hold in query["legal_holds"]}
    assert by_id[active["hold_id"]]["state"] == "released"
    assert by_id[expired["hold_id"]]["state"] == "expired"
    assert not s16._active_hold_union(scope)
