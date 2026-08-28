"""Ticket #32 / T17 — production FastAPI fixture and shell contracts.

The factory drives the released S16 governed-deletion seams into one
terminated registered application with registered objects, a persistent S02
absence store, an empty evaluation plane and a scope-scoped backup root.
The browser then exercises preflight, two approvals, commit, worker fault,
repair, completion, receipt, hard refresh and post-delete retrieval through
the real FastAPI authority and the shared production React build.

The worker is not auto-started here: every deletion attempt runs through
the registered process endpoint so the browser scenario stays deterministic
(S01 background runtime disabled in the fixture).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from task4_consistency.controlled.s01 import (
    AdmissionDisposition,
    ControlledScenarioService,
    S01CommandPrincipal,
)
from task4_consistency.controlled.s16 import (
    BackupDeletionOwner,
    ExportTempOwner,
    GovernedDeletionService,
    RetentionPolicy,
    S01DeletionOwner,
    S02DeletionOwner,
    S12DeletionOwner,
)
from task4_consistency.controlled.s12 import EvaluationService

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"
FIXTURES = ROOT / "fixtures" / "applications"

T17_GOVERNANCE_CREDENTIAL = "t17-governance-credential"
T17_GOVERNANCE_SUBJECT = "t17-governance"
T17_APPROVER1_CREDENTIAL = "t17-approver1-credential"
T17_APPROVER1_SUBJECT = "t17-approver1"
T17_APPROVER2_CREDENTIAL = "t17-approver2-credential"
T17_APPROVER2_SUBJECT = "t17-approver2"
T17_SCOPE = "R-OBSERVED/tenant-test"

FIXED_NOW = 1_800_000_000


def _build_registered_service(work_root: Path) -> tuple[ControlledScenarioService, dict[str, object], str]:
    """One terminated registered application with a persistent S02 absence
    store and the standard two registered objects."""
    from tests.test_s02_controlled import (
        INTEGRATOR,
        TENANT_SCOPE,
        _descriptor,
        _detection_result,
        _png,
        _registered_service,
    )
    from tests.test_s14_controlled import _settle_to_terminated

    service, submission = _registered_service(work_root)
    admitted = service.submit_registered(
        submission=submission,
        idempotency_key="t17-registered-intake",
        principal=INTEGRATOR,
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED, admitted
    from tests.test_s01_controlled import worker_test_driver

    worker_test_driver(service).process_next_job(now=FIXED_NOW)
    service.refresh_projection()
    application_id = str(admitted.application_id)
    route = service.current_route_view(
        principal=S01CommandPrincipal(
            subject=INTEGRATOR.subject,
            role="reviewer",
            scope=TENANT_SCOPE,
            source_id="t17-registered-review",
        ),
        application_id=application_id,
    )
    cancel = service.cancel_application(
        application_id=application_id,
        principal=S01CommandPrincipal(
            subject=INTEGRATOR.subject,
            role="integrator",
            scope=TENANT_SCOPE,
            source_id=INTEGRATOR.source_id,
        ),
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key=f"t17-registered-cancel-{application_id}",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted", cancel
    settled = _settle_to_terminated(service, application_id, cancel["lifecycle_revision"])
    assert settled["status"] == "terminated", settled
    # Rebuild the authority with the deployment-configured absence store.
    source_boundary = service.registered_source_boundary
    governed_service = ControlledScenarioService(
        fixture_root=FIXTURES,
        rules_path=RULES,
        state_path=work_root / "target.sqlite3",
        registered_sources=source_boundary._registrations,
        controlled_objects=tuple(source_boundary._objects.values()),
        controlled_object_absence_store=work_root / "s02_absence.sqlite3",
    )
    return governed_service, submission, application_id


def _empty_evaluation(work_root: Path) -> EvaluationService:
    return EvaluationService(
        state_path=work_root / "evaluation.sqlite3",
        clock=lambda: FIXED_NOW,
        snapshot_provider=lambda *_: None,
        release_provider=lambda *_: None,
        label_manifest_provider=lambda *_: None,
        business_state_provider=lambda: {},
        business_publication_guard=lambda revisions: None,
    )


def _build_fixture(work_root: Path) -> dict[str, Any]:
    """One terminated registered application, S02 absence store, S12 empty
    plane, backup root and a fault flag the browser scenario toggles."""
    service, submission, application_id = _build_registered_service(work_root)
    evaluation = _empty_evaluation(work_root)
    backup_root = work_root / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    fault_flag = work_root / "s02_fault"
    repaired_flag = work_root / "s02_repaired"

    def fault_injector(owner_id: str) -> None:
        if owner_id == "s02" and fault_flag.exists() and not repaired_flag.exists():
            raise RuntimeError("injected s02 deletion fault")

    retention = RetentionPolicy(retention_seconds=10**12)
    s01_owner = S01DeletionOwner(
        service,
        retention=retention,
        clock=lambda: FIXED_NOW,
    )
    def _recording_writer() -> Any:
        def writer(record: dict[str, Any]) -> bool:
            return True

        return writer

    s16 = GovernedDeletionService(
        ledger_path=work_root / "s16.sqlite3",
        owners={
            "s01": s01_owner,
            "s02": S02DeletionOwner(
                service.registered_source_boundary, s01_owner
            ),
            "s12": S12DeletionOwner(evaluation),
            "backup": BackupDeletionOwner(
                backup_root, clock=lambda: FIXED_NOW
            ),
            "s17-disabled": ExportTempOwner(),
        },
        retention=retention,
        governance_subject=T17_GOVERNANCE_SUBJECT,
        approver_subjects=(T17_APPROVER1_SUBJECT, T17_APPROVER2_SUBJECT),
        governance_scope=T17_SCOPE,
        security_audit_writer=_recording_writer(),
        clock=lambda: FIXED_NOW,
        fault_injector=fault_injector,
    )
    s16._owners["s02"].verify_repair = (  # type: ignore[method-assign]
        lambda owner_id, repair_fact: (
            repair_fact == "s02-repair-verified"
            and (not fault_flag.exists() or repaired_flag.exists())
        )
    )
    return {
        "service": service,
        "evaluation": evaluation,
        "s16": s16,
        "submission": submission,
        "application_id": application_id,
        "backup_root": backup_root,
        "fault_flag": fault_flag,
        "repaired_flag": repaired_flag,
        "reference": str(submission["upstream_application_ref"]),
    }


def create_t17_react_test_app():
    """Return the real FastAPI app bound to one persisted T17 fixture."""
    import task4_consistency.web.app as web

    work_root = Path(os.environ["TASK4_T17_FIXTURE_ROOT"])
    work_root.mkdir(parents=True, exist_ok=True)
    fixture = _build_fixture(work_root)

    web.S01_BACKGROUND_ENABLED = False
    web.S01_REQUIRE_CONFIGURED_STARTUP = False
    web.S01_SERVICE = fixture["service"]
    web.S01_DEMO_CREDENTIAL = "t17-demo-credential"
    web.S01_DEMO_SUBJECT = "t17-demo"
    web.S01_OPERATOR_CREDENTIAL = "t17-operator-credential"
    web.S01_OPERATOR_SUBJECT = "t17-operator"
    web.S12_SERVICE = fixture["evaluation"]
    web.S16_SERVICE = fixture["s16"]
    web.S16_GOVERNANCE_CREDENTIAL = T17_GOVERNANCE_CREDENTIAL
    web.S16_GOVERNANCE_SUBJECT = T17_GOVERNANCE_SUBJECT
    web.S16_APPROVER1_CREDENTIAL = T17_APPROVER1_CREDENTIAL
    web.S16_APPROVER1_SUBJECT = T17_APPROVER1_SUBJECT
    web.S16_APPROVER2_CREDENTIAL = T17_APPROVER2_CREDENTIAL
    web.S16_APPROVER2_SUBJECT = T17_APPROVER2_SUBJECT
    web.S16_GOVERNANCE_SCOPE = T17_SCOPE
    react_dir = os.environ.get("TASK4_T17_REACT_DIR", "").strip()
    web.S01_REACT_INDEX = (
        Path(react_dir).resolve() / "index.html"
        if react_dir
        else web.S01_REACT_STATIC / "index.html"
    )
    (work_root / "fixture.json").write_text(
        json.dumps(
            {
                "schema_version": "t17-browser-fixture/1",
                "reference": fixture["reference"],
                "application_id": fixture["application_id"],
                "fault_flag": str(fixture["fault_flag"]),
                "repaired_flag": str(fixture["repaired_flag"]),
                "governance_credential": T17_GOVERNANCE_CREDENTIAL,
                "approver1_credential": T17_APPROVER1_CREDENTIAL,
                "approver2_credential": T17_APPROVER2_CREDENTIAL,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return web.app


def _install(monkeypatch: Any, tmp_path: Path) -> dict[str, Any]:
    import task4_consistency.web.app as web

    fixture = _build_fixture(tmp_path)
    monkeypatch.setattr(web, "S01_BACKGROUND_ENABLED", False)
    monkeypatch.setattr(web, "S01_REQUIRE_CONFIGURED_STARTUP", False)
    monkeypatch.setattr(web, "S01_SERVICE", fixture["service"])
    monkeypatch.setattr(web, "S12_SERVICE", fixture["evaluation"])
    monkeypatch.setattr(web, "S16_SERVICE", fixture["s16"])
    monkeypatch.setattr(web, "S16_GOVERNANCE_CREDENTIAL", T17_GOVERNANCE_CREDENTIAL)
    monkeypatch.setattr(web, "S16_GOVERNANCE_SUBJECT", T17_GOVERNANCE_SUBJECT)
    monkeypatch.setattr(web, "S16_APPROVER1_CREDENTIAL", T17_APPROVER1_CREDENTIAL)
    monkeypatch.setattr(web, "S16_APPROVER1_SUBJECT", T17_APPROVER1_SUBJECT)
    monkeypatch.setattr(web, "S16_APPROVER2_CREDENTIAL", T17_APPROVER2_CREDENTIAL)
    monkeypatch.setattr(web, "S16_APPROVER2_SUBJECT", T17_APPROVER2_SUBJECT)
    monkeypatch.setattr(web, "S16_GOVERNANCE_SCOPE", T17_SCOPE)
    return fixture


def _governance_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {T17_GOVERNANCE_CREDENTIAL}"}


def _approver_headers(index: int) -> dict[str, str]:
    credential = T17_APPROVER1_CREDENTIAL if index == 1 else T17_APPROVER2_CREDENTIAL
    return {"Authorization": f"Bearer {credential}"}


def test_t17_shell_requires_governance_identity(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path)
    client = TestClient(webapp_of())

    anonymous = client.get("/controlled/s16")
    assert anonymous.status_code == 403
    anonymous_alias = client.get("/controlled/s16/react")
    assert anonymous_alias.status_code == 403

    issued = client.get(
        "/controlled/s16/react", headers=_governance_headers()
    )
    assert issued.status_code == 200, issued.text
    assert issued.headers["cache-control"] == "no-store"


def test_t17_shell_fails_closed_without_production_build(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import task4_consistency.web.app as web

    _install(monkeypatch, tmp_path)
    missing = tmp_path / "missing-react-build"
    missing.mkdir()
    monkeypatch.setattr(web, "S01_REACT_INDEX", missing / "index.html")
    client = TestClient(webapp_of())

    for path in ("/controlled/s16", "/controlled/s16/react"):
        broken = client.get(path, headers=_governance_headers())
        assert broken.status_code == 503
        assert broken.json()["detail"]["error"] == "S16_REACT_UNAVAILABLE"
        assert broken.headers["cache-control"] == "no-store"


def test_t17_full_governed_deletion_path_through_public_routes(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Preflight -> two approvals -> commit -> faulted attempts ->
    repair -> complete -> receipt -> post-delete existence hiding."""
    fixture = _install(monkeypatch, tmp_path)
    client = TestClient(webapp_of())
    reference = fixture["reference"]

    preflight = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_governance_headers(),
        json={"application_reference": reference, "idempotency_key": "t17-preflight"},
    )
    assert preflight.status_code == 200, preflight.text
    body = preflight.json()
    request_id = body["request_id"]
    manifest_digest = body["manifest_digest"]
    assert {entry["copy_class"] for entry in body["entries"]} == {
        "source_object",
        "derived_object",
        "evidence",
        "run_or_finding",
        "projection_or_cache",
        "export_or_temp",
        "evaluation_copy",
        "replica",
        "backup_manifest",
    }
    assert body["early_deletion"] is True

    for index in (1, 2):
        approved = client.post(
            f"/controlled/s16/api/deletions/{request_id}/approve",
            headers=_approver_headers(index),
            json={
                "manifest_digest": manifest_digest,
                "idempotency_key": f"t17-approve-{index}",
            },
        )
        assert approved.status_code == 200, approved.text

    committed = client.post(
        f"/controlled/s16/api/deletions/{request_id}/commit",
        headers=_governance_headers(),
        json={"idempotency_key": "t17-commit"},
    )
    assert committed.status_code == 200, committed.text

    # Arm the S02 fault: the first attempts fail and the job enters
    # repair_required after the bounded retry budget.
    fixture["fault_flag"].write_text("armed", encoding="utf-8")
    for _attempt in range(5):
        outcome = client.post(
            "/controlled/s16/api/process", headers=_governance_headers()
        )
        assert outcome.status_code == 200, outcome.text
        if outcome.json()["status"] == "repair_required":
            break
    query = client.get(
        f"/controlled/s16/api/deletions/{request_id}",
        headers=_governance_headers(),
    )
    assert query.status_code == 200, query.text
    job = query.json()["job"]
    assert job["status"] == "repair_required", job
    assert job["stable_failure"]["owner_id"] == "s02"
    assert job["stable_failure"]["reason_code"] == "S16_OWNER_DELETE_FAILED"

    # The repair fact is rejected while the fault is still armed.
    rejected = client.post(
        f"/controlled/s16/api/deletions/{request_id}/repair",
        headers=_governance_headers(),
        json={
            "owner_id": "s02",
            "repair_fact": "s02-repair-verified",
            "idempotency_key": "t17-repair-early",
        },
    )
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["reason_code"] == "S16_REPAIR_NOT_VERIFIED"

    # The operator repairs the owner; repair resumes the same job.
    fixture["repaired_flag"].write_text("ok", encoding="utf-8")
    repaired = client.post(
        f"/controlled/s16/api/deletions/{request_id}/repair",
        headers=_governance_headers(),
        json={
            "owner_id": "s02",
            "repair_fact": "s02-repair-verified",
            "idempotency_key": "t17-repair",
        },
    )
    assert repaired.status_code == 200, repaired.text

    outcome = client.post(
        "/controlled/s16/api/process", headers=_governance_headers()
    )
    assert outcome.status_code == 200, outcome.text
    assert outcome.json()["status"] == "complete", outcome.text

    receipt = client.get(
        f"/controlled/s16/api/deletions/{request_id}/receipt",
        headers=_governance_headers(),
    )
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["result"] == "deleted"
    assert receipt.json()["owner_counts"].get("s02") == 2
    serialized = json.dumps(receipt.json())
    assert fixture["application_id"] not in serialized
    assert reference not in serialized

    # Post-delete retrieval: the same preflight key now existence-hides.
    hidden = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_governance_headers(),
        json={"application_reference": reference, "idempotency_key": "t17-preflight"},
    )
    assert hidden.status_code == 404
    assert hidden.json()["detail"]["error"] == "S16_NOT_FOUND"
    # The S02 absence store keeps the objects unreadable.
    with pytest.raises(LookupError):
        fixture["service"].registered_source_boundary.read_object(
            tenant_id="tenant-test",
            source_system_id="registered-source",
            object_ref="result-object",
        )
    assert fixture["service"].registered_source_boundary.s02_inventory()["objects"] == []
    # S01 reads existence-hide.
    from task4_consistency.controlled.s01 import QueryNotFound

    with pytest.raises(QueryNotFound):
        fixture["service"].current_route_view(
            principal=S01CommandPrincipal(
                subject="t17-reviewer",
                role="reviewer",
                scope=T17_SCOPE,
                source_id="t17-review",
            ),
            application_id=fixture["application_id"],
        )


def webapp_of():
    import task4_consistency.web.app as web

    return web.app
