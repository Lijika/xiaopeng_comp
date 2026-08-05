from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from task4_consistency.controlled.s01 import (
    AdmissionDisposition,
    ControlledScenarioService,
    ControlledScenarioTestDriver,
    QueryNotFound,
    S01CommandPrincipal,
)
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules
from tests.test_s04_controlled import (
    REVIEWER as CORRECTION_REVIEWER,
    _ready_engine_correction,
)
from tests.test_s05_controlled import (
    APPROVER as S05_APPROVER,
    REVIEWER as S05_REVIEWER,
    ROUTER as S05_ROUTER,
    _approve_brand_exception,
    _ready_brand_exception,
    _request_brand_exception,
)
from tests.test_s06_controlled import (
    SUPPLEMENT_INTEGRATOR,
    REVIEWER as S06_REVIEWER,
    _attachment_submission,
    _generic_observation_submission,
    _ready_supplement_request,
    _supplement_service,
)


ROOT = Path(__file__).resolve().parents[1]
INTAKE = S01CommandPrincipal(
    subject="s07-reviewer",
    role="integrator",
    scope="C-DEMO",
    source_id="s07-demo-intake",
)
REVIEWER = S01CommandPrincipal(
    subject=INTAKE.subject,
    role="reviewer",
    scope=INTAKE.scope,
    source_id="s07-review-console",
)
OPERATOR = S01CommandPrincipal(
    subject="s07-operator",
    role="operator",
    scope="C-DEMO",
    source_id="s07-operations-console",
)


def _service(tmp_path: Path, **kwargs: object) -> ControlledScenarioService:
    return ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        **kwargs,
    )


def _admit(service: ControlledScenarioService) -> str:
    result = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s07-admission",
        principal=INTAKE,
    )
    assert result.disposition is AdmissionDisposition.ACCEPTED
    assert result.application_id is not None
    return result.application_id


def test_terminal_protected_failure_opens_minimized_lifecycle_recovery_work(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    application_id = _admit(service)

    failed = ControlledScenarioTestDriver(service).process_next_job(
        worker_id="s07-check-worker",
        now=10,
        operation_fault="checker_incompatible",
    )

    assert failed.status == "blocked"
    assert failed.application_id == application_id
    assert failed.recovery_work_id is not None
    view = service.recovery_work_view(
        principal=REVIEWER,
        recovery_work_id=failed.recovery_work_id,
    )
    assert view == {
        **view,
        "schema_version": "recovery-work-view/1",
        "status": "open",
        "application_id": application_id,
        "phase": "Unprocessable",
        "primary_reason_code": "configuration.checker_unavailable",
        "related_reason_codes": [],
        "operation": "execute_check_run",
        "dependency": "c-demo-target-checker",
        "responsible_party": "policy_owner",
        "recovery_action": "restore_exact_release_or_activate_compatible_successor",
        "recovery_target": "Evidence Ready",
        "protected_business_revision": 0,
        "current_run_id": None,
        "retryable": False,
    }
    assert view["attempts"] == [
        {
            "attempt": 1,
            "classification": "terminal",
            "status": "blocked",
            "started_at": 10,
            "retry_not_before": None,
        }
    ]
    assert view["criterion"]["id"] == "s07-checker-compatibility/1"
    assert len(view["criterion"]["digest"]) == 64
    encoded = json.dumps(view, sort_keys=True)
    assert "run_spec" not in encoded
    assert "evidence_snapshot" not in encoded
    assert '"raw"' not in encoded


def test_transient_retry_uses_three_fixed_attempts_then_exhausts(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _admit(service)
    driver = ControlledScenarioTestDriver(service)

    first = driver.process_next_job(
        worker_id="s07-check-worker",
        now=0,
        operation_fault="checker_transient",
    )
    too_early_1 = driver.process_next_job(worker_id="s07-check-worker", now=0)
    second = driver.process_next_job(
        worker_id="s07-check-worker",
        now=1,
        operation_fault="checker_transient",
    )
    too_early_2 = driver.process_next_job(worker_id="s07-check-worker", now=2)
    exhausted = driver.process_next_job(
        worker_id="s07-check-worker",
        now=3,
        operation_fault="checker_transient",
    )

    assert (first.status, first.retry_after_seconds) == ("retry_wait", 1)
    assert (second.status, second.retry_after_seconds) == ("retry_wait", 2)
    assert too_early_1.status == too_early_2.status == "idle"
    assert first.lifecycle_revision == second.lifecycle_revision == 4
    assert exhausted.status == "blocked"
    assert exhausted.lifecycle_revision == 5
    assert exhausted.recovery_work_id is not None
    view = service.recovery_work_view(
        principal=REVIEWER,
        recovery_work_id=exhausted.recovery_work_id,
    )
    assert view["primary_reason_code"] == "check.failed"
    assert view["related_reason_codes"] == ["operation.retry_exhausted"]
    assert view["logical_operation_id"] == first.job_id == second.job_id
    assert view["job_status"] == "exhausted"
    assert view["delivery_semantics"] == "at_least_once"
    assert view["retry_policy"] == {
        "id": "s07-c-demo-retry/1",
        "max_attempts": 3,
        "retry_offsets_seconds": [1, 2],
        "jitter": False,
    }
    assert view["attempts"] == [
        {
            "attempt": 1,
            "classification": "transient",
            "status": "retry_wait",
            "started_at": 0,
            "retry_not_before": 1,
        },
        {
            "attempt": 2,
            "classification": "transient",
            "status": "retry_wait",
            "started_at": 1,
            "retry_not_before": 3,
        },
        {
            "attempt": 3,
            "classification": "transient",
            "status": "exhausted",
            "started_at": 3,
            "retry_not_before": None,
        },
    ]
    assert view["protected_business_revision"] == 0
    assert view["current_run_id"] is None


def test_three_expired_crash_leases_exhaust_without_a_fourth_execution(
    tmp_path: Path,
) -> None:
    checker_calls = 0

    def checker(application: object) -> object:
        nonlocal checker_calls
        checker_calls += 1
        return RuleEngine(
            load_rules(ROOT / "configs" / "rules_auto_lease.yaml")
        ).run(application)  # type: ignore[arg-type]

    service = _service(tmp_path, checker_runner=checker)
    application_id = _admit(service)
    crashes = []
    for attempt, now in enumerate((0, 30, 60), start=1):
        restarted = _service(tmp_path, checker_runner=checker)
        crashes.append(
            ControlledScenarioTestDriver(restarted).process_next_job(
                worker_id=f"s07-crash-worker-{attempt}",
                now=now,
                crash=True,
            )
        )

    authority = _service(tmp_path, checker_runner=checker)
    exhausted = ControlledScenarioTestDriver(authority).process_next_job(
        worker_id="s07-no-fourth-execution",
        now=90,
    )

    assert [result.status for result in crashes] == ["crashed"] * 3
    assert {result.job_id for result in crashes} == {crashes[0].job_id}
    assert [result.fence for result in crashes] == [1, 2, 3]
    assert checker_calls == 0
    assert exhausted.status == "blocked"
    assert exhausted.application_id == application_id
    assert exhausted.job_id == crashes[0].job_id
    assert exhausted.fence == crashes[-1].fence
    assert exhausted.recovery_work_id is not None
    assert authority.fact_counts()["attempts"] == 3
    view = authority.recovery_work_view(
        principal=REVIEWER,
        recovery_work_id=exhausted.recovery_work_id,
    )
    assert view["primary_reason_code"] == "check.failed"
    assert view["related_reason_codes"] == ["operation.retry_exhausted"]
    assert view["job_status"] == "exhausted"
    assert view["logical_operation_id"] == crashes[0].job_id
    assert view["attempts"] == [
        {
            "attempt": 1,
            "classification": "transient",
            "status": "retry_wait",
            "started_at": 0,
            "retry_not_before": 1,
        },
        {
            "attempt": 2,
            "classification": "transient",
            "status": "retry_wait",
            "started_at": 30,
            "retry_not_before": 32,
        },
        {
            "attempt": 3,
            "classification": "transient",
            "status": "exhausted",
            "started_at": 60,
            "retry_not_before": None,
        },
    ]
    assert view["protected_business_revision"] == 0
    assert view["current_run_id"] is None


def test_dead_letter_is_stable_recovery_work_and_never_an_implicit_requeue(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    _admit(service)
    driver = ControlledScenarioTestDriver(service)

    blocked = driver.process_next_job(
        worker_id="s07-dead-letter-worker",
        now=5,
        operation_fault="checker_dead_lettered",
    )

    assert blocked.status == "blocked"
    assert blocked.recovery_work_id is not None
    view = service.recovery_work_view(
        principal=REVIEWER,
        recovery_work_id=blocked.recovery_work_id,
    )
    assert view["primary_reason_code"] == "check.failed"
    assert view["related_reason_codes"] == ["operation.dead_lettered"]
    assert view["job_status"] == "dead_lettered"
    assert view["retryable"] is False
    assert view["protected_business_revision"] == 0
    assert view["current_run_id"] is None
    before = service.fact_counts()

    idle = driver.process_next_job(worker_id="s07-must-not-requeue", now=500)

    assert (idle.status, idle.reason_code) == ("idle", "NO_READY_JOB")
    assert service.fact_counts() == before
    assert service.recovery_work_view(
        principal=REVIEWER,
        recovery_work_id=blocked.recovery_work_id,
    ) == view


def test_verified_recovery_resolves_work_and_reenters_the_fixed_normal_gate(
    tmp_path: Path,
) -> None:
    verifier_calls: list[dict[str, object]] = []

    def verifier(work: dict[str, object]) -> dict[str, object]:
        verifier_calls.append(work)
        criterion = work["criterion"]
        assert isinstance(criterion, dict)
        conditions = work["conditions"]
        assert isinstance(conditions, list)
        return {
            "verification_id": "s07-checker-probe-fact-1",
            "observed_at": int(work["opened_at"]) + 1,
            "evidence_kind": "checker_compatibility_probe",
            "scope": work["visibility_scope"],
            "recovery_work_id": work["recovery_work_id"],
            "criterion_digest": criterion["digest"],
            "conditions": [
                {
                    "condition_id": condition["condition_id"],
                    "verified": True,
                    "evidence_digest": "a" * 64,
                }
                for condition in conditions
            ],
        }

    service = _service(tmp_path, recovery_verifier=verifier)
    _admit(service)
    blocked = ControlledScenarioTestDriver(service).process_next_job(
        worker_id="s07-check-worker",
        now=10,
        operation_fault="checker_incompatible",
    )
    assert blocked.recovery_work_id is not None
    before = service.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    )

    accepted = service.verify_recovery(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
        expected_lifecycle_revision=before["lifecycle_revision"],
        expected_criterion_digest=before["criterion"]["digest"],
        idempotency_key="s07-verify-1",
    )
    replay = service.verify_recovery(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
        expected_lifecycle_revision=before["lifecycle_revision"],
        expected_criterion_digest=before["criterion"]["digest"],
        idempotency_key="s07-verify-1",
    )

    assert len(verifier_calls) == 1
    assert accepted["status"] == "accepted"
    assert accepted["phase"] == "Evidence Ready"
    assert accepted["lifecycle_revision"] == before["lifecycle_revision"] + 1
    assert accepted["recovery_fact_id"] == "s07-checker-probe-fact-1"
    assert accepted["successor_job_id"] != blocked.job_id
    assert accepted["successor_fence"] > blocked.fence
    assert replay == {**accepted, "replayed": True}
    resolved = service.recovery_work_view(
        principal=REVIEWER,
        recovery_work_id=blocked.recovery_work_id,
    )
    assert resolved["status"] == "resolved"
    assert resolved["phase"] == "Evidence Ready"
    assert resolved["current_run_id"] is None

    rerun = ControlledScenarioTestDriver(service).process_next_job(
        worker_id="s07-recovery-worker",
        now=12,
    )
    assert rerun.status == "complete"
    assert rerun.fence > blocked.fence
    assert rerun.lifecycle_phases[-2:] == (
        "Routing Determination",
        "Manual Review",
    )


def test_compensation_recovery_requires_every_fresh_condition_and_exact_scope(
    tmp_path: Path,
) -> None:
    verification: dict[str, object] = {}

    def verifier(work: dict[str, object]) -> dict[str, object]:
        return {
            "verification_id": verification.get("verification_id", "s07-fact"),
            "observed_at": verification.get(
                "observed_at", int(work["opened_at"]) + 1
            ),
            "evidence_kind": verification.get(
                "evidence_kind", "compensation_receipt"
            ),
            "scope": work["visibility_scope"],
            "recovery_work_id": work["recovery_work_id"],
            "criterion_digest": work["criterion"]["digest"],
            "conditions": [
                {
                    "condition_id": condition["condition_id"],
                    "verified": condition["condition_id"]
                    != verification.get("false_condition"),
                    "evidence_digest": "b" * 64,
                }
                for condition in work["conditions"]
            ],
        }

    service = _service(tmp_path, recovery_verifier=verifier)
    _admit(service)
    blocked = ControlledScenarioTestDriver(service).process_next_job(
        worker_id="s07-check-worker",
        now=20,
        operation_fault="compensation_failed",
    )
    assert blocked.status == "blocked"
    assert blocked.recovery_work_id is not None
    work_id = blocked.recovery_work_id
    view = service.recovery_work_view(principal=OPERATOR, recovery_work_id=work_id)
    assert view["primary_reason_code"] == "operation.compensation_failed"
    assert view["related_reason_codes"] == ["check.outcome_unknown"]
    assert view["job_status"] == "compensation_failed"
    assert view["outcome_known"] is False
    assert [condition["condition_id"] for condition in view["criterion"]["conditions"]] == [
        "s07-exact-operation-reconciled/1",
        "s07-compensation-receipt/1",
    ]

    command = {
        "principal": OPERATOR,
        "recovery_work_id": work_id,
        "expected_lifecycle_revision": view["lifecycle_revision"],
        "expected_criterion_digest": view["criterion"]["digest"],
    }
    before = service.fact_counts()
    verification["false_condition"] = "s07-exact-operation-reconciled/1"
    rejected = service.verify_recovery(**command, idempotency_key="s07-false")
    assert rejected["reason_code"] == "recovery.criterion_not_satisfied"
    assert service.fact_counts() == before

    verification.update(
        false_condition=None,
        observed_at=20,
    )
    timer = service.verify_recovery(**command, idempotency_key="s07-timer")
    assert timer["reason_code"] == "recovery.criterion_not_satisfied"
    assert service.fact_counts() == before

    verification.update(observed_at=21, evidence_kind="old_success")
    old_success = service.verify_recovery(**command, idempotency_key="s07-old-success")
    assert old_success["reason_code"] == "recovery.criterion_not_satisfied"
    assert service.fact_counts() == before

    with pytest.raises(QueryNotFound):
        service.verify_recovery(
            **{**command, "principal": REVIEWER},
            idempotency_key="s07-permission-only",
        )
    with pytest.raises(QueryNotFound):
        service.verify_recovery(
            **{
                **command,
                "principal": S01CommandPrincipal(
                    subject="other-tenant-operator",
                    role="operator",
                    scope="R-OBSERVED/OTHER",
                    source_id="other-console",
                ),
            },
            idempotency_key="s07-cross-scope",
        )
    assert service.fact_counts() == before

    verification.update(evidence_kind="compensation_receipt", verification_id="s07-ok")
    accepted = service.verify_recovery(**command, idempotency_key="s07-all-verified")
    assert accepted["status"] == "accepted"
    assert accepted["phase"] == "Evidence Ready"


def test_recovery_inbox_rejects_same_fact_identity_with_different_semantics(
    tmp_path: Path,
) -> None:
    def verifier(work: dict[str, object]) -> dict[str, object]:
        criterion = work["criterion"]
        assert isinstance(criterion, dict)
        return {
            "verification_id": "s07-shared-inbox-fact",
            "observed_at": int(work["opened_at"]) + 1,
            "evidence_kind": criterion["evidence_kind"],
            "scope": work["visibility_scope"],
            "recovery_work_id": work["recovery_work_id"],
            "criterion_digest": criterion["digest"],
            "conditions": [
                {
                    "condition_id": condition["condition_id"],
                    "verified": True,
                    "evidence_digest": "1" * 64,
                }
                for condition in work["conditions"]
            ],
        }

    service = _service(tmp_path, recovery_verifier=verifier)
    first_application_id = _admit(service)
    first_blocked = ControlledScenarioTestDriver(service).process_next_job(
        worker_id="s07-first-inbox-worker",
        now=100,
        operation_fault="checker_incompatible",
    )
    second_service = _service(
        tmp_path,
        recovery_verifier=verifier,
        scenario_id="app_bad_brand.json",
    )
    second_admission = second_service.submit_demo(
        scenario_id="app_bad_brand.json",
        idempotency_key="s07-second-admission",
        principal=INTAKE,
    )
    assert second_admission.application_id not in {None, first_application_id}
    second_blocked = ControlledScenarioTestDriver(second_service).process_next_job(
        worker_id="s07-second-inbox-worker",
        now=101,
        operation_fault="checker_incompatible",
    )
    assert first_blocked.recovery_work_id is not None
    assert second_blocked.recovery_work_id is not None
    first_view = service.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=first_blocked.recovery_work_id,
    )
    second_view = service.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=second_blocked.recovery_work_id,
    )
    first = service.verify_recovery(
        principal=OPERATOR,
        recovery_work_id=first_blocked.recovery_work_id,
        expected_lifecycle_revision=first_view["lifecycle_revision"],
        expected_criterion_digest=first_view["criterion"]["digest"],
        idempotency_key="s07-first-inbox-recovery",
    )
    assert first["status"] == "accepted"
    before = service.fact_counts()

    conflict = service.verify_recovery(
        principal=OPERATOR,
        recovery_work_id=second_blocked.recovery_work_id,
        expected_lifecycle_revision=second_view["lifecycle_revision"],
        expected_criterion_digest=second_view["criterion"]["digest"],
        idempotency_key="s07-second-inbox-recovery",
    )

    assert conflict == {
        "status": "conflict",
        "reason_code": "recovery.fact_identity_conflict",
        "replayed": False,
    }
    assert service.fact_counts() == before
    unresolved = service.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=second_blocked.recovery_work_id,
    )
    assert unresolved["status"] == "open"
    assert unresolved["recovery_fact_count"] == 0
    assert unresolved["resolution_count"] == 0
    resolved = service.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=first_blocked.recovery_work_id,
    )
    assert resolved["status"] == "resolved"
    assert resolved["recovery_fact_count"] == 1
    assert resolved["resolution_count"] == 1


def test_recovery_cas_race_accepts_one_fact_and_returns_stale_for_the_loser(
    tmp_path: Path,
) -> None:
    verifier_barrier = threading.Barrier(2)

    def verifier(fact_id: str):
        def verify(work: dict[str, object]) -> dict[str, object]:
            verifier_barrier.wait(timeout=5)
            criterion = work["criterion"]
            assert isinstance(criterion, dict)
            return {
                "verification_id": fact_id,
                "observed_at": int(work["opened_at"]) + 1,
                "evidence_kind": criterion["evidence_kind"],
                "scope": work["visibility_scope"],
                "recovery_work_id": work["recovery_work_id"],
                "criterion_digest": criterion["digest"],
                "conditions": [
                    {
                        "condition_id": condition["condition_id"],
                        "verified": True,
                        "evidence_digest": fact_id[-1] * 64,
                    }
                    for condition in work["conditions"]
                ],
            }

        return verify

    authority = _service(tmp_path)
    _admit(authority)
    blocked = ControlledScenarioTestDriver(authority).process_next_job(
        worker_id="s07-race-block-worker",
        now=120,
        operation_fault="checker_incompatible",
    )
    assert blocked.recovery_work_id is not None
    view = authority.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    )
    services = [
        _service(tmp_path, recovery_verifier=verifier("s07-race-fact-a")),
        _service(tmp_path, recovery_verifier=verifier("s07-race-fact-b")),
    ]
    results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def recover(index: int) -> None:
        try:
            results.append(
                services[index].verify_recovery(
                    principal=OPERATOR,
                    recovery_work_id=blocked.recovery_work_id or "",
                    expected_lifecycle_revision=view["lifecycle_revision"],
                    expected_criterion_digest=view["criterion"]["digest"],
                    idempotency_key=f"s07-race-command-{index}",
                )
            )
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    workers = [threading.Thread(target=recover, args=(index,)) for index in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert sorted(result["status"] for result in results) == ["accepted", "stale"]
    loser = next(result for result in results if result["status"] == "stale")
    assert loser["reason_code"] == "recovery.work_not_open"
    final = authority.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    )
    assert final["status"] == "resolved"
    assert final["recovery_fact_count"] == 1
    assert final["resolution_count"] == 1


def test_duplicate_recovery_fact_delivery_replays_the_same_semantic_result(
    tmp_path: Path,
) -> None:
    verifier_barrier = threading.Barrier(2)

    def verifier(work: dict[str, object]) -> dict[str, object]:
        verifier_barrier.wait(timeout=5)
        criterion = work["criterion"]
        assert isinstance(criterion, dict)
        return {
            "verification_id": "s07-duplicate-fact",
            "observed_at": int(work["opened_at"]) + 1,
            "evidence_kind": criterion["evidence_kind"],
            "scope": work["visibility_scope"],
            "recovery_work_id": work["recovery_work_id"],
            "criterion_digest": criterion["digest"],
            "conditions": [
                {
                    "condition_id": condition["condition_id"],
                    "verified": True,
                    "evidence_digest": "d" * 64,
                }
                for condition in work["conditions"]
            ],
        }

    authority = _service(tmp_path)
    _admit(authority)
    blocked = ControlledScenarioTestDriver(authority).process_next_job(
        worker_id="s07-duplicate-block-worker",
        now=130,
        operation_fault="checker_incompatible",
    )
    assert blocked.recovery_work_id is not None
    view = authority.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    )
    services = [
        _service(tmp_path, recovery_verifier=verifier),
        _service(tmp_path, recovery_verifier=verifier),
    ]
    results: dict[int, dict[str, object]] = {}
    errors: list[BaseException] = []

    def recover(index: int, service: ControlledScenarioService) -> None:
        try:
            results[index] = service.verify_recovery(
                principal=OPERATOR,
                recovery_work_id=blocked.recovery_work_id or "",
                expected_lifecycle_revision=view["lifecycle_revision"],
                expected_criterion_digest=view["criterion"]["digest"],
                idempotency_key=f"s07-duplicate-delivery-{index}",
            )
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    workers = [
        threading.Thread(target=recover, args=(index, service))
        for index, service in enumerate(services)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    original = next(
        result for result in results.values() if result["replayed"] is False
    )
    duplicate_index, duplicate = next(
        (index, result)
        for index, result in results.items()
        if result["replayed"] is True
    )
    assert duplicate == {**original, "replayed": True}
    assert original["status"] == "accepted"
    assert original["recovery_fact_id"] == "s07-duplicate-fact"
    replay = services[duplicate_index].verify_recovery(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
        expected_lifecycle_revision=view["lifecycle_revision"],
        expected_criterion_digest=view["criterion"]["digest"],
        idempotency_key=f"s07-duplicate-delivery-{duplicate_index}",
    )
    assert replay == duplicate
    before_sequential = authority.fact_counts()
    before_work = authority.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    )

    def sequential_verifier(work: dict[str, object]) -> dict[str, object]:
        criterion = work["criterion"]
        assert isinstance(criterion, dict)
        return {
            "verification_id": "s07-duplicate-fact",
            "observed_at": int(work["opened_at"]) + 1,
            "evidence_kind": criterion["evidence_kind"],
            "scope": work["visibility_scope"],
            "recovery_work_id": work["recovery_work_id"],
            "criterion_digest": criterion["digest"],
            "conditions": [
                {
                    "condition_id": condition["condition_id"],
                    "verified": True,
                    "evidence_digest": "d" * 64,
                }
                for condition in work["conditions"]
            ],
        }

    fresh = _service(tmp_path, recovery_verifier=sequential_verifier)
    sequential = fresh.verify_recovery(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
        expected_lifecycle_revision=view["lifecycle_revision"],
        expected_criterion_digest=view["criterion"]["digest"],
        idempotency_key="s07-duplicate-delivery-fresh",
    )
    assert sequential == duplicate
    assert fresh.fact_counts() == before_sequential
    assert fresh.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    ) == before_work
    final = authority.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    )
    assert final["status"] == "resolved"
    assert final["recovery_fact_count"] == 1
    assert final["resolution_count"] == 1


def test_timeout_reconciles_the_same_operation_before_retry_or_blocking(
    tmp_path: Path,
) -> None:
    queries: list[dict[str, object]] = []
    engine = RuleEngine(load_rules(ROOT / "configs" / "rules_auto_lease.yaml"))

    def status_query(request: dict[str, object]) -> dict[str, object]:
        queries.append(request)
        if len(queries) == 1:
            return {
                "status": "not_committed",
                "logical_operation_id": request["logical_operation_id"],
            }
        return {
            "status": "committed",
            "logical_operation_id": request["logical_operation_id"],
            "result_id": "s07-authoritative-result-1",
            "result_digest": "c" * 64,
            "result": engine.run(request["application"]),
        }

    def unexpected_checker(_: object) -> object:
        raise AssertionError("committed reconciliation must not execute a new check")

    service = _service(
        tmp_path,
        checker_runner=unexpected_checker,
        checker_status_query=status_query,
    )
    application_id = _admit(service)
    driver = ControlledScenarioTestDriver(service)
    retried = driver.process_next_job(
        worker_id="s07-timeout-worker",
        now=0,
        operation_fault="checker_timeout",
    )
    completed = driver.process_next_job(
        worker_id="s07-timeout-worker",
        now=1,
        operation_fault="checker_timeout",
    )

    assert (retried.status, retried.retry_after_seconds) == ("retry_wait", 1)
    assert completed.status == "complete"
    assert completed.job_id == retried.job_id
    assert completed.recovery_work_id is None
    assert "Unprocessable" not in completed.lifecycle_phases
    assert completed.reconciliation == {
        "status": "committed",
        "logical_operation_id": retried.job_id,
        "result_id": "s07-authoritative-result-1",
        "result_digest": "c" * 64,
    }
    assert len(queries) == 2
    assert {query["logical_operation_id"] for query in queries} == {retried.job_id}
    assert len({query["semantic_idempotency_identity"] for query in queries}) == 1
    assert {query["run_id"] for query in queries} == {completed.run_id}
    assert [query["attempt"] for query in queries] == [1, 2]
    assert all("idempotency_key" not in query for query in queries)

    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    complete_runs = [run for run in history["runs"] if run["status"] == "complete"]
    assert len(complete_runs) == 1
    assert complete_runs[0]["run_id"] == completed.run_id
    assert complete_runs[0]["current"] is True
    assert complete_runs[0]["reconciliation"] == completed.reconciliation
    findings_after_commit = service.fact_counts()["findings"]
    duplicate = driver.process_next_job(
        worker_id="s07-duplicate-worker",
        now=2,
        duplicate=True,
    )
    assert duplicate.status == "duplicate"
    assert duplicate.run_id == completed.run_id
    assert service.fact_counts()["findings"] == findings_after_commit

    unknown_service = _service(
        tmp_path / "unknown",
        checker_status_query=lambda request: {
            "status": "unknown",
            "logical_operation_id": request["logical_operation_id"],
        },
    )
    _admit(unknown_service)
    unknown = ControlledScenarioTestDriver(unknown_service).process_next_job(
        worker_id="s07-timeout-worker",
        now=5,
        operation_fault="checker_timeout",
    )
    assert unknown.status == "blocked"
    assert unknown.reason_code == "check.outcome_unknown"
    assert unknown.recovery_work_id is not None
    unknown_view = unknown_service.recovery_work_view(
        principal=REVIEWER,
        recovery_work_id=unknown.recovery_work_id,
    )
    assert unknown_view["outcome_known"] is False
    assert unknown_view["job_status"] == "outcome_unknown"
    assert unknown_view["related_reason_codes"] == ["operation.status_unavailable"]


def test_invalid_or_mismatched_timeout_status_is_minimized_unknown_work(
    tmp_path: Path,
) -> None:
    responses = (
        lambda request: {
            "status": "committed",
            "logical_operation_id": "another-operation",
            "result_id": "untrusted-result",
            "result_digest": "d" * 64,
            "result": {"restricted": "must-not-leak"},
        },
        lambda request: {
            "status": "exactly_once",
            "logical_operation_id": request["logical_operation_id"],
        },
    )
    for index, status_query in enumerate(responses):
        service = _service(
            tmp_path / str(index),
            checker_status_query=status_query,
        )
        _admit(service)
        blocked = ControlledScenarioTestDriver(service).process_next_job(
            worker_id="s07-timeout-worker",
            now=30,
            operation_fault="checker_timeout",
        )

        assert blocked.status == "blocked"
        assert blocked.reason_code == "check.outcome_unknown"
        assert blocked.reconciliation == {
            "status": "unknown",
            "logical_operation_id": blocked.job_id,
        }
        assert service.fact_counts()["findings"] == 0
        assert blocked.recovery_work_id is not None
        view = service.recovery_work_view(
            principal=REVIEWER,
            recovery_work_id=blocked.recovery_work_id,
        )
        assert view["outcome_known"] is False
        assert "restricted" not in json.dumps(view, sort_keys=True)


def test_publication_failure_is_atomic_and_authority_stop_reconciles_later(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    application_id = _admit(service)

    failed = ControlledScenarioTestDriver(service).process_next_job(
        worker_id="s07-publication-worker",
        now=40,
        operation_fault="result_publication_audit",
    )
    assert failed.status == "blocked"
    assert failed.reason_code == "control.audit_unavailable"
    assert failed.recovery_work_id is not None
    blocked_view = service.recovery_work_view(
        principal=REVIEWER,
        recovery_work_id=failed.recovery_work_id,
    )
    assert blocked_view["phase"] == "Unprocessable"
    assert blocked_view["protected_business_revision"] == 0
    assert blocked_view["current_run_id"] is None
    assert service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )["phase"] == "Unprocessable"

    class FailOnce:
        fired = False

        def __call__(self, point: str) -> None:
            if point == "s07.failure.audit" and not self.fired:
                self.fired = True
                raise OSError("audit authority unavailable")

    authority = _service(tmp_path / "authority", fault_injector=FailOnce())
    _admit(authority)
    authority_failure = ControlledScenarioTestDriver(authority).process_next_job(
        worker_id="s07-authority-worker",
        now=40,
        operation_fault="result_publication_audit",
    )
    assert authority_failure.status == "authority_unavailable"
    assert authority_failure.recovery_work_id is None
    assert authority.current_route_view(
        principal=REVIEWER,
        application_id=authority_failure.application_id or "",
    )["phase"] == "Checking"
    assert authority.fact_counts()["findings"] == 0

    reconciled = ControlledScenarioTestDriver(authority).process_next_job(
        worker_id="s07-authority-worker",
        now=41,
        operation_fault="result_publication_audit",
    )
    assert reconciled.status == "blocked"
    assert reconciled.recovery_work_id is not None
    reconciled_view = authority.recovery_work_view(
        principal=REVIEWER,
        recovery_work_id=reconciled.recovery_work_id,
    )
    assert reconciled_view["phase"] == "Unprocessable"
    assert reconciled_view["attempts"] == [
        {
            "attempt": 1,
            "classification": "authority_unavailable",
            "status": "reconcile_wait",
            "started_at": 40,
            "retry_not_before": 41,
        },
        {
            "attempt": 2,
            "classification": "terminal",
            "status": "blocked",
            "started_at": 41,
            "retry_not_before": None,
        },
    ]
    assert authority.fact_counts()["findings"] == 0


def test_persistent_failure_publication_fault_stops_then_reconciles_without_reexecution(
    tmp_path: Path,
) -> None:
    class PersistentPublicationFault:
        enabled = True

        def __call__(self, point: str) -> None:
            if self.enabled and point == "s07.failure.audit":
                raise OSError("audit authority unavailable")

    checker_calls: list[str] = []

    def checker_must_not_run(application: object) -> object:
        checker_calls.append(str(application))
        raise AssertionError("failure publication must not re-execute the checker")

    fault = PersistentPublicationFault()
    service = _service(
        tmp_path,
        fault_injector=fault,
        checker_runner=checker_must_not_run,
    )
    application_id = _admit(service)
    driver = ControlledScenarioTestDriver(service)

    first = driver.process_next_job(
        worker_id="s07-publication-worker",
        now=40,
        operation_fault="checker_incompatible",
    )
    assert first.status == "authority_unavailable"
    assert first.reason_code == "control.failure_publication_unavailable"
    assert first.retry_after_seconds == 1
    assert first.reconciliation == {
        "status": "failure_publication_pending",
        "logical_operation_id": first.job_id,
        "attempt": 1,
        "max_attempts": 3,
    }
    assert first.recovery_work_id is None
    first_counts = service.fact_counts()
    first_history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert first_counts["attempts"] == 1
    assert first_counts["runs"] == 1
    assert first_counts["findings"] == 0
    assert len(first_history["runs"]) == 1
    original_run_id = first_history["runs"][0]["run_id"]
    original_snapshot_id = first_history["runs"][0]["evidence_snapshot_id"]
    assert service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )["phase"] == "Checking"

    too_early = driver.process_next_job(
        worker_id="s07-publication-worker",
        now=40,
        operation_fault="checker_incompatible",
    )
    second = driver.process_next_job(
        worker_id="s07-publication-worker",
        now=41,
        operation_fault="checker_incompatible",
    )
    assert too_early.status == "idle"
    assert second.status == "authority_unavailable"
    assert second.retry_after_seconds == 2
    assert second.reconciliation == {
        "status": "failure_publication_pending",
        "logical_operation_id": first.job_id,
        "attempt": 2,
        "max_attempts": 3,
    }
    assert service.fact_counts() == first_counts

    too_early_again = driver.process_next_job(
        worker_id="s07-publication-worker",
        now=42,
        operation_fault="checker_incompatible",
    )
    exhausted = driver.process_next_job(
        worker_id="s07-publication-worker",
        now=43,
        operation_fault="checker_incompatible",
    )
    assert too_early_again.status == "idle"
    assert exhausted.status == "stopped"
    assert exhausted.reason_code == "control.failure_publication_exhausted"
    assert exhausted.retry_after_seconds == 0
    assert exhausted.reconciliation == {
        "status": "failure_publication_exhausted",
        "logical_operation_id": first.job_id,
        "attempt": 3,
        "max_attempts": 3,
    }
    assert exhausted.recovery_work_id is None
    exhausted_counts = service.fact_counts()
    assert exhausted_counts == {
        **first_counts,
        "audit_events": first_counts["audit_events"] + 1,
    }
    assert service.cohort_status() == {
        "track": "C-DEMO",
        "admission": "stopped",
        "reason_code": "S01_RUNTIME_UNHEALTHY",
        "failure_reason_code": "control.failure_publication_exhausted",
    }
    assert service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )["phase"] == "Checking"

    stable = driver.process_next_job(
        worker_id="s07-publication-worker",
        now=100,
        operation_fault="checker_incompatible",
    )
    assert stable.status == "stopped"
    assert stable.reason_code == "control.failure_publication_exhausted"
    assert service.fact_counts() == exhausted_counts

    restarted = _service(
        tmp_path,
        fault_injector=fault,
        checker_runner=checker_must_not_run,
    )
    assert restarted.cohort_status() == service.cohort_status()
    assert restarted.fact_counts() == exhausted_counts
    assert ControlledScenarioTestDriver(restarted).process_next_job(
        worker_id="s07-publication-worker",
        now=101,
        operation_fault="checker_incompatible",
    ).status == "stopped"
    assert restarted.fact_counts() == exhausted_counts

    unverified = restarted.recover_runtime(
        principal=OPERATOR,
        expected_failure_reason_code="control.failure_publication_exhausted",
    )
    assert unverified["recovery"] == "rejected"
    assert unverified["reason_code"] == "S01_RUNTIME_REPAIR_NOT_VERIFIED"
    assert unverified["requeued_jobs"] == 0
    assert restarted.fact_counts() == exhausted_counts

    fault.enabled = False
    recovered = restarted.recover_runtime(
        principal=OPERATOR,
        expected_failure_reason_code="control.failure_publication_exhausted",
    )
    assert recovered["recovery"] == "scheduled"
    assert recovered["requeued_jobs"] == 1
    reconciled = ControlledScenarioTestDriver(restarted).process_next_job(
        worker_id="s07-publication-worker",
        now=102,
    )
    assert reconciled.status == "blocked"
    assert reconciled.reason_code == "configuration.checker_unavailable"
    assert reconciled.job_id == first.job_id
    assert reconciled.run_id == original_run_id
    assert reconciled.recovery_work_id is not None
    final_counts = restarted.fact_counts()
    assert final_counts["attempts"] == first_counts["attempts"]
    assert final_counts["evidence_events"] == first_counts["evidence_events"]
    assert final_counts["findings"] == 0
    final_history = restarted.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert {run["run_id"] for run in final_history["runs"]} == {original_run_id}
    assert {run["evidence_snapshot_id"] for run in final_history["runs"]} == {
        original_snapshot_id
    }
    work = restarted.recovery_work_view(
        principal=REVIEWER,
        recovery_work_id=reconciled.recovery_work_id,
    )
    assert work["phase"] == "Unprocessable"
    assert work["logical_operation_id"] == first.job_id
    assert work["protected_business_revision"] == 0
    assert checker_calls == []


def test_object_storage_recovery_reenters_assembly_without_mutating_evidence(
    tmp_path: Path,
) -> None:
    def verifier(work: dict[str, object]) -> dict[str, object]:
        return {
            "verification_id": "s07-object-binding-fact-1",
            "observed_at": int(work["opened_at"]) + 1,
            "evidence_kind": "object_storage_binding_probe",
            "scope": work["visibility_scope"],
            "recovery_work_id": work["recovery_work_id"],
            "criterion_digest": work["criterion"]["digest"],
            "conditions": [
                {
                    "condition_id": condition["condition_id"],
                    "verified": True,
                    "evidence_digest": "e" * 64,
                }
                for condition in work["conditions"]
            ],
        }

    service = _service(tmp_path, recovery_verifier=verifier)
    _admit(service)
    blocked = ControlledScenarioTestDriver(service).process_next_job(
        worker_id="s07-object-worker",
        now=50,
        operation_fault="object_storage_unavailable",
    )
    assert blocked.status == "blocked"
    assert blocked.recovery_work_id is not None
    view = service.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    )
    assert view["primary_reason_code"] == "control.storage_unavailable"
    assert view["dependency"] == "c-demo-object-store"
    assert view["recovery_target"] == "Assembly"
    assert view["evidence_revision"] == 1
    assert view["protected_business_revision"] == 0
    before_evidence_events = service.fact_counts()["evidence_events"]

    accepted = service.verify_recovery(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
        expected_lifecycle_revision=view["lifecycle_revision"],
        expected_criterion_digest=view["criterion"]["digest"],
        idempotency_key="s07-object-recovery",
    )
    assert accepted["status"] == "accepted"
    assert accepted["phase"] == "Assembly"
    assert accepted["evidence_revision"] == 1
    assert service.fact_counts()["evidence_events"] == before_evidence_events

    rerun = ControlledScenarioTestDriver(service).process_next_job(
        worker_id="s07-object-recovery-worker",
        now=52,
    )
    assert rerun.status == "complete"
    assert rerun.fence > blocked.fence
    assert rerun.lifecycle_phases[-4:] == (
        "Evidence Ready",
        "Checking",
        "Routing Determination",
        "Manual Review",
    )


def test_restart_rebuilds_recovery_job_only_while_its_gate_is_current(
    tmp_path: Path,
) -> None:
    def verifier(work: dict[str, object]) -> dict[str, object]:
        return {
            "verification_id": "s07-restart-fact-1",
            "observed_at": int(work["opened_at"]) + 1,
            "evidence_kind": "checker_compatibility_probe",
            "scope": work["visibility_scope"],
            "recovery_work_id": work["recovery_work_id"],
            "criterion_digest": work["criterion"]["digest"],
            "conditions": [
                {
                    "condition_id": condition["condition_id"],
                    "verified": True,
                    "evidence_digest": "f" * 64,
                }
                for condition in work["conditions"]
            ],
        }

    service = _service(tmp_path, recovery_verifier=verifier)
    application_id = _admit(service)
    blocked = ControlledScenarioTestDriver(service).process_next_job(
        worker_id="s07-restart-worker",
        now=60,
        operation_fault="checker_incompatible",
    )
    assert blocked.recovery_work_id is not None
    view = service.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    )
    recovered = service.verify_recovery(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
        expected_lifecycle_revision=view["lifecycle_revision"],
        expected_criterion_digest=view["criterion"]["digest"],
        idempotency_key="s07-restart-recovery",
    )

    state_path = tmp_path / "target.sqlite3"
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "DELETE FROM jobs WHERE item_id = ?", (recovered["successor_job_id"],)
        )
        connection.commit()
    restarted = _service(tmp_path, recovery_verifier=verifier)
    rerun = ControlledScenarioTestDriver(restarted).process_next_job(
        worker_id="s07-rebuilt-worker",
        now=62,
    )
    assert rerun.status == "complete"
    assert rerun.application_id == application_id
    assert rerun.job_id == recovered["successor_job_id"]
    assert rerun.fence == recovered["successor_fence"] + 1

    counts_after_complete = restarted.fact_counts()
    history_after_complete = restarted.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    route_after_complete = restarted.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    work_after_complete = restarted.recovery_work_view(
        principal=REVIEWER,
        recovery_work_id=blocked.recovery_work_id,
    )
    assert work_after_complete["status"] == "resolved"
    assert work_after_complete["recovery_fact_count"] == 1
    assert work_after_complete["resolution_count"] == 1

    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "DELETE FROM jobs WHERE item_id = ?", (recovered["successor_job_id"],)
        )
        connection.commit()
    completed_restart = _service(tmp_path, recovery_verifier=verifier)
    counts_after_delete = completed_restart.fact_counts()
    assert counts_after_delete == {
        **counts_after_complete,
        "jobs": counts_after_complete["jobs"] - 1,
    }
    idle = ControlledScenarioTestDriver(completed_restart).process_next_job(
        worker_id="s07-must-stay-idle",
        now=93,
    )
    assert (idle.status, idle.reason_code) == ("idle", "NO_READY_JOB")
    assert completed_restart.fact_counts() == counts_after_delete
    assert completed_restart.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    ) == history_after_complete
    assert completed_restart.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    ) == route_after_complete
    assert completed_restart.recovery_work_view(
        principal=REVIEWER,
        recovery_work_id=blocked.recovery_work_id,
    ) == work_after_complete


def test_recovery_successor_fences_late_old_worker_result(
    tmp_path: Path,
) -> None:
    checker_entered = threading.Event()
    release_checker = threading.Event()
    delegate = RuleEngine(load_rules(ROOT / "configs" / "rules_auto_lease.yaml"))

    def blocking_checker(application: object) -> object:
        checker_entered.set()
        assert release_checker.wait(timeout=5)
        return delegate.run(application)  # type: ignore[arg-type]

    def verifier(work: dict[str, object]) -> dict[str, object]:
        criterion = work["criterion"]
        assert isinstance(criterion, dict)
        return {
            "verification_id": "s07-late-worker-recovery-fact",
            "observed_at": int(work["opened_at"]) + 1,
            "evidence_kind": criterion["evidence_kind"],
            "scope": work["visibility_scope"],
            "recovery_work_id": work["recovery_work_id"],
            "criterion_digest": criterion["digest"],
            "conditions": [
                {
                    "condition_id": condition["condition_id"],
                    "verified": True,
                    "evidence_digest": "9" * 64,
                }
                for condition in work["conditions"]
            ],
        }

    first = _service(
        tmp_path,
        checker_runner=blocking_checker,
        worker_identity="s07-old-worker",
        clock=lambda: 0,
    )
    application_id = _admit(first)
    late_results: list[object] = []
    errors: list[BaseException] = []

    def run_old_worker() -> None:
        try:
            late_results.append(first.process_next_job())
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    worker = threading.Thread(target=run_old_worker)
    worker.start()
    assert checker_entered.wait(timeout=5)

    takeover = _service(
        tmp_path,
        recovery_verifier=verifier,
        worker_identity="s07-takeover-worker",
    )
    blocked = ControlledScenarioTestDriver(takeover).process_next_job(
        worker_id="s07-takeover-worker",
        now=31,
        operation_fault="checker_incompatible",
    )
    assert blocked.status == "blocked"
    assert blocked.recovery_work_id is not None
    view = takeover.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    )
    accepted = takeover.verify_recovery(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
        expected_lifecycle_revision=view["lifecycle_revision"],
        expected_criterion_digest=view["criterion"]["digest"],
        idempotency_key="s07-late-worker-recovery",
    )
    assert accepted["status"] == "accepted"
    successor_job_id = accepted["successor_job_id"]
    before_counts = takeover.fact_counts()
    before_work = takeover.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    )

    release_checker.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []
    assert len(late_results) == 1
    late = late_results[0]
    assert late.status == "stale"  # type: ignore[union-attr]
    assert late.reason_code == "STALE_COMPARE_AND_SET"  # type: ignore[union-attr]

    fresh = _service(tmp_path, recovery_verifier=verifier)
    assert fresh.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )["current_run_id"] is None
    assert fresh.fact_counts()["findings"] == before_counts["findings"]
    assert fresh.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    ) == before_work
    successor = ControlledScenarioTestDriver(fresh).process_next_job(
        worker_id="s07-fresh-successor-worker",
        now=32,
        crash=True,
    )
    assert successor.status == "crashed"
    assert successor.job_id == successor_job_id
    assert successor.fence > blocked.fence


@pytest.mark.parametrize("attempted", (False, True))
def test_restart_rejects_a_mutated_recovery_successor(
    tmp_path: Path,
    attempted: bool,
) -> None:
    def verifier(work: dict[str, object]) -> dict[str, object]:
        criterion = work["criterion"]
        assert isinstance(criterion, dict)
        return {
            "verification_id": "s07-mutated-successor-fact",
            "observed_at": int(work["opened_at"]) + 1,
            "evidence_kind": criterion["evidence_kind"],
            "scope": work["visibility_scope"],
            "recovery_work_id": work["recovery_work_id"],
            "criterion_digest": criterion["digest"],
            "conditions": [
                {
                    "condition_id": condition["condition_id"],
                    "verified": True,
                    "evidence_digest": "8" * 64,
                }
                for condition in work["conditions"]
            ],
        }

    service = _service(tmp_path, recovery_verifier=verifier)
    application_id = _admit(service)
    blocked = ControlledScenarioTestDriver(service).process_next_job(
        worker_id="s07-mutated-successor-block-worker",
        now=70,
        operation_fault="checker_incompatible",
    )
    assert blocked.recovery_work_id is not None
    view = service.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    )
    recovered = service.verify_recovery(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
        expected_lifecycle_revision=view["lifecycle_revision"],
        expected_criterion_digest=view["criterion"]["digest"],
        idempotency_key="s07-mutated-successor-recovery",
    )
    if attempted:
        crashed = ControlledScenarioTestDriver(service).process_next_job(
            worker_id="s07-mutated-successor-crash-worker",
            now=72,
            crash=True,
        )
        assert crashed.status == "crashed"
        assert crashed.job_id == recovered["successor_job_id"]
    before = service.fact_counts()

    state_path = tmp_path / "target.sqlite3"
    with sqlite3.connect(state_path) as connection:
        row = connection.execute(
            "SELECT payload FROM jobs WHERE item_id = ?",
            (recovered["successor_job_id"],),
        ).fetchone()
        assert row is not None
        payload = json.loads(row[0])
        payload["attempt_no"] = 99
        payload["fence"] = 99
        connection.execute(
            "UPDATE jobs SET payload = ? WHERE item_id = ?",
            (
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                recovered["successor_job_id"],
            ),
        )
        connection.commit()

    restarted = _service(tmp_path, recovery_verifier=verifier)
    stopped = ControlledScenarioTestDriver(restarted).process_next_job(
        worker_id="s07-must-not-run-mutated-successor",
        now=103,
    )

    assert stopped.status == "stopped"
    assert stopped.reason_code == "ADMISSION_JOB_RECOVERY_UNAVAILABLE"
    assert restarted.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )["current_run_id"] is None
    assert restarted.fact_counts()["findings"] == before["findings"]
    resolved = restarted.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    )
    assert resolved["status"] == "resolved"
    assert resolved["recovery_fact_count"] == 1
    assert resolved["resolution_count"] == 1


@pytest.mark.parametrize(
    "fault_point",
    ("s07.recovery.audit", "s07.recovery.publish"),
)
def test_recovery_transaction_failure_has_no_partial_gate_claim(
    tmp_path: Path,
    fault_point: str,
) -> None:
    class FailOnce:
        fired = False

        def __call__(self, point: str) -> None:
            if point == fault_point and not self.fired:
                self.fired = True
                raise OSError("recovery authority unavailable")

    def verifier(work: dict[str, object]) -> dict[str, object]:
        criterion = work["criterion"]
        assert isinstance(criterion, dict)
        return {
            "verification_id": f"s07-atomic-{fault_point}-fact",
            "observed_at": int(work["opened_at"]) + 1,
            "evidence_kind": criterion["evidence_kind"],
            "scope": work["visibility_scope"],
            "recovery_work_id": work["recovery_work_id"],
            "criterion_digest": criterion["digest"],
            "conditions": [
                {
                    "condition_id": condition["condition_id"],
                    "verified": True,
                    "evidence_digest": "7" * 64,
                }
                for condition in work["conditions"]
            ],
        }

    service = _service(
        tmp_path,
        fault_injector=FailOnce(),
        recovery_verifier=verifier,
    )
    application_id = _admit(service)
    blocked = ControlledScenarioTestDriver(service).process_next_job(
        worker_id="s07-atomic-recovery-block-worker",
        now=80,
        operation_fault="checker_incompatible",
    )
    assert blocked.recovery_work_id is not None
    view = service.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    )
    before_counts = service.fact_counts()
    command = {
        "principal": OPERATOR,
        "recovery_work_id": blocked.recovery_work_id,
        "expected_lifecycle_revision": view["lifecycle_revision"],
        "expected_criterion_digest": view["criterion"]["digest"],
        "idempotency_key": f"s07-atomic-{fault_point}",
    }

    unavailable = service.verify_recovery(**command)

    assert unavailable == {
        "status": "unavailable",
        "reason_code": "recovery.authority_unavailable",
        "replayed": False,
    }
    assert service.fact_counts() == before_counts
    assert service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )["phase"] == "Unprocessable"
    unchanged = service.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    )
    assert unchanged["status"] == "open"
    assert unchanged["recovery_fact_count"] == 0
    assert unchanged["resolution_count"] == 0

    accepted = service.verify_recovery(**command)
    assert accepted["status"] == "accepted"
    assert accepted["replayed"] is False


def test_projection_replay_cannot_resurrect_review_work_after_unprocessable(
    tmp_path: Path,
) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    work_item = service.review_work_item_view(
        principal=CORRECTION_REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    corrected = service.correct_field_observation(
        principal=CORRECTION_REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=work_item["command_context"],
        idempotency_key="s07-projection-lag-correction",
        correction=correction,
        now=101,
    )
    blocked = ControlledScenarioTestDriver(service).process_next_job(
        worker_id="s07-projection-lag-worker",
        now=102,
        operation_fault="checker_incompatible",
    )
    assert corrected["status"] == "accepted"
    assert blocked.status == "blocked"
    assert blocked.recovery_work_id is not None

    old_delivery = next(
        event
        for event in service._store.outbox
        if event.get("kind") == "review_projection_requested"
        and event.get("application_id") == application_id
    )
    service._store.outbox.append(
        {
            **copy.deepcopy(old_delivery),
            "event_id": "s07-projection-lag-replayed-delivery",
            "status": "pending",
        }
    )
    service._store.persist()
    route_before = service.current_route_view(
        principal=CORRECTION_REVIEWER,
        application_id=application_id,
    )
    recovery_before = service.recovery_work_view(
        principal=CORRECTION_REVIEWER,
        recovery_work_id=blocked.recovery_work_id,
    )

    replayed = _service(tmp_path)
    replayed.refresh_projection()

    queue = replayed.queue_view(
        role="reviewer",
        scope=CORRECTION_REVIEWER.scope,
        subject=CORRECTION_REVIEWER.subject,
        now=103,
    )
    assert queue["items"] == []
    assert [item["recovery_work_id"] for item in queue["recovery_items"]] == [
        blocked.recovery_work_id
    ]
    queue_recovery = queue["recovery_items"][0]
    assert queue_recovery["status"] == "open"
    assert queue_recovery["phase"] == "Unprocessable"
    assert (
        queue_recovery["lifecycle_revision"]
        == recovery_before["lifecycle_revision"]
    )
    with pytest.raises(QueryNotFound):
        replayed.workspace_view(
            application_id,
            role="reviewer",
            scope=CORRECTION_REVIEWER.scope,
            subject=CORRECTION_REVIEWER.subject,
            now=103,
        )
    assert replayed.current_route_view(
        principal=CORRECTION_REVIEWER,
        application_id=application_id,
    ) == route_before
    recovery_after = replayed.recovery_work_view(
        principal=CORRECTION_REVIEWER,
        recovery_work_id=blocked.recovery_work_id,
    )
    assert recovery_after["recovery_fact_count"] == recovery_before[
        "recovery_fact_count"
    ]
    assert recovery_after["resolution_count"] == recovery_before["resolution_count"]


@pytest.mark.parametrize("winner", ("attachment", "recovery"))
def test_later_attachment_successor_and_recovery_fact_have_one_lifecycle_cas_winner(
    tmp_path: Path,
    winner: str,
) -> None:
    service, application_id, _, request, source = _ready_supplement_request(tmp_path)
    service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key=f"s07-context-race-progress-{winner}",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    fulfilled = service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=True),
        idempotency_key=f"s07-context-race-fulfillment-{winner}",
        principal=SUPPLEMENT_INTEGRATOR,
        now=201,
    )
    blocked = ControlledScenarioTestDriver(service).process_next_job(
        worker_id=f"s07-context-race-worker-{winner}",
        now=202,
        operation_fault="checker_incompatible",
    )
    assert blocked.recovery_work_id is not None
    work = service.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    )
    assert work["status"] == "open"
    assert work["phase"] == "Unprocessable"
    assert work["recovery_target"] == "Evidence Ready"

    successor = copy.deepcopy(_attachment_submission(request, source, closed=True))
    successor["envelope_id"] = f"s07-context-successor-{winner}"
    successor["source_revision"] = int(fulfilled.source_revision or 0) + 1
    successor["predecessor_revision"] = fulfilled.source_revision
    successor["request_binding"]["request_progress_revision"] = (  # type: ignore[index]
        int(fulfilled.request_progress_revision or 0) + 1
    )
    successor["attachment_lineage"].update(  # type: ignore[union-attr]
        {
            "predecessor_attachment_id": fulfilled.attachment_id,
            "predecessor_attachment_version": fulfilled.attachment_version,
            "attachment_version": int(fulfilled.attachment_version or 0) + 1,
        }
    )

    def verifier(recovery: dict[str, object]) -> dict[str, object]:
        criterion = recovery["criterion"]
        assert isinstance(criterion, dict)
        return {
            "verification_id": f"s07-context-race-fact-{winner}",
            "observed_at": int(recovery["opened_at"]) + 1,
            "evidence_kind": criterion["evidence_kind"],
            "scope": recovery["visibility_scope"],
            "recovery_work_id": recovery["recovery_work_id"],
            "criterion_digest": criterion["digest"],
            "conditions": [
                {
                    "condition_id": condition["condition_id"],
                    "verified": True,
                    "evidence_digest": "f" * 64,
                }
                for condition in recovery["conditions"]
            ],
        }

    staged = threading.Barrier(2)
    release_loser = threading.Event()
    losing_point = (
        "s07.recovery.publish"
        if winner == "attachment"
        else "supplement_progress.publish"
    )

    def block_loser(write_point: str) -> None:
        if write_point != losing_point:
            return
        staged.wait(timeout=5)
        if not release_loser.wait(timeout=5):
            raise TimeoutError("S07 context/recovery CAS loser was not released")

    attachment_service = _supplement_service(
        tmp_path,
        source,
        fault_injector=block_loser if winner == "recovery" else None,
    )
    recovery_service = _service(
        tmp_path,
        fault_injector=block_loser if winner == "attachment" else None,
        recovery_verifier=verifier,
    )

    def submit_successor() -> object:
        return attachment_service.submit_attachment_version(
            submission=successor,
            idempotency_key=f"s07-context-successor-command-{winner}",
            principal=SUPPLEMENT_INTEGRATOR,
            now=203,
        )

    def verify() -> dict[str, object]:
        return recovery_service.verify_recovery(
            principal=OPERATOR,
            recovery_work_id=blocked.recovery_work_id or "",
            expected_lifecycle_revision=work["lifecycle_revision"],
            expected_criterion_digest=work["criterion"]["digest"],
            idempotency_key=f"s07-context-race-verify-{winner}",
        )

    loser_command = verify if winner == "attachment" else submit_successor
    winner_command = submit_successor if winner == "attachment" else verify
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(loser_command)
        staged.wait(timeout=5)
        try:
            winning_result = winner_command()
        finally:
            release_loser.set()
        losing_result = future.result(timeout=5)

    attachment_result = winning_result if winner == "attachment" else losing_result
    recovery_result = losing_result if winner == "attachment" else winning_result
    final = service.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    )
    history = service.application_history_view(
        principal=S06_REVIEWER,
        application_id=application_id,
    )
    route = service.current_route_view(
        principal=S06_REVIEWER,
        application_id=application_id,
    )

    if winner == "attachment":
        assert attachment_result.disposition is AdmissionDisposition.ACCEPTED
        assert attachment_result.phase == "Assembly"
        assert attachment_result.evidence_revision == work["evidence_revision"] + 1
        assert recovery_result == {
            "status": "stale",
            "reason_code": "recovery.work_not_open",
            "replayed": False,
        }
        assert final["status"] == "superseded"
        assert final["recovery_fact_count"] == final["resolution_count"] == 0
        assert [item["version"] for item in history["attachment_versions"]] == [
            1,
            2,
            3,
        ]
        assert route["phase"] == "Assembly"
        before_replay = service.fact_counts()
        replay = _supplement_service(tmp_path, source).submit_attachment_version(
            submission=successor,
            idempotency_key="s07-context-successor-fresh-replay",
            principal=SUPPLEMENT_INTEGRATOR,
            now=204,
        )
        assert replay.receipt_id == attachment_result.receipt_id
        assert replay.replayed is True
        assert service.fact_counts() == before_replay
    else:
        assert recovery_result["status"] == "accepted"
        assert recovery_result["phase"] == "Evidence Ready"
        assert attachment_result.disposition is AdmissionDisposition.REJECTED
        assert attachment_result.reason_code == "evidence.late_input_requires_reopen"
        assert final["status"] == "resolved"
        assert final["recovery_fact_count"] == final["resolution_count"] == 1
        assert [item["version"] for item in history["attachment_versions"]] == [1, 2]
        assert route["phase"] == "Evidence Ready"
    assert route["phase"] != "Verification Completed"
    assert route["current_run_id"] is None


def test_routing_failure_recovers_only_through_the_normal_routing_gate(
    tmp_path: Path,
) -> None:
    service, application_id, work_item_id, claim, finding = _ready_brand_exception(
        tmp_path
    )
    request = _request_brand_exception(
        service, work_item_id, claim, finding, key="s07-routing-request"
    )
    decision = _approve_brand_exception(service, request, now=103)
    fault_points: list[str] = []
    dependency_available = False

    def fail(point: str) -> None:
        nonlocal dependency_available
        fault_points.append(point)
        if point == "exception_route.operation" and not dependency_available:
            dependency_available = True
            raise OSError("deterministic routing dependency failure")

    def verifier(work: dict[str, object]) -> dict[str, object]:
        criterion = work["criterion"]
        assert isinstance(criterion, dict)
        return {
            "verification_id": "s07-routing-recovery-fact-1",
            "observed_at": int(work["opened_at"]) + 1,
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
                for condition in work["conditions"]
            ],
        }

    faulty = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
        scenario_id="app_bad_brand.json",
        exception_approver_subject=S05_APPROVER.subject,
        fault_injector=fail,
        recovery_verifier=verifier,
    )
    failed = faulty.determine_business_exception_route(
        principal=S05_ROUTER,
        request_id=str(request["request_id"]),
        expected_context=decision["routing_context"],
        idempotency_key="s07-routing-operation-1",
        now=104,
    )

    assert "exception_route.operation" in fault_points
    assert failed["status"] == "blocked"
    assert failed["application_id"] == application_id
    assert failed["recovery_work_id"]
    blocked_route = faulty.current_route_view(
        principal=S05_REVIEWER,
        application_id=application_id,
    )
    assert blocked_route["phase"] == "Unprocessable"
    work = faulty.recovery_work_view(
        principal=OPERATOR,
        recovery_work_id=failed["recovery_work_id"],
    )
    assert work["recovery_target"] == "Routing Determination"
    assert work["application_id"] == application_id

    verified = faulty.verify_recovery(
        principal=OPERATOR,
        recovery_work_id=failed["recovery_work_id"],
        expected_lifecycle_revision=work["lifecycle_revision"],
        expected_criterion_digest=work["criterion"]["digest"],
        idempotency_key="s07-routing-recovery-verify",
    )
    assert verified["status"] == "accepted"
    assert verified["phase"] == "Routing Determination"
    assert faulty.current_route_view(
        principal=S05_REVIEWER,
        application_id=application_id,
    )["phase"] == "Routing Determination"

    rerouted = faulty.determine_business_exception_route(
        principal=S05_ROUTER,
        request_id=str(request["request_id"]),
        expected_context=verified["routing_context"],
        idempotency_key="s07-routing-operation-2",
        now=106,
    )
    assert rerouted["status"] == "accepted"
    assert rerouted["phase"] == "Verification Completed"


def test_release_drill_resumes_only_after_probe_and_fences_old_execution(
    tmp_path: Path,
) -> None:
    service, application_id, _, request, source = _ready_supplement_request(
        tmp_path
    )
    registered_operator = S01CommandPrincipal(
        subject="s07-registered-operator",
        role="operator",
        scope=SUPPLEMENT_INTEGRATOR.scope,
        source_id="s07-release-console",
    )
    unrelated = service.submit_registered(
        submission=_generic_observation_submission(request, source),
        idempotency_key="s07-release-existing-unknown",
        principal=SUPPLEMENT_INTEGRATOR,
    )
    assert unrelated.disposition is AdmissionDisposition.ACCEPTED
    unknown = ControlledScenarioTestDriver(service).process_next_job(
        worker_id="s07-unknown-worker",
        now=10,
        operation_fault="checker_timeout",
    )
    assert unknown.status == "blocked"
    assert unknown.recovery_work_id is not None
    unknown_before = service.recovery_work_view(
        principal=registered_operator,
        recovery_work_id=unknown.recovery_work_id,
    )
    assert unknown_before["primary_reason_code"] == "check.outcome_unknown"
    assert unknown_before["job_status"] == "outcome_unknown"
    assert unknown_before["recovery_fact_count"] == 0
    assert unknown_before["resolution_count"] == 0

    service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="s07-release-supplement-progress",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    fulfilled = service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=True),
        idempotency_key="s07-release-supplement-fulfilled",
        principal=SUPPLEMENT_INTEGRATOR,
        now=201,
    )
    assert fulfilled.disposition is AdmissionDisposition.ACCEPTED
    assert fulfilled.job_id is not None
    assert fulfilled.adapter_id == "s06-detection-adapter"
    history_before = service.application_history_view(
        principal=S06_REVIEWER,
        application_id=application_id,
    )

    checker_entered = threading.Event()
    release_checker = threading.Event()
    delegate = RuleEngine(load_rules(ROOT / "configs" / "rules_auto_lease.yaml"))

    def blocking_checker(application: object) -> object:
        checker_entered.set()
        if not release_checker.wait(timeout=5):
            raise TimeoutError("S07 release-drill worker was not released")
        return delegate.run(application)  # type: ignore[arg-type]

    old_worker = _supplement_service(
        tmp_path,
        source,
        checker_runner=blocking_checker,
        worker_identity="s07-release-old-worker",
        clock=lambda: 0,
    )

    def broken_probe(_: object) -> object:
        raise RuntimeError("release dependency is still unavailable")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(old_worker.process_next_job)
        assert checker_entered.wait(timeout=5)
        try:
            operator_stop = service.stop_new_cohort(
                reason_code="S07_RELEASE_ROLLBACK",
                principal=OPERATOR,
            )
            blocked_admission = service.submit_demo(
                scenario_id="app_missing_vin_docs.json",
                idempotency_key="s07-release-stopped-admission",
                principal=INTAKE,
            )
            draining = service.drain_supplement_operations(
                principal=OPERATOR,
                idempotency_key="s07-release-stop-requests-and-drain",
                now=202,
            )
            stopped_intake = service.stop_supplement_intake(
                principal=OPERATOR,
                idempotency_key="s07-release-stop-intake",
                now=203,
            )
            paused_routing = service.close_business_exception_operations(
                principal=S05_ROUTER,
                idempotency_key="s07-release-pause-routing",
                now=204,
            )
            fenced = service.fence_supplement_workers(
                principal=OPERATOR,
                idempotency_key="s07-release-fence-supplement-adapter",
                now=205,
            )
            runtime_stop = service.stop_new_cohort(
                reason_code="S01_RUNTIME_UNHEALTHY",
                failure_reason_code="S01_BACKGROUND_RUNTIME_EXCEPTION",
                principal=OPERATOR,
            )
            stopped_attachment = service.submit_attachment_version(
                submission=_attachment_submission(request, source, closed=False),
                idempotency_key="s07-release-stopped-attachment",
                principal=SUPPLEMENT_INTEGRATOR,
                now=206,
            )

            assert operator_stop["reason_code"] == "S07_RELEASE_ROLLBACK"
            assert blocked_admission.disposition is AdmissionDisposition.REJECTED
            assert blocked_admission.reason_code == "S07_RELEASE_ROLLBACK"
            assert draining["drain"] == "draining"
            assert draining["requests"] == "closed"
            assert stopped_intake["intake"] == "closed"
            assert paused_routing["operations"] == "closed"
            assert fenced["workers"] == "fenced"
            assert fenced["fenced_job_ids"] == [fulfilled.job_id]
            assert runtime_stop["reason_code"] == "S01_RUNTIME_UNHEALTHY"
            assert stopped_attachment.disposition is AdmissionDisposition.REJECTED
            assert stopped_attachment.reason_code == "supplement.intake_stopped"

            failed_probe = _supplement_service(
                tmp_path,
                source,
                checker_runner=broken_probe,
                clock=lambda: 31,
            )
            assert failed_probe.supplement_operations_status(
                principal=OPERATOR, now=207
            )["workers"] == "fenced"
            assert failed_probe.business_exception_operations_status(
                principal=S05_ROUTER, now=207
            )["operations"] == "closed"
            rejected_recovery = failed_probe.recover_runtime(
                principal=OPERATOR,
                expected_failure_reason_code="S01_BACKGROUND_RUNTIME_EXCEPTION",
            )
            assert rejected_recovery["reason_code"] == "S01_RUNTIME_REPAIR_NOT_VERIFIED"
            failed_supplement_resume = failed_probe.resume_supplement_operations(
                principal=OPERATOR,
                idempotency_key="s07-release-resume-before-probe",
                now=208,
            )
            failed_routing_resume = failed_probe.resume_business_exception_operations(
                principal=S05_ROUTER,
                idempotency_key="s07-release-routing-resume-before-probe",
                now=208,
            )
            assert failed_supplement_resume["status"] == "stopped"
            assert failed_supplement_resume["reason_code"] == (
                "S01_RUNTIME_REPAIR_NOT_VERIFIED"
            )
            assert failed_routing_resume["status"] == "stopped"
            assert failed_routing_resume["reason_code"] == (
                "S01_RUNTIME_REPAIR_NOT_VERIFIED"
            )
        finally:
            release_checker.set()
        late = future.result(timeout=5)

    assert late.status == "stale"
    assert late.reason_code == "STALE_COMPARE_AND_SET"
    unknown_after_stop = service.recovery_work_view(
        principal=registered_operator,
        recovery_work_id=unknown.recovery_work_id,
    )
    assert unknown_after_stop["status"] == "open"
    assert unknown_after_stop["job_status"] == "outcome_unknown"
    assert unknown_after_stop["recovery_fact_count"] == 0
    assert unknown_after_stop["resolution_count"] == 0

    restarted = _supplement_service(tmp_path, source, clock=lambda: 31)
    assert restarted.supplement_operations_status(
        principal=OPERATOR, now=209
    )["workers"] == "fenced"
    assert restarted.business_exception_operations_status(
        principal=S05_ROUTER, now=209
    )["operations"] == "closed"
    recovered = restarted.recover_runtime(
        principal=OPERATOR,
        expected_failure_reason_code="S01_BACKGROUND_RUNTIME_EXCEPTION",
    )
    assert recovered["recovery"] == "scheduled"
    assert restarted.cohort_status() == operator_stop
    routing_resume = restarted.resume_business_exception_operations(
        principal=S05_ROUTER,
        idempotency_key="s07-release-routing-resume-after-probe",
        now=210,
    )
    supplement_resume = restarted.resume_supplement_operations(
        principal=OPERATOR,
        idempotency_key="s07-release-resume-after-probe",
        now=210,
    )
    completed = restarted.process_next_job()

    assert routing_resume["status"] == "accepted"
    assert supplement_resume["status"] == "accepted"
    assert completed.status == "complete"
    assert completed.job_id == fulfilled.job_id
    assert late.fence is not None and completed.fence is not None
    assert completed.fence > late.fence
    history_after = restarted.application_history_view(
        principal=S06_REVIEWER,
        application_id=application_id,
    )
    assert history_after["attachment_versions"] == history_before[
        "attachment_versions"
    ]
    assert {run["run_id"] for run in history_before["runs"]} <= {
        run["run_id"] for run in history_after["runs"]
    }
    assert sum(run["current"] for run in history_after["runs"]) == 1
    unknown_final = restarted.recovery_work_view(
        principal=registered_operator,
        recovery_work_id=unknown.recovery_work_id,
    )
    assert unknown_final["status"] == "open"
    assert unknown_final["job_status"] == "outcome_unknown"
    assert unknown_final["recovery_fact_count"] == 0
    assert unknown_final["resolution_count"] == 0


@pytest.mark.parametrize(
    "corruption",
    (
        "envelope",
        "lifecycle",
    ),
)
def test_queue_publishes_no_recovery_item_for_corrupt_application_authority(
    tmp_path: Path, corruption: str
) -> None:
    service = _service(tmp_path)
    driver = ControlledScenarioTestDriver(service)

    application_id = _admit(service)
    failure = driver.process_next_job(
        worker_id="s07-queue-authority-worker",
        now=10,
        operation_fault="checker_incompatible",
    )
    assert failure.status == "blocked"
    assert failure.recovery_work_id is not None
    queue_before = service.queue_view(
        role="reviewer",
        scope="C-DEMO",
        subject=REVIEWER.subject,
    )
    assert [
        item["recovery_work_id"] for item in queue_before["recovery_items"]
    ] == [failure.recovery_work_id]

    corrupt_app = service._store.applications[application_id]
    assert corrupt_app["phase"] == "Unprocessable"
    if corruption == "envelope":
        corrupt_app["upstream_application_reference"] = "attacker-controlled-ref"
    else:
        service._store.lifecycle_events.append(
            {
                "event_id": "s07-attacker-injected-lifecycle",
                "application_id": application_id,
                "revision": 999,
                "cycle": 1,
                "phase": "Evidence Ready",
                "reason_code": "ATTACK_INJECTED",
            }
        )
    service._store.persist()

    queue_after = service.queue_view(
        role="reviewer",
        scope="C-DEMO",
        subject=REVIEWER.subject,
    )
    assert queue_after["recovery_items"] == []
    assert queue_after["items"] == []
    assert queue_after["projection_watermark"] == queue_before["projection_watermark"]
    assert application_id not in str(queue_after)
    assert failure.recovery_work_id not in str(queue_after)
