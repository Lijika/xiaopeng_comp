from __future__ import annotations

import copy
import hashlib
import json
import multiprocessing
import os
import signal
import sqlite3
import sys
import tempfile
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
import yaml

from task4_consistency.controlled import s01_checker
from task4_consistency.controlled.s01 import (
    AdmissionDisposition,
    AdmissionResult,
    ControlledScenarioService,
    ControlledScenarioTestDriver,
    QueryNotFound,
    S01CommandPrincipal,
)
from task4_consistency.controlled.s01_store import SQLiteTargetStore
from task4_consistency.controlled.s01_checker import TargetChecker, TargetRelease
from task4_consistency.kb.store import get_kb, reload_kb
from task4_consistency.models import Verdict
from task4_consistency.normalize.base import normalize_model, register_normalizer
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules


ROOT = Path(__file__).resolve().parents[1]
TEST_INTEGRATOR = S01CommandPrincipal(
    subject="registered-test-integrator",
    role="integrator",
    scope="C-DEMO",
    source_id="s01-test-client",
)
TEST_OPERATOR = S01CommandPrincipal(
    subject="registered-test-operator",
    role="operator",
    scope="C-DEMO",
    source_id="s01-test-control-plane",
)


def _eligible_field(
    raw: str,
    *,
    observation_id: str,
    source_region: str,
) -> dict[str, object]:
    return {
        "raw": raw,
        "confidence": 0.99,
        "observation_id": observation_id,
        "source_object_ref": "c-demo-object:sha256:test-source",
        "source_sha256": "test-source",
        "provenance_manifest_digest": "f" * 64,
        "source_page": 1,
        "source_region": source_region,
        "evidence_eligible": True,
        "eligibility_reason": "SYNTHETIC_SOURCE_VERIFIED",
    }


def _complete_run_spec(
    release: TargetRelease,
    evidence: list[dict[str, object]],
    *,
    application_id: str,
) -> dict[str, object]:
    snapshot = {
        "schema_version": "s01-evidence-snapshot/1",
        "evidence": evidence,
    }
    snapshot_bytes = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()
    manifest = release.public_manifest()
    return {
        "run_id": "run_contract_test",
        "application_id": application_id,
        "cycle": 1,
        "lifecycle_revision": 4,
        "evidence_snapshot_id": f"snapshot_sha256_{snapshot_digest}",
        "evidence_snapshot_digest": snapshot_digest,
        "evidence_snapshot": snapshot,
        "evidence_revision": 1,
        "evidence_readiness_policy": "c-demo-readiness/1",
        "baseline_release": copy.deepcopy(manifest),
        "release_id": manifest["release_id"],
        "release_digest": manifest["digest"],
        "checker_build": manifest["checker_build"],
        "fence": 1,
        "limits": copy.deepcopy(manifest["limits"]),
        "applicable_check_ids": manifest["applicable_check_ids"],
        "applicable_check_count": manifest["applicable_check_count"],
    }


def make_service(
    fixture_root: Path | None = None,
    *,
    fault_injector: Callable[[str], None] | None = None,
    checker_runner: Callable[[object], object] | None = None,
    application_id_allocator: Callable[[], str] | None = None,
) -> ControlledScenarioService:
    return ControlledScenarioService(
        fixture_root=fixture_root or ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        fault_injector=fault_injector,
        checker_runner=checker_runner,
        application_id_allocator=application_id_allocator,
        state_path=Path(tempfile.mkdtemp(prefix="xiaopeng-s01-unit-"))
        / "target.sqlite3",
    )


def worker_test_driver(
    service: ControlledScenarioService,
) -> ControlledScenarioTestDriver:
    return ControlledScenarioTestDriver(service)


class FailWriteOnce:
    def __init__(self, failure_point: str) -> None:
        self.failure_point = failure_point
        self.fired = False

    def __call__(self, write_point: str) -> None:
        if write_point == self.failure_point and not self.fired:
            self.fired = True
            raise OSError(f"injected write failure: {write_point}")


class FailOnceChecker:
    def __init__(self) -> None:
        self._delegate = RuleEngine(
            load_rules(ROOT / "configs" / "rules_auto_lease.yaml")
        )
        self.calls = 0

    def __call__(self, application: object) -> object:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("injected checker execution failure")
        return self._delegate.run(application)  # type: ignore[arg-type]


class FailAfterConcurrentCommitOnce:
    def __init__(self, concurrent_owner: ControlledScenarioService) -> None:
        self._concurrent_owner = concurrent_owner
        self._delegate = RuleEngine(
            load_rules(ROOT / "configs" / "rules_auto_lease.yaml")
        )
        self.calls = 0

    def __call__(self, application: object) -> object:
        self.calls += 1
        if self.calls == 1:
            self._concurrent_owner.issue_session(
                now=100,
                ttl_seconds=10,
                subject="concurrent-test-user",
                roles=("integrator", "reviewer"),
            )
            raise RuntimeError("injected checker failure after concurrent commit")
        return self._delegate.run(application)  # type: ignore[arg-type]


class AlwaysFailChecker:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _application: object) -> object:
        self.calls += 1
        raise RuntimeError("injected permanent checker failure")


class ExplodingSnapshots(list[object]):
    def __iter__(self):
        raise RuntimeError("injected result conversion failure")


class InvalidOnceChecker:
    def __init__(self, invalid_case: str) -> None:
        self._delegate = RuleEngine(
            load_rules(ROOT / "configs" / "rules_auto_lease.yaml")
        )
        self.invalid_case = invalid_case
        self.calls = 0

    def __call__(self, application: object) -> object:
        self.calls += 1
        report = self._delegate.run(application)  # type: ignore[arg-type]
        if self.calls != 1:
            return report
        if self.invalid_case == "none":
            return None
        if self.invalid_case == "missing_checks":
            return SimpleNamespace(application_id=report.application_id)
        if self.invalid_case == "empty_checks":
            return replace(report, checks=[])
        if self.invalid_case == "missing_applicable_check":
            return replace(
                report,
                checks=[
                    check for check in report.checks if check.rule_id != "R_ENGINE_CROSS"
                ],
            )
        if self.invalid_case == "duplicate_check_id":
            return replace(report, checks=[*report.checks, report.checks[0]])
        if self.invalid_case == "unknown_check_id":
            unknown = replace(report.checks[0], rule_id="R_UNKNOWN")
            return replace(report, checks=[unknown, *report.checks[1:]])
        if self.invalid_case in {"non_terminal_verdict", "illegal_verdict"}:
            value = "running" if self.invalid_case == "non_terminal_verdict" else "approved"
            invalid = replace(
                report.checks[0], verdict=SimpleNamespace(value=value)
            )
            return replace(report, checks=[invalid, *report.checks[1:]])
        if self.invalid_case == "over_limit":
            return replace(report, checks=[report.checks[0]] * 101)
        if self.invalid_case == "conversion_exception":
            blocker = next(
                check for check in report.checks if check.rule_id == "R_ENGINE_CROSS"
            )
            broken = replace(
                blocker,
                snapshots=ExplodingSnapshots(blocker.snapshots),
            )
            return replace(
                report,
                checks=[
                    broken if check.rule_id == blocker.rule_id else check
                    for check in report.checks
                ],
            )
        raise AssertionError(f"unknown invalid result case: {self.invalid_case}")


@pytest.mark.parametrize(
    "failure_point",
    (
        "admission.application",
        "admission.lifecycle_event",
        "admission.evidence_event",
        "admission.audit_event",
        "admission.job",
        "admission.outbox",
        "admission.idempotency_binding",
        "admission.receipt",
        "admission.publish",
    ),
)
def test_admission_write_faults_publish_no_partial_facts_and_can_retry(
    failure_point: str,
) -> None:
    failure = FailWriteOnce(failure_point)
    service = make_service(fault_injector=failure)

    rejected = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=f"s01-admission-fault-{failure_point}",
    )

    assert failure.fired is True
    assert rejected.disposition is AdmissionDisposition.REJECTED
    assert rejected.reason_code == "STORAGE_UNAVAILABLE"
    assert service.fact_counts() == {
        "applications": 0,
        "receipts": 0,
        "lifecycle_events": 0,
        "evidence_events": 0,
        "audit_events": 0,
        "jobs": 0,
        "attempts": 0,
        "runs": 0,
        "findings": 0,
        "outbox": 0,
    }

    assert service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject) == {
        "items": [],
        "projection_watermark": 0,
    }

    recovered = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=f"s01-admission-fault-{failure_point}",
    )

    assert recovered.disposition is AdmissionDisposition.ACCEPTED
    assert recovered.lifecycle_revision == 1
    assert recovered.evidence_revision == 1


def test_external_audit_replica_runs_after_commit_and_cannot_veto_admission(
    tmp_path: Path,
) -> None:
    observed_counts: list[dict[str, int]] = []
    service: ControlledScenarioService

    def unavailable_replica(_: dict[str, object]) -> bool:
        observed_counts.append(service.fact_counts())
        return False

    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "audit-authority.sqlite3",
        audit_writer=unavailable_replica,
    )

    admitted = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-post-commit-audit-replica",
    )

    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    assert admitted.audit_recorded is True
    assert observed_counts == [
        {
            "applications": 1,
            "receipts": 1,
            "lifecycle_events": 1,
            "evidence_events": 1,
            "audit_events": 1,
            "jobs": 1,
            "attempts": 0,
            "runs": 0,
            "findings": 0,
            "outbox": 1,
        }
    ]


def test_fresh_owner_rejects_tampered_immutable_audit_history(tmp_path: Path) -> None:
    state_path = tmp_path / "tampered-audit.sqlite3"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    admitted = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-immutable-audit-history",
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED

    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "UPDATE audit_events SET payload = ?",
            ('{"action":"tampered"}',),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="immutable S01 integrity"):
        ControlledScenarioService(
            fixture_root=ROOT / "fixtures" / "applications",
            rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
            state_path=state_path,
        )


def test_fresh_owner_rejects_deleted_immutable_audit_history(tmp_path: Path) -> None:
    state_path = tmp_path / "deleted-audit.sqlite3"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    admitted = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-deleted-audit-history",
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED

    with sqlite3.connect(state_path) as connection:
        connection.execute("DELETE FROM audit_events")
        connection.commit()

    with pytest.raises(RuntimeError, match="immutable S01 integrity"):
        ControlledScenarioService(
            fixture_root=ROOT / "fixtures" / "applications",
            rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
            state_path=state_path,
        )


def test_fresh_owner_rejects_tampered_outbox_event_body(tmp_path: Path) -> None:
    state_path = tmp_path / "tampered-outbox.sqlite3"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-immutable-outbox-body",
    )
    completed = service.process_next_job()
    assert completed.status == "complete"
    assert service.refresh_projection()["updated"] == 1

    with sqlite3.connect(state_path) as connection:
        rows = connection.execute("SELECT item_id, payload FROM outbox").fetchall()
        event_id, payload = next(
            (item_id, json.loads(encoded))
            for item_id, encoded in rows
            if json.loads(encoded)["kind"] == "review_projection_requested"
        )
        payload["kind"] = "tampered"
        connection.execute(
            "UPDATE outbox SET payload = ? WHERE item_id = ?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), event_id),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="immutable S01 integrity"):
        ControlledScenarioService(
            fixture_root=ROOT / "fixtures" / "applications",
            rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
            state_path=state_path,
        )


def test_fresh_owner_rejects_regressed_outbox_delivery(tmp_path: Path) -> None:
    state_path = tmp_path / "regressed-outbox-delivery.sqlite3"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-monotonic-outbox-delivery",
    )
    assert service.process_next_job().status == "complete"
    assert service.refresh_projection()["updated"] == 1

    with sqlite3.connect(state_path) as connection:
        published_event = connection.execute(
            "SELECT item_id FROM outbox WHERE delivery_status = 'published'"
        ).fetchone()
        assert published_event is not None
        connection.execute(
            "UPDATE outbox SET delivery_status = 'pending' WHERE item_id = ?",
            (published_event[0],),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="immutable S01 integrity"):
        ControlledScenarioService(
            fixture_root=ROOT / "fixtures" / "applications",
            rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
            state_path=state_path,
        )


def test_store_rejects_stale_snapshot_without_overwriting_current_state(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "stale-store.sqlite3"
    first = SQLiteTargetStore(state_path)
    stale = SQLiteTargetStore(state_path)

    first.cohort_stop = {
        "track": "C-DEMO",
        "admission": "stopped",
        "reason_code": "S01_NEW_COHORT_STOPPED",
    }
    first.persist()

    stale.projection_watermark = 1
    with pytest.raises(RuntimeError, match="stale S01 store revision"):
        stale.persist()

    fresh = SQLiteTargetStore(state_path)
    assert fresh.cohort_stop == first.cohort_stop
    assert fresh.projection_watermark == 0


def test_store_reload_reads_one_cross_table_sqlite_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "reload-snapshot.sqlite3"
    owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    admission = owner.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-reload-snapshot",
    )
    assert owner.process_next_job().status == "complete"
    reader = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    writer = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    meta_read = threading.Event()
    continue_read = threading.Event()
    writer_finished = threading.Event()
    original_connect = reader._store._connect

    class PausingCursor:
        def __init__(self, cursor: Any) -> None:
            self._cursor = cursor

        def fetchone(self) -> Any:
            value = self._cursor.fetchone()
            meta_read.set()
            assert continue_read.wait(timeout=5)
            return value

        def __getattr__(self, name: str) -> Any:
            return getattr(self._cursor, name)

    class PausingConnection:
        def __init__(self, connection: Any) -> None:
            self._connection = connection

        def __enter__(self) -> "PausingConnection":
            self._connection.__enter__()
            return self

        def __exit__(self, *args: object) -> Any:
            return self._connection.__exit__(*args)

        def execute(self, sql: str, parameters: object = ()) -> Any:
            cursor = self._connection.execute(sql, parameters)
            if sql.startswith("SELECT projection_watermark, cohort_stop"):
                return PausingCursor(cursor)
            return cursor

        def __getattr__(self, name: str) -> Any:
            return getattr(self._connection, name)

    monkeypatch.setattr(
        reader._store,
        "_connect",
        lambda: PausingConnection(original_connect()),
    )
    reader_result: dict[str, Any] = {}
    writer_result: dict[str, Any] = {}

    def read_queue() -> None:
        try:
            reader_result["value"] = reader.queue_view(
                role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
            )
        except Exception as error:
            reader_result["error"] = error

    def publish_projection() -> None:
        try:
            writer_result["value"] = writer.refresh_projection()
        except Exception as error:
            writer_result["error"] = error
        finally:
            writer_finished.set()

    reader_thread = threading.Thread(target=read_queue)
    reader_thread.start()
    assert meta_read.wait(timeout=2)
    writer_thread = threading.Thread(target=publish_projection)
    writer_thread.start()
    writer_finished.wait(timeout=0.2)
    continue_read.set()
    reader_thread.join(timeout=5)
    writer_thread.join(timeout=5)

    assert "error" not in reader_result
    assert "error" not in writer_result
    queue = reader_result["value"]
    observed = (
        queue["projection_watermark"],
        len(queue["items"]),
        queue["items"][0]["projection_watermark"] if queue["items"] else None,
    )
    assert observed in {(0, 0, None), (1, 1, 1)}
    assert writer_result["value"] == {
        "updated": 1,
        "projection_watermark": 1,
    }
    assert admission.application_id is not None


def test_concurrent_projection_consumers_resolve_stale_publish_as_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "projection-contention.sqlite3"
    owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    admission = owner.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-projection-contention",
    )
    assert owner.process_next_job().status == "complete"
    consumers = [
        ControlledScenarioService(
            fixture_root=ROOT / "fixtures" / "applications",
            rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
            state_path=state_path,
        )
        for _ in range(2)
    ]
    ready_to_persist = threading.Barrier(2)

    def synchronize_first_persist(
        original_persist: Callable[[], None],
    ) -> Callable[[], None]:
        first_persist = True

        def synchronized_persist() -> None:
            nonlocal first_persist
            if first_persist:
                first_persist = False
                ready_to_persist.wait(timeout=5)
            original_persist()

        return synchronized_persist

    for consumer in consumers:
        monkeypatch.setattr(
            consumer._store,
            "persist",
            synchronize_first_persist(consumer._store.persist),
        )

    results: list[dict[str, int]] = []
    errors: list[BaseException] = []

    def consume(consumer: ControlledScenarioService) -> None:
        try:
            results.append(consumer.refresh_projection())
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    threads = [threading.Thread(target=consume, args=(consumer,)) for consumer in consumers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert sorted(result["updated"] for result in results) == [0, 1]
    assert {result["projection_watermark"] for result in results} == {1}
    final = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    queue = final.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject)
    assert [item["application_id"] for item in queue["items"]] == [
        admission.application_id
    ]
    assert final.cohort_status() == {"track": "C-DEMO", "admission": "open"}


def test_fresh_worker_cannot_claim_job_while_durable_lease_is_active(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "durable-claim.sqlite3"
    checker_entered = threading.Event()
    release_checker = threading.Event()
    delegate = RuleEngine(load_rules(ROOT / "configs" / "rules_auto_lease.yaml"))

    def blocking_checker(application: object) -> object:
        checker_entered.set()
        if not release_checker.wait(timeout=5):
            raise TimeoutError("test checker was not released")
        return delegate.run(application)  # type: ignore[arg-type]

    first = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
        checker_runner=blocking_checker,
        worker_identity="worker-a",
        clock=lambda: 0,
    )
    first.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-durable-claim",
    )
    results: list[object] = []
    errors: list[BaseException] = []

    def run_first_worker() -> None:
        try:
            results.append(first.process_next_job())
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    worker = threading.Thread(target=run_first_worker)
    worker.start()
    assert checker_entered.wait(timeout=5)

    contender = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
        worker_identity="worker-b",
        clock=lambda: 10,
    ).process_next_job()

    release_checker.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []
    assert contender.status == "idle"
    assert contender.reason_code == "NO_READY_JOB"
    assert len(results) == 1
    assert results[0].status == "complete"  # type: ignore[union-attr]


def test_expired_takeover_fences_late_worker_result_as_diagnostic(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "durable-fence.sqlite3"
    checker_entered = threading.Event()
    release_checker = threading.Event()
    delegate = RuleEngine(load_rules(ROOT / "configs" / "rules_auto_lease.yaml"))

    def blocking_checker(application: object) -> object:
        checker_entered.set()
        if not release_checker.wait(timeout=5):
            raise TimeoutError("test checker was not released")
        return delegate.run(application)  # type: ignore[arg-type]

    first = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
        checker_runner=blocking_checker,
        worker_identity="worker-expiring",
        clock=lambda: 0,
    )
    admitted = first.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-durable-fence",
    )
    late_results: list[object] = []
    errors: list[BaseException] = []

    def run_expiring_worker() -> None:
        try:
            late_results.append(first.process_next_job())
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    worker = threading.Thread(target=run_expiring_worker)
    worker.start()
    assert checker_entered.wait(timeout=5)

    takeover = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
        worker_identity="worker-takeover",
        clock=lambda: 31,
    )
    winner = takeover.process_next_job()
    assert winner.status == "complete"

    release_checker.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []
    assert len(late_results) == 1
    assert late_results[0].status == "stale"  # type: ignore[union-attr]
    assert late_results[0].reason_code == "STALE_COMPARE_AND_SET"  # type: ignore[union-attr]

    fresh = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    assert fresh.fact_counts()["attempts"] == 2
    assert fresh.fact_counts()["runs"] == 2
    assert fresh.refresh_projection()["updated"] == 1
    workspace = fresh.workspace_view(
        admitted.application_id or "",
        role="reviewer",
        scope="C-DEMO",
        subject=TEST_INTEGRATOR.subject,
        now=31,
    )
    assert workspace["current_run_id"] == winner.run_id


def test_sigterm_after_claim_preserves_attempt_and_frozen_run_spec(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "sigterm-after-claim.sqlite3"
    checker_entered = multiprocessing.get_context("fork").Event()

    authority = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    authority.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-sigterm-after-claim",
    )

    def run_claimed_worker() -> None:
        def blocking_checker(application: object) -> object:
            del application
            checker_entered.set()
            signal.pause()

        worker = ControlledScenarioService(
            fixture_root=ROOT / "fixtures" / "applications",
            rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
            state_path=state_path,
            checker_runner=blocking_checker,
            worker_identity="worker-sigterm",
            clock=lambda: 0,
        )
        worker.process_next_job()

    process = multiprocessing.get_context("fork").Process(target=run_claimed_worker)
    process.start()
    assert checker_entered.wait(timeout=5)
    assert process.pid is not None
    os.kill(process.pid, signal.SIGTERM)
    process.join(timeout=5)
    assert not process.is_alive()
    assert process.exitcode == -signal.SIGTERM

    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
        worker_identity="worker-takeover",
        clock=lambda: 31,
    )
    attempts = restarted._store.attempts
    assert len(attempts) == 1
    assert attempts[0]["worker_id"] == "worker-sigterm"
    assert attempts[0]["fence"] == 1
    assert attempts[0]["run_spec"]["fence"] == 1

    winner = restarted.process_next_job()

    assert winner.status == "complete"
    assert winner.fence == 2
    assert winner.run_id == attempts[0]["run_spec"]["run_id"]
    assert restarted.fact_counts()["attempts"] == 2


@pytest.mark.parametrize(
    "failure_point",
    (
        "result.findings",
        "result.run",
        "result.attempt",
        "result.job",
        "result.current_run",
        "result.lifecycle.routing",
        "result.route",
        "result.lifecycle.final",
        "result.lifecycle.run_link",
        "result.review_work",
        "result.audit_event",
        "result.projection",
        "result.projection_outbox",
        "result.publish",
    ),
)
def test_complete_result_write_faults_keep_diagnostic_non_current_and_recover(
    failure_point: str,
) -> None:
    failure = FailWriteOnce(failure_point)
    service = make_service(fault_injector=failure)
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=f"s01-result-fault-{failure_point}",
    )
    driver = worker_test_driver(service)

    failed = driver.process_next_job(now=0)

    assert failure.fired is True
    assert failed.status == "failed"
    assert failed.reason_code == "RESULT_PUBLICATION_FAILED"
    assert failed.retry_after_seconds == 1
    assert failed.run_id is not None
    assert failed.lifecycle_revision == 4
    assert failed.lifecycle_phases == (
        "Intake",
        "Assembly",
        "Evidence Ready",
        "Checking",
    )
    assert service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject) == {
        "items": [],
        "projection_watermark": 0,
    }
    assert service.refresh_projection() == {
        "updated": 0,
        "projection_watermark": 0,
    }
    assert service.fact_counts() == {
        "applications": 1,
        "receipts": 1,
        "lifecycle_events": 4,
        "evidence_events": 2,
        "audit_events": 1,
        "jobs": 1,
        "attempts": 1,
        "runs": 1,
        "findings": 0,
        "outbox": 1,
    }

    assert driver.process_next_job(now=0).status == "idle"
    recovered = driver.process_next_job(now=1)

    assert recovered.status == "complete"
    assert recovered.application_id == admission.application_id
    assert recovered.run_id == failed.run_id
    assert recovered.fence == 2
    assert recovered.lifecycle_phases == (
        "Intake",
        "Assembly",
        "Evidence Ready",
        "Checking",
        "Assembly",
        "Evidence Ready",
        "Checking",
        "Routing Determination",
        "Manual Review",
    )
    assert service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject) == {
        "items": [],
        "projection_watermark": 0,
    }
    with pytest.raises(QueryNotFound):
        service.workspace_view(
            admission.application_id or "", role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
        )
    assert service.fact_counts()["outbox"] == 2

    assert service.refresh_projection() == {
        "updated": 1,
        "projection_watermark": 1,
    }
    assert service.refresh_projection() == {
        "updated": 0,
        "projection_watermark": 1,
    }
    workspace = service.workspace_view(
        admission.application_id or "", role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
    )
    assert workspace["current_run_id"] == recovered.run_id
    assert workspace["current_run_id"] == failed.run_id
    assert service.fact_counts()["runs"] == 2
    assert service.fact_counts()["findings"] == 13


def test_complete_result_fails_closed_when_required_audit_is_unavailable(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "result-audit-unavailable.sqlite3"
    admission_owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    admission = admission_owner.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-result-audit-unavailable",
    )
    worker = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        audit_available=False,
        state_path=state_path,
    )

    failed = worker_test_driver(worker).process_next_job(now=0)

    assert admission.disposition is AdmissionDisposition.ACCEPTED
    assert failed.status == "failed"
    assert failed.reason_code == "RESULT_PUBLICATION_FAILED"
    assert failed.lifecycle_revision == 4
    assert failed.lifecycle_phases == (
        "Intake",
        "Assembly",
        "Evidence Ready",
        "Checking",
    )
    assert worker.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject) == {
        "items": [],
        "projection_watermark": 0,
    }
    assert worker.refresh_projection() == {
        "updated": 0,
        "projection_watermark": 0,
    }
    assert worker.fact_counts() == {
        "applications": 1,
        "receipts": 1,
        "lifecycle_events": 4,
        "evidence_events": 2,
        "audit_events": 1,
        "jobs": 1,
        "attempts": 1,
        "runs": 1,
        "findings": 0,
        "outbox": 1,
    }


def test_checker_exception_is_non_current_and_higher_fence_recovers() -> None:
    checker = FailOnceChecker()
    service = make_service(checker_runner=checker)
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-checker-exception",
    )
    driver = worker_test_driver(service)

    failed = driver.process_next_job(now=0)

    assert checker.calls == 1
    assert failed.status == "failed"
    assert failed.reason_code == "CHECKER_EXCEPTION"
    assert failed.retry_after_seconds == 1
    assert failed.run_id is not None
    assert failed.fence == 1
    assert failed.lifecycle_revision == 4
    assert failed.lifecycle_phases == (
        "Intake",
        "Assembly",
        "Evidence Ready",
        "Checking",
    )
    assert service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject) == {
        "items": [],
        "projection_watermark": 0,
    }
    assert service.refresh_projection() == {
        "updated": 0,
        "projection_watermark": 0,
    }
    assert service.fact_counts()["attempts"] == 1
    assert service.fact_counts()["runs"] == 1
    assert service.fact_counts()["findings"] == 0

    assert driver.process_next_job(now=0).status == "idle"
    recovered = driver.process_next_job(now=1)

    assert checker.calls == 2
    assert recovered.status == "complete"
    assert recovered.application_id == admission.application_id
    assert recovered.run_id == failed.run_id
    assert recovered.fence == 2
    assert recovered.lifecycle_phases == (
        "Intake",
        "Assembly",
        "Evidence Ready",
        "Checking",
        "Assembly",
        "Evidence Ready",
        "Checking",
        "Routing Determination",
        "Manual Review",
    )
    assert service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject) == {
        "items": [],
        "projection_watermark": 0,
    }
    assert service.fact_counts()["outbox"] == 2
    assert service.refresh_projection() == {
        "updated": 1,
        "projection_watermark": 1,
    }
    assert service.refresh_projection() == {
        "updated": 0,
        "projection_watermark": 1,
    }
    workspace = service.workspace_view(
        admission.application_id or "", role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
    )
    assert workspace["current_run_id"] == recovered.run_id
    assert workspace["current_run_id"] == failed.run_id


@pytest.mark.parametrize("diagnostic", ("publication_failed", "stale"))
def test_result_diagnostics_rebase_over_an_unrelated_concurrent_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    diagnostic: str,
) -> None:
    state_path = tmp_path / f"{diagnostic}-contention.sqlite3"
    failure = FailWriteOnce("result.publish") if diagnostic == "publication_failed" else None
    worker = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
        fault_injector=failure,
    )
    worker.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=f"s01-{diagnostic}-contention",
    )
    concurrent_owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    store_type = type(worker._store)
    original_persist = store_type.persist
    contended = False

    def persist_after_concurrent_commit(store: object) -> None:
        nonlocal contended
        if not contended and any(
            run.get("status") == diagnostic for run in store.runs  # type: ignore[attr-defined]
        ):
            contended = True
            concurrent_owner.issue_session(
                now=100,
                ttl_seconds=10,
                subject="concurrent-test-user",
                roles=("integrator", "reviewer"),
            )
        original_persist(store)

    monkeypatch.setattr(store_type, "persist", persist_after_concurrent_commit)
    driver = worker_test_driver(worker)

    if diagnostic == "publication_failed":
        first = driver.process_next_job(now=0)
        expected = ("failed", "RESULT_PUBLICATION_FAILED")
        recovery_now = 1
    else:
        first = driver.process_next_job(now=0, cas_fault="cycle")
        expected = ("stale", "STALE_COMPARE_AND_SET")
        recovery_now = 31

    assert contended is True
    assert (first.status, first.reason_code) == expected
    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    assert restarted.fact_counts()["attempts"] == 1
    assert restarted.fact_counts()["runs"] == 1

    recovered = driver.process_next_job(now=recovery_now)

    assert recovered.status == "complete"
    assert recovered.run_id == first.run_id


def test_checker_failure_rebases_over_an_unrelated_concurrent_commit(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "checker-failure-contention.sqlite3"
    authority = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    authority.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-checker-failure-contention",
    )
    concurrent_owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    checker = FailAfterConcurrentCommitOnce(concurrent_owner)
    worker = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        checker_runner=checker,
        state_path=state_path,
    )
    driver = worker_test_driver(worker)

    failed = driver.process_next_job(now=0)

    assert failed.status == "failed"
    assert failed.reason_code == "CHECKER_EXCEPTION"
    assert failed.retry_after_seconds == 1
    assert failed.fence == 1
    assert worker.fact_counts() == {
        "applications": 1,
        "receipts": 1,
        "lifecycle_events": 4,
        "evidence_events": 2,
        "audit_events": 1,
        "jobs": 1,
        "attempts": 1,
        "runs": 1,
        "findings": 0,
        "outbox": 1,
    }

    recovered = driver.process_next_job(now=1)

    assert checker.calls == 2
    assert recovered.status == "complete"
    assert recovered.fence == 2
    assert recovered.run_id == failed.run_id


def test_checker_failure_publication_rebases_after_stage_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "checker-failure-publication-contention.sqlite3"
    authority = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    authority.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-checker-failure-publication-contention",
    )
    concurrent_owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    worker = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        checker_runner=FailOnceChecker(),
        state_path=state_path,
    )
    store_type = type(worker._store)
    original_persist = store_type.persist
    contended = False

    def persist_after_checker_failure(store: object) -> None:
        nonlocal contended
        if not contended and any(
            run.get("status") == "checker_failed"  # type: ignore[attr-defined]
            for run in store.runs  # type: ignore[attr-defined]
        ):
            contended = True
            concurrent_owner.issue_session(
                now=100,
                ttl_seconds=10,
                subject="concurrent-test-user",
                roles=("integrator", "reviewer"),
            )
        original_persist(store)

    monkeypatch.setattr(store_type, "persist", persist_after_checker_failure)
    driver = worker_test_driver(worker)

    failed = driver.process_next_job(now=0)

    assert contended is True
    assert failed.status == "failed"
    assert failed.reason_code == "CHECKER_EXCEPTION"
    assert failed.retry_after_seconds == 1
    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    assert restarted.fact_counts()["attempts"] == 1
    assert restarted.fact_counts()["runs"] == 1
    recovered = worker_test_driver(restarted).process_next_job(now=1)
    assert recovered.status == "complete"


def test_checker_failure_publication_contention_exhaustion_stops_with_worker_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "checker-failure-contention-exhaustion.sqlite3"
    authority = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    admission = authority.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-checker-failure-contention-exhaustion",
    )
    concurrent_owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    worker = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        checker_runner=FailOnceChecker(),
        state_path=state_path,
    )
    store_type = type(worker._store)
    original_persist = store_type.persist
    contentions = 0

    def persist_after_checker_failure(store: object) -> None:
        nonlocal contentions
        if contentions < 3 and any(
            run.get("status") == "checker_failed"  # type: ignore[attr-defined]
            for run in store.runs  # type: ignore[attr-defined]
        ):
            contentions += 1
            concurrent_owner.issue_session(
                now=100 + contentions,
                ttl_seconds=10,
                subject=f"concurrent-test-user-{contentions}",
                roles=("integrator", "reviewer"),
            )
        original_persist(store)

    monkeypatch.setattr(store_type, "persist", persist_after_checker_failure)
    worker_id = "s01-contention-worker"

    stopped = worker_test_driver(worker).process_next_job(
        worker_id=worker_id,
        now=0,
    )

    assert contentions == 3
    assert stopped.status == "stopped"
    assert stopped.reason_code == "S01_BACKGROUND_RUNTIME_EXCEPTION"
    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    assert restarted.cohort_status() == {
        "track": "C-DEMO",
        "admission": "stopped",
        "reason_code": "S01_RUNTIME_UNHEALTHY",
        "failure_reason_code": "S01_BACKGROUND_RUNTIME_EXCEPTION",
    }
    timeline = restarted.audit_timeline(
        principal=S01CommandPrincipal(
            subject="registered-test-auditor",
            role="auditor",
            scope="C-DEMO",
            source_id="s01-test-audit-console",
        ),
        application_id=admission.application_id or "",
    )
    stop_actor = next(
        event["actor"]
        for event in timeline["events"]
        if event["action"] == "controlled_cohort_stop"
    )
    assert stop_actor == {
        "subject": worker_id,
        "role": "operator",
        "scope": "C-DEMO",
        "source_id": "s01-target-worker",
    }


def test_complete_result_contention_exhaustion_stops_without_partial_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "complete-result-contention-exhaustion.sqlite3"
    worker_id = "s01-complete-contention-worker"
    authority = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    admission = authority.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-complete-result-contention-exhaustion",
    )
    concurrent_owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    worker = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
        worker_identity=worker_id,
        clock=lambda: 0,
    )
    store_type = type(worker._store)
    original_persist = store_type.persist
    contentions = 0
    contend = True

    def persist_after_complete_stage(store: object) -> None:
        nonlocal contentions
        if contend and any(
            run.get("status") == "complete"  # type: ignore[attr-defined]
            for run in store.runs  # type: ignore[attr-defined]
        ):
            contentions += 1
            concurrent_owner.issue_session(
                now=100 + contentions,
                ttl_seconds=10_000,
                subject=f"complete-contention-{contentions}",
                roles=("integrator", "reviewer"),
            )
        original_persist(store)

    monkeypatch.setattr(store_type, "persist", persist_after_complete_stage)
    previous_recursion_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(180)
    try:
        stopped = worker.process_next_job()
    finally:
        sys.setrecursionlimit(previous_recursion_limit)

    assert admission.disposition is AdmissionDisposition.ACCEPTED
    assert contentions == 3
    assert stopped.status == "stopped"
    assert stopped.reason_code == "RESULT_PUBLICATION_FAILED_RETRY_EXHAUSTED"
    assert stopped.application_id == admission.application_id
    assert stopped.lifecycle_phases == (
        "Intake",
        "Assembly",
        "Evidence Ready",
        "Checking",
    )
    assert worker.fact_counts()["runs"] == 1
    assert worker.cohort_status() == {
        "track": "C-DEMO",
        "admission": "stopped",
        "reason_code": "S01_RUNTIME_UNHEALTHY",
        "failure_reason_code": "RESULT_PUBLICATION_FAILED_RETRY_EXHAUSTED",
    }

    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
        worker_identity=worker_id,
        clock=lambda: 0,
    )
    assert restarted.cohort_status() == worker.cohort_status()
    assert restarted.fact_counts() == {
        "applications": 1,
        "receipts": 1,
        "lifecycle_events": 4,
        "evidence_events": 2,
        "audit_events": 2,
        "jobs": 1,
        "attempts": 1,
        "runs": 1,
        "findings": 0,
        "outbox": 1,
    }
    persisted_run = restarted._store.runs[0]
    assert persisted_run["status"] == "publication_failed"
    assert persisted_run["reason_code"] == "RESULT_PUBLICATION_FAILED"
    assert persisted_run["run_id"] == stopped.run_id
    assert persisted_run["finding_ids"] == []
    assert "completion_context" not in persisted_run
    persisted_job = restarted._store.jobs[0]
    assert persisted_job["status"] == "diagnostic"
    assert persisted_job["terminal_reason_code"] == (
        "RESULT_PUBLICATION_FAILED_RETRY_EXHAUSTED"
    )
    persisted_app = restarted._store.applications[admission.application_id or ""]
    assert {
        key: persisted_app[key]
        for key in (
            "phase",
            "lifecycle_revision",
            "phase_history",
            "evidence_ready",
            "route",
            "current_run_id",
            "projection_pending",
            "projection_visible",
        )
    } == {
        "phase": "Checking",
        "lifecycle_revision": 4,
        "phase_history": ["Intake", "Assembly", "Evidence Ready", "Checking"],
        "evidence_ready": True,
        "route": "pending_check",
        "current_run_id": None,
        "projection_pending": False,
        "projection_visible": False,
    }
    assert "current_evidence_snapshot_id" not in persisted_app
    assert "current_evidence_snapshot_digest" not in persisted_app
    persisted_receipt = restarted._store.receipts[admission.receipt_id or ""]
    assert persisted_receipt.disposition is AdmissionDisposition.ACCEPTED
    assert persisted_receipt.application_id == admission.application_id
    assert persisted_receipt.job_id == persisted_job["job_id"]
    assert [
        (event["revision"], event["phase"])
        for event in restarted._store.lifecycle_events
    ] == [
        (1, "Intake"),
        (2, "Assembly"),
        (3, "Evidence Ready"),
        (4, "Checking"),
    ]
    assert restarted.process_next_job().status == "stopped"
    timeline = restarted.audit_timeline(
        principal=S01CommandPrincipal(
            subject="registered-test-auditor",
            role="auditor",
            scope="C-DEMO",
            source_id="s01-test-audit-console",
        ),
        application_id=admission.application_id or "",
    )
    stop_event = next(
        event
        for event in timeline["events"]
        if event["action"] == "controlled_cohort_stop"
    )
    assert all(
        event["action"] != "controlled_run_result" for event in timeline["events"]
    )
    assert stop_event["actor"] == {
        "subject": worker_id,
        "role": "worker",
        "scope": "C-DEMO",
        "source_id": "s01-target-worker",
    }
    assert stop_event["context"]["reason_code"] == "S01_RUNTIME_UNHEALTHY"
    assert stop_event["context"]["failure_reason_code"] == (
        "RESULT_PUBLICATION_FAILED_RETRY_EXHAUSTED"
    )

    contend = False
    recovery = restarted.recover_runtime(
        principal=TEST_OPERATOR,
        expected_failure_reason_code="RESULT_PUBLICATION_FAILED_RETRY_EXHAUSTED",
    )
    assert recovery["recovery"] == "scheduled"
    assert recovery["requeued_jobs"] == 1
    assert restarted.cohort_status() == {"track": "C-DEMO", "admission": "open"}

    recovered = restarted.process_next_job()
    assert recovered.status == "complete"
    assert recovered.run_id == stopped.run_id
    assert recovered.lifecycle_phases == (
        "Intake",
        "Assembly",
        "Evidence Ready",
        "Checking",
        "Assembly",
        "Evidence Ready",
        "Checking",
        "Routing Determination",
        "Manual Review",
    )
    assert restarted.fact_counts()["runs"] == 2


def test_permanent_checker_failure_has_durable_backoff_and_stops_runtime(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "permanent-checker-failure.sqlite3"
    checker = AlwaysFailChecker()
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        checker_runner=checker,
        state_path=state_path,
    )
    service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-permanent-checker-failure",
    )
    driver = worker_test_driver(service)

    first = driver.process_next_job(now=0)
    before_second = driver.process_next_job(now=0)
    second = driver.process_next_job(now=1)
    before_terminal = driver.process_next_job(now=2)
    terminal = driver.process_next_job(now=3)
    after_terminal = driver.process_next_job(now=100)

    assert first.status == "failed"
    assert first.reason_code == "CHECKER_EXCEPTION"
    assert first.retry_after_seconds == 1
    assert before_second.status == "idle"
    assert second.status == "failed"
    assert second.retry_after_seconds == 2
    assert before_terminal.status == "idle"
    assert terminal.status == "stopped"
    assert terminal.reason_code == "CHECKER_EXCEPTION_RETRY_EXHAUSTED"
    assert terminal.retry_after_seconds == 0
    assert after_terminal.status == "stopped"
    assert after_terminal.reason_code == "CHECKER_EXCEPTION_RETRY_EXHAUSTED"
    assert checker.calls == 3
    assert service.fact_counts()["attempts"] == 3
    assert service.fact_counts()["runs"] == 3
    assert service.cohort_status() == {
        "track": "C-DEMO",
        "admission": "stopped",
        "reason_code": "S01_RUNTIME_UNHEALTHY",
        "failure_reason_code": "CHECKER_EXCEPTION_RETRY_EXHAUSTED",
    }
    terminal_stop_event = service._store.audit_events[-1]
    assert terminal_stop_event["action"] == "controlled_cohort_stop"
    assert terminal_stop_event["subject"] == "s01-test-worker"
    assert terminal_stop_event["role"] == "worker"
    assert terminal_stop_event["result"] == "stopped"
    assert terminal_stop_event["failure_reason_code"] == (
        "CHECKER_EXCEPTION_RETRY_EXHAUSTED"
    )
    unverified_recovery = service.recover_runtime(
        principal=TEST_OPERATOR,
        expected_failure_reason_code="CHECKER_EXCEPTION_RETRY_EXHAUSTED"
    )
    assert unverified_recovery == {
        "track": "C-DEMO",
        "recovery": "rejected",
        "reason_code": "S01_RUNTIME_REPAIR_NOT_VERIFIED",
        "failure_reason_code": "CHECKER_EXCEPTION_RETRY_EXHAUSTED",
        "requeued_jobs": 0,
    }
    assert service.process_next_job().status == "stopped"

    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    assert restarted.process_next_job().status == "stopped"
    assert restarted.cohort_status() == service.cohort_status()

    rejected_recovery = restarted.recover_runtime(
        principal=TEST_OPERATOR,
        expected_failure_reason_code="INVALID_RUN_RESULT_RETRY_EXHAUSTED"
    )
    assert rejected_recovery == {
        "track": "C-DEMO",
        "recovery": "rejected",
        "reason_code": "S01_RUNTIME_RECOVERY_PRECONDITION_FAILED",
        "failure_reason_code": "CHECKER_EXCEPTION_RETRY_EXHAUSTED",
        "requeued_jobs": 0,
    }
    assert restarted.process_next_job().status == "stopped"

    recovery = restarted.recover_runtime(
        principal=TEST_OPERATOR,
        expected_failure_reason_code="CHECKER_EXCEPTION_RETRY_EXHAUSTED"
    )
    recovered = worker_test_driver(restarted).process_next_job(now=4)

    assert recovery == {
        "track": "C-DEMO",
        "recovery": "scheduled",
        "reason_code": "S01_RUNTIME_RECOVERY_SCHEDULED",
        "failure_reason_code": "CHECKER_EXCEPTION_RETRY_EXHAUSTED",
        "requeued_jobs": 1,
    }
    assert recovered.status == "complete"
    assert recovered.fence == 4
    assert restarted.cohort_status() == {"track": "C-DEMO", "admission": "open"}
    assert restarted.fact_counts()["attempts"] == 4
    assert restarted.fact_counts()["runs"] == 4


def test_terminal_checker_stop_is_not_persisted_without_required_audit(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "terminal-stop-audit-unavailable.sqlite3"
    owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    owner.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-terminal-stop-audit-unavailable",
    )
    worker = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        checker_runner=AlwaysFailChecker(),
        audit_available=False,
        state_path=state_path,
    )
    driver = worker_test_driver(worker)

    assert driver.process_next_job(now=0).status == "failed"
    assert driver.process_next_job(now=1).status == "failed"
    terminal = driver.process_next_job(now=3)

    assert terminal.status == "stopped"
    assert terminal.reason_code == "CHECKER_EXCEPTION_RETRY_EXHAUSTED"
    assert worker.fact_counts()["audit_events"] == 1
    with sqlite3.connect(state_path) as connection:
        cohort_stop = connection.execute(
            "SELECT cohort_stop FROM s01_meta WHERE id = 1"
        ).fetchone()[0]
    assert cohort_stop is None


def test_verified_runtime_recovery_restores_existing_operator_stop(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "operator-stop-runtime-recovery.sqlite3"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        checker_runner=AlwaysFailChecker(),
        state_path=state_path,
    )
    accepted = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-operator-stop-runtime-failure",
    )
    operator_stop = service.stop_new_cohort(principal=TEST_OPERATOR)
    driver = worker_test_driver(service)

    assert driver.process_next_job(now=0).status == "failed"
    assert driver.process_next_job(now=1).status == "failed"
    terminal = driver.process_next_job(now=3)
    assert terminal.status == "stopped"
    assert service.cohort_status()["reason_code"] == "S01_RUNTIME_UNHEALTHY"

    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    recovery = restarted.recover_runtime(
        principal=TEST_OPERATOR,
        expected_failure_reason_code="CHECKER_EXCEPTION_RETRY_EXHAUSTED"
    )
    recovered = worker_test_driver(restarted).process_next_job(now=4)
    rejected = restarted.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-admission-after-runtime-recovery",
    )

    assert accepted.disposition is AdmissionDisposition.ACCEPTED
    assert recovery["recovery"] == "scheduled"
    assert recovered.status == "complete"
    assert restarted.cohort_status() == operator_stop
    assert rejected.disposition is AdmissionDisposition.REJECTED
    assert rejected.reason_code == "S01_NEW_COHORT_STOPPED"
    assert restarted.fact_counts()["audit_events"] == 6
    assert [event["action"] for event in restarted._store.audit_events] == [
        "controlled_admission",
        "controlled_cohort_stop",
        "controlled_cohort_stop",
        "runtime_recovery",
        "controlled_run_result",
        "controlled_admission",
    ]


def test_runtime_recovery_changes_no_authority_when_required_audit_is_unavailable(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "recovery-audit-unavailable.sqlite3"
    owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    stopped = owner.stop_new_cohort(
        principal=TEST_OPERATOR,
        reason_code="S01_RUNTIME_UNHEALTHY",
        failure_reason_code="S01_BACKGROUND_RUNTIME_EXCEPTION",
    )
    unavailable = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        audit_available=False,
        state_path=state_path,
    )
    before = unavailable.fact_counts()

    rejected = unavailable.recover_runtime(
        principal=TEST_OPERATOR,
        expected_failure_reason_code="S01_BACKGROUND_RUNTIME_EXCEPTION"
    )

    assert rejected == {
        "track": "C-DEMO",
        "recovery": "rejected",
        "reason_code": "AUDIT_UNAVAILABLE",
        "failure_reason_code": "S01_BACKGROUND_RUNTIME_EXCEPTION",
        "requeued_jobs": 0,
    }
    assert unavailable.fact_counts() == before
    assert unavailable.cohort_status() == stopped


@pytest.mark.parametrize("broken_boundary", ("checker", "projection"))
def test_background_runtime_recovery_requires_live_authority_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    broken_boundary: str,
) -> None:
    service = make_service()
    runtime_stop = service.stop_new_cohort(
        principal=TEST_OPERATOR,
        reason_code="S01_RUNTIME_UNHEALTHY",
        failure_reason_code="S01_BACKGROUND_RUNTIME_EXCEPTION",
    )
    original_checker = service._run_checker
    original_projection = service.refresh_projection

    def still_broken(*_args: object) -> object:
        raise RuntimeError(f"{broken_boundary} authority remains unavailable")

    if broken_boundary == "checker":
        monkeypatch.setattr(service, "_run_checker", still_broken)
    else:
        monkeypatch.setattr(service, "refresh_projection", still_broken)

    rejected = service.recover_runtime(
        principal=TEST_OPERATOR,
        expected_failure_reason_code="S01_BACKGROUND_RUNTIME_EXCEPTION"
    )

    assert rejected == {
        "track": "C-DEMO",
        "recovery": "rejected",
        "reason_code": "S01_RUNTIME_REPAIR_NOT_VERIFIED",
        "failure_reason_code": "S01_BACKGROUND_RUNTIME_EXCEPTION",
        "requeued_jobs": 0,
    }
    assert service.cohort_status() == runtime_stop

    monkeypatch.setattr(service, "_run_checker", original_checker)
    monkeypatch.setattr(service, "refresh_projection", original_projection)
    recovery = service.recover_runtime(
        principal=TEST_OPERATOR,
        expected_failure_reason_code="S01_BACKGROUND_RUNTIME_EXCEPTION"
    )

    assert recovery == {
        "track": "C-DEMO",
        "recovery": "scheduled",
        "reason_code": "S01_RUNTIME_RECOVERY_SCHEDULED",
        "failure_reason_code": "S01_BACKGROUND_RUNTIME_EXCEPTION",
        "requeued_jobs": 0,
    }
    assert service.cohort_status() == {"track": "C-DEMO", "admission": "open"}


def test_background_runtime_recovery_probes_queued_checker_without_side_effects() -> None:
    checker = AlwaysFailChecker()
    service = make_service(checker_runner=checker)
    service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-background-recovery-broken-checker",
    )
    runtime_stop = service.stop_new_cohort(
        principal=TEST_OPERATOR,
        reason_code="S01_RUNTIME_UNHEALTHY",
        failure_reason_code="S01_BACKGROUND_RUNTIME_EXCEPTION",
    )
    before = service.fact_counts()

    rejected = service.recover_runtime(
        principal=TEST_OPERATOR,
        expected_failure_reason_code="S01_BACKGROUND_RUNTIME_EXCEPTION"
    )

    assert rejected == {
        "track": "C-DEMO",
        "recovery": "rejected",
        "reason_code": "S01_RUNTIME_REPAIR_NOT_VERIFIED",
        "failure_reason_code": "S01_BACKGROUND_RUNTIME_EXCEPTION",
        "requeued_jobs": 0,
    }
    assert checker.calls == 1
    assert service.cohort_status() == runtime_stop
    assert service.fact_counts() == before


def test_terminal_checker_recovery_requires_a_live_projection_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "checker-recovery-projection.sqlite3"
    failed = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        checker_runner=AlwaysFailChecker(),
        state_path=state_path,
    )
    failed.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-checker-recovery-projection",
    )
    driver = worker_test_driver(failed)
    assert driver.process_next_job(now=0).status == "failed"
    assert driver.process_next_job(now=1).status == "failed"
    assert driver.process_next_job(now=3).status == "stopped"

    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    runtime_stop = restarted.cohort_status()
    before = restarted.fact_counts()

    def projection_still_broken() -> dict[str, int]:
        raise RuntimeError("projection authority remains unavailable")

    monkeypatch.setattr(restarted, "refresh_projection", projection_still_broken)
    rejected = restarted.recover_runtime(
        principal=TEST_OPERATOR,
        expected_failure_reason_code="CHECKER_EXCEPTION_RETRY_EXHAUSTED"
    )

    assert rejected == {
        "track": "C-DEMO",
        "recovery": "rejected",
        "reason_code": "S01_RUNTIME_REPAIR_NOT_VERIFIED",
        "failure_reason_code": "CHECKER_EXCEPTION_RETRY_EXHAUSTED",
        "requeued_jobs": 0,
    }
    assert restarted.cohort_status() == runtime_stop
    assert restarted.fact_counts() == before


def test_terminal_checker_recovery_rejects_a_drifted_admission_release(
    tmp_path: Path,
) -> None:
    rules_source = ROOT / "configs" / "rules_auto_lease.yaml"
    deployed_rules = tmp_path / "rules.yaml"
    original_rules = rules_source.read_bytes()
    deployed_rules.write_bytes(original_rules)
    state_path = tmp_path / "checker-recovery-release.sqlite3"
    failed = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=deployed_rules,
        checker_runner=AlwaysFailChecker(),
        state_path=state_path,
    )
    failed.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-checker-recovery-release",
    )
    driver = worker_test_driver(failed)
    assert driver.process_next_job(now=0).status == "failed"
    assert driver.process_next_job(now=1).status == "failed"
    assert driver.process_next_job(now=3).status == "stopped"

    deployed_rules.write_bytes(
        original_rules.replace(
            b"low_confidence_threshold: 0.6",
            b"low_confidence_threshold: 1.0",
        )
    )
    drifted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=deployed_rules,
        state_path=state_path,
    )
    runtime_stop = drifted.cohort_status()
    before = drifted.fact_counts()

    rejected = drifted.recover_runtime(
        principal=TEST_OPERATOR,
        expected_failure_reason_code="CHECKER_EXCEPTION_RETRY_EXHAUSTED"
    )

    assert rejected == {
        "track": "C-DEMO",
        "recovery": "rejected",
        "reason_code": "S01_RUNTIME_REPAIR_NOT_VERIFIED",
        "failure_reason_code": "CHECKER_EXCEPTION_RETRY_EXHAUSTED",
        "requeued_jobs": 0,
    }
    assert drifted.cohort_status() == runtime_stop
    assert drifted.fact_counts() == before


@pytest.mark.parametrize(
    "invalid_case",
    (
        "none",
        "missing_checks",
        "empty_checks",
        "missing_applicable_check",
        "duplicate_check_id",
        "unknown_check_id",
        "non_terminal_verdict",
        "illegal_verdict",
        "over_limit",
        "conversion_exception",
    ),
)
def test_invalid_checker_result_is_non_current_and_higher_fence_recovers(
    invalid_case: str,
) -> None:
    checker = InvalidOnceChecker(invalid_case)
    service = make_service(checker_runner=checker)
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=f"s01-invalid-result-{invalid_case}",
    )
    queue_before = service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject)
    driver = worker_test_driver(service)

    failed = driver.process_next_job(now=0)

    assert checker.calls == 1
    assert failed.status == "failed"
    assert failed.reason_code == "INVALID_RUN_RESULT"
    assert failed.retry_after_seconds == 1
    assert failed.run_id is not None
    assert failed.fence == 1
    assert failed.semantic_differential is None
    assert failed.lifecycle_revision == 4
    assert failed.lifecycle_phases == (
        "Intake",
        "Assembly",
        "Evidence Ready",
        "Checking",
    )
    assert service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject) == queue_before == {
        "items": [],
        "projection_watermark": 0,
    }
    assert service.refresh_projection() == {
        "updated": 0,
        "projection_watermark": 0,
    }
    assert service.fact_counts() == {
        "applications": 1,
        "receipts": 1,
        "lifecycle_events": 4,
        "evidence_events": 2,
        "audit_events": 1,
        "jobs": 1,
        "attempts": 1,
        "runs": 1,
        "findings": 0,
        "outbox": 1,
    }

    assert driver.process_next_job(now=0).status == "idle"
    recovered = driver.process_next_job(now=1)

    assert checker.calls == 2
    assert recovered.status == "complete"
    assert recovered.application_id == admission.application_id
    assert recovered.run_id == failed.run_id
    assert recovered.fence == 2
    assert recovered.semantic_differential == {
        "oracle": "legacy-rule-engine",
        "scope": "one-c-demo-fixture",
        "checks_compared": 13,
        "mismatches": [],
        "status": "match",
    }
    assert service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject) == {
        "items": [],
        "projection_watermark": 0,
    }
    assert service.fact_counts()["outbox"] == 2
    assert service.refresh_projection() == {
        "updated": 1,
        "projection_watermark": 1,
    }
    assert service.refresh_projection() == {
        "updated": 0,
        "projection_watermark": 1,
    }
    workspace = service.workspace_view(
        admission.application_id or "", role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
    )
    assert workspace["current_run_id"] == recovered.run_id
    assert workspace["current_run_id"] == failed.run_id
    assert service.fact_counts()["runs"] == 2
    assert service.fact_counts()["findings"] == 13


def test_fixed_c_demo_admission_is_atomic_and_idempotent() -> None:
    service = make_service()

    first = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-first-admission",
    )

    assert first.disposition is AdmissionDisposition.ACCEPTED
    assert first.application_id
    assert first.receipt_id
    assert first.lifecycle_revision == 1
    assert first.evidence_revision == 1
    assert first.audit_recorded is True
    assert first.job_id
    assert service.fact_counts() == {
        "applications": 1,
        "receipts": 1,
        "lifecycle_events": 1,
        "evidence_events": 1,
        "audit_events": 1,
        "jobs": 1,
        "attempts": 0,
        "runs": 0,
        "findings": 0,
        "outbox": 1,
    }


    replay = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-first-admission",
    )

    assert replay.disposition is AdmissionDisposition.ACCEPTED
    assert replay.replayed is True
    assert replay.receipt_id == first.receipt_id
    assert replay.application_id == first.application_id
    assert service.fact_counts() == {
        "applications": 1,
        "receipts": 1,
        "lifecycle_events": 1,
        "evidence_events": 1,
        "audit_events": 1,
        "jobs": 1,
        "attempts": 0,
        "runs": 0,
        "findings": 0,
        "outbox": 1,
    }


def test_canonical_envelope_persists_stable_identity_time_scope_and_batch(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "canonical-envelope.sqlite3"
    idempotency_key = "s01-canonical-envelope-secret-key"
    principal = S01CommandPrincipal(
        subject="canonical-integrator",
        role="integrator",
        scope="C-DEMO",
        source_id="registered-c-demo-source",
    )
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )

    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=idempotency_key,
        principal=principal,
    )
    replay = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=idempotency_key,
        principal=principal,
    )
    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    replay_after_restart = restarted.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=idempotency_key,
        principal=principal,
    )

    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    assert admitted.envelope_id
    assert admitted.stream_id
    assert admitted.source_revision_id
    assert admitted.idempotency_identity
    assert admitted.idempotency_key_digest == hashlib.sha256(
        idempotency_key.encode("utf-8")
    ).hexdigest()
    stable_identity = (
        admitted.envelope_id,
        admitted.stream_id,
        admitted.source_revision_id,
        admitted.batch_id,
        admitted.envelope_fingerprint,
    )
    assert (
        replay.envelope_id,
        replay.stream_id,
        replay.source_revision_id,
        replay.batch_id,
        replay.envelope_fingerprint,
    ) == stable_identity
    assert (
        replay_after_restart.envelope_id,
        replay_after_restart.stream_id,
        replay_after_restart.source_revision_id,
        replay_after_restart.batch_id,
        replay_after_restart.envelope_fingerprint,
    ) == stable_identity
    assert replay.replayed is True
    assert replay_after_restart.replayed is True

    with sqlite3.connect(state_path) as connection:
        application_payload = json.loads(
            connection.execute(
                "SELECT payload FROM applications WHERE item_id = ?",
                (admitted.application_id,),
            ).fetchone()[0]
        )
        audit_payload = json.loads(
            connection.execute("SELECT payload FROM audit_events").fetchone()[0]
        )
        receipt_payload = json.loads(
            connection.execute(
                "SELECT payload FROM receipts WHERE item_id = ?",
                (admitted.receipt_id,),
            ).fetchone()[0]
        )

    envelope = application_payload["envelope"]
    assert envelope == audit_payload["envelope"]
    assert envelope["envelope_id"] == admitted.envelope_id
    assert envelope["stream_id"] == admitted.stream_id
    assert envelope["source_revision_id"] == admitted.source_revision_id
    assert envelope["idempotency_identity"] == admitted.idempotency_identity
    assert envelope["idempotency_key_digest"] == admitted.idempotency_key_digest
    assert envelope["occurred_at"] is None
    assert envelope["occurred_at_status"] == "unknown"
    assert envelope["produced_at"] is None
    assert envelope["produced_at_status"] == "unknown"
    assert envelope["observed_at"] is None
    assert envelope["observed_at_status"] == "unknown"
    assert envelope["received_at"] == "2000-01-01T00:00:00Z"
    assert envelope["received_at_status"] == "fixed_c_demo_protocol_time"
    assert envelope["must_understand"] == []
    assert envelope["scope"] == {
        "mode": "full",
        "track": "C-DEMO",
        "upstream_application_reference": "APP-R53-BAD-ENGINE",
        "document_references": ["id", "inv", "lease", "pol", "reg"],
        "fact_kinds": ["application_identity", "field_observations"],
    }
    batch = dict(envelope["batch"])
    manifest_digest = batch.pop("manifest_digest")
    assert batch == {
        "batch_id": admitted.batch_id,
        "item_sequence": 1,
        "item_count": 1,
        "final_sequence": 1,
        "scope_mode": "full",
        "closed": True,
    }
    assert len(manifest_digest) == 64
    assert set(manifest_digest) <= set("0123456789abcdef")
    assert envelope["fingerprint"] == admitted.envelope_fingerprint
    assert envelope["disposition"] == "accepted"
    assert receipt_payload["envelope_id"] == admitted.envelope_id
    assert receipt_payload["stream_id"] == admitted.stream_id
    assert receipt_payload["source_revision_id"] == admitted.source_revision_id
    assert receipt_payload["batch_id"] == admitted.batch_id
    persisted = json.dumps(
        {
            "application": application_payload,
            "audit": audit_payload,
            "receipt": receipt_payload,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    assert idempotency_key not in persisted


def test_concurrent_first_admission_replays_the_winning_receipt(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "concurrent-admission.sqlite3"
    ready = threading.Barrier(2)

    def allocator(application_id: str) -> Callable[[], str]:
        def allocate() -> str:
            ready.wait(timeout=5)
            return application_id

        return allocate

    services = [
        ControlledScenarioService(
            fixture_root=ROOT / "fixtures" / "applications",
            rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
            state_path=state_path,
            application_id_allocator=allocator("app_concurrent_first_a"),
        ),
        ControlledScenarioService(
            fixture_root=ROOT / "fixtures" / "applications",
            rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
            state_path=state_path,
            application_id_allocator=allocator("app_concurrent_first_b"),
        ),
    ]
    results: list[AdmissionResult] = []
    errors: list[BaseException] = []

    def submit(service: ControlledScenarioService) -> None:
        try:
            results.append(
                service.submit_demo(
                    principal=TEST_INTEGRATOR,
                    scenario_id="app_r53_bad_engine.json",
                    idempotency_key="s01-concurrent-first-admission",
                )
            )
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=submit, args=(service,)) for service in services]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert all(result.disposition is AdmissionDisposition.ACCEPTED for result in results)
    assert {result.replayed for result in results} == {False, True}
    assert len({result.application_id for result in results}) == 1
    assert len({result.receipt_id for result in results}) == 1

    authority = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    assert authority.fact_counts() == {
        "applications": 1,
        "receipts": 1,
        "lifecycle_events": 1,
        "evidence_events": 1,
        "audit_events": 1,
        "jobs": 1,
        "attempts": 0,
        "runs": 0,
        "findings": 0,
        "outbox": 1,
    }


def test_concurrent_cross_principal_admission_has_a_stable_duplicate_receipt(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "concurrent-cross-principal.sqlite3"
    ready = threading.Barrier(2)

    def allocator(application_id: str) -> Callable[[], str]:
        def allocate() -> str:
            ready.wait(timeout=5)
            return application_id

        return allocate

    services = [
        ControlledScenarioService(
            fixture_root=ROOT / "fixtures" / "applications",
            rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
            state_path=state_path,
            application_id_allocator=allocator("app_cross_principal_a"),
        ),
        ControlledScenarioService(
            fixture_root=ROOT / "fixtures" / "applications",
            rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
            state_path=state_path,
            application_id_allocator=allocator("app_cross_principal_b"),
        ),
    ]
    principals = [
        S01CommandPrincipal(
            subject="integrator-a",
            role="integrator",
            scope="C-DEMO",
            source_id="registered-c-demo-source",
        ),
        S01CommandPrincipal(
            subject="integrator-b",
            role="integrator",
            scope="C-DEMO",
            source_id="registered-c-demo-source",
        ),
    ]
    results: list[tuple[int, AdmissionResult]] = []
    errors: list[BaseException] = []

    def submit(index: int) -> None:
        try:
            results.append(
                (
                    index,
                    services[index].submit_demo(
                        scenario_id="app_r53_bad_engine.json",
                        idempotency_key="s01-cross-principal-admission",
                        principal=principals[index],
                    ),
                )
            )
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=submit, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    accepted = [item for item in results if item[1].disposition is AdmissionDisposition.ACCEPTED]
    rejected = [item for item in results if item[1].disposition is AdmissionDisposition.REJECTED]
    assert len(accepted) == 1
    assert len(rejected) == 1
    rejected_index, rejected_result = rejected[0]
    assert rejected_result.reason_code == "APPLICATION_ALREADY_ADMITTED"
    assert rejected_result.receipt_id is not None
    assert rejected_result.replayed is False

    replay = services[rejected_index].submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-cross-principal-admission",
        principal=principals[rejected_index],
    )
    assert replay.disposition is AdmissionDisposition.REJECTED
    assert replay.reason_code == "APPLICATION_ALREADY_ADMITTED"
    assert replay.receipt_id == rejected_result.receipt_id
    assert replay.replayed is True

    authority = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    assert authority.fact_counts() == {
        "applications": 1,
        "receipts": 2,
        "lifecycle_events": 1,
        "evidence_events": 1,
        "audit_events": 2,
        "jobs": 1,
        "attempts": 0,
        "runs": 0,
        "findings": 0,
        "outbox": 1,
    }


def test_target_application_id_is_stable_and_never_projects_upstream_reference() -> None:
    upstream_reference = "APP-R53-BAD-ENGINE"
    service = make_service()
    admitted = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-target-identity",
    )
    replayed = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-target-identity",
    )
    completed = service.process_next_job()
    before_projection = service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject)
    with pytest.raises(QueryNotFound):
        service.workspace_view(
            admitted.application_id or "", role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
        )
    projected = service.refresh_projection()
    queue = service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject)
    workspace = service.workspace_view(
        admitted.application_id or "", role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
    )

    assert before_projection == {"items": [], "projection_watermark": 0}
    assert projected == {"updated": 1, "projection_watermark": 1}
    assert admitted.application_id is not None
    assert admitted.application_id.startswith("app_")
    assert admitted.application_id != upstream_reference
    assert replayed.application_id == admitted.application_id
    assert completed.application_id == admitted.application_id
    assert queue["items"][0]["application_id"] == admitted.application_id
    assert workspace["application_id"] == admitted.application_id
    public_payload = json.dumps(
        {
            "admitted": admitted.__dict__,
            "replayed": replayed.__dict__,
            "completed": completed.__dict__,
            "queue": queue,
            "workspace": workspace,
        },
        default=str,
        ensure_ascii=False,
    )
    assert upstream_reference not in public_payload


def test_target_owner_allocates_application_id_independent_of_source_content() -> None:
    allocated_ids = ["app_owner_allocated_001"]
    allocator_calls = 0

    def allocate_application_id() -> str:
        nonlocal allocator_calls
        allocator_calls += 1
        return allocated_ids.pop(0)

    service = make_service(application_id_allocator=allocate_application_id)
    admitted = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-owner-allocated-id",
    )
    replayed = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-owner-allocated-id",
    )
    completed = service.process_next_job()
    assert service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject) == {
        "items": [],
        "projection_watermark": 0,
    }
    assert service.refresh_projection() == {
        "updated": 1,
        "projection_watermark": 1,
    }
    queue = service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject)
    workspace = service.workspace_view(
        admitted.application_id or "", role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
    )

    assert allocator_calls == 1
    assert admitted.application_id == "app_owner_allocated_001"
    assert replayed.application_id == admitted.application_id
    assert completed.application_id == admitted.application_id
    assert queue["items"][0]["application_id"] == admitted.application_id
    assert workspace["application_id"] == admitted.application_id

    fresh_a = make_service().submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-fresh-owner-a",
    )
    fresh_b = make_service().submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-fresh-owner-b",
    )
    assert fresh_a.application_id is not None
    assert fresh_b.application_id is not None
    assert fresh_a.application_id != fresh_b.application_id


@pytest.mark.parametrize("source_change", ("deleted", "corrupt", "changed"))
def test_same_key_replay_does_not_reread_mutable_legacy_source(
    tmp_path: Path, source_change: str
) -> None:
    fixture_root = tmp_path / "controlled"
    fixture_root.mkdir()
    source = ROOT / "fixtures" / "applications" / "app_r53_bad_engine.json"
    target = fixture_root / source.name
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    service = make_service(fixture_root)
    first = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-conflict",
    )

    if source_change == "deleted":
        target.unlink()
    elif source_change == "corrupt":
        target.write_text("not-json", encoding="utf-8")
    else:
        changed = json.loads(target.read_text(encoding="utf-8"))
        changed["documents"][0]["fields"]["engine_no"]["raw"] = "CHANGED-SOURCE"
        target.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    replay = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-conflict",
    )

    assert replay.disposition is AdmissionDisposition.ACCEPTED
    assert replay.replayed is True
    assert replay.receipt_id == first.receipt_id
    assert replay.application_id == first.application_id
    assert service.fact_counts()["applications"] == 1
    assert service.fact_counts()["lifecycle_events"] == 1


def test_same_key_different_logical_command_is_a_conflict_without_revision() -> None:
    service = make_service()
    first = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-command-conflict",
    )
    counts = service.fact_counts()

    conflict = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="different-scenario.json",
        idempotency_key="s01-command-conflict",
    )

    assert first.disposition is AdmissionDisposition.ACCEPTED
    assert conflict.disposition is AdmissionDisposition.REJECTED
    assert conflict.reason_code == "IDEMPOTENCY_CONFLICT"
    assert conflict.replayed is False
    assert conflict.application_id is None
    assert conflict.lifecycle_revision == 0
    assert conflict.evidence_revision == 0
    assert service.fact_counts() == counts


def test_invalid_or_unlisted_demo_scenario_is_rejected_without_business_facts() -> None:
    service = make_service()

    result = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="../fixtures/applications/app_r53_bad_engine.json",
        idempotency_key="s01-invalid",
    )

    assert result.disposition is AdmissionDisposition.REJECTED
    assert result.reason_code == "SCENARIO_NOT_ALLOWED"
    assert service.fact_counts() == {
        "applications": 0,
        "receipts": 1,
        "lifecycle_events": 0,
        "evidence_events": 0,
        "audit_events": 1,
        "jobs": 0,
        "attempts": 0,
        "runs": 0,
        "findings": 0,
        "outbox": 0,
    }


def test_rejected_submission_has_stable_scoped_receipt_and_binding_across_restart(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "rejected-receipt.sqlite3"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )

    rejected = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="../outside.json",
        idempotency_key="s01-stable-rejection",
    )
    counts = service.fact_counts()
    replay = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="../outside.json",
        idempotency_key="s01-stable-rejection",
    )
    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    replay_after_restart = restarted.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="../outside.json",
        idempotency_key="s01-stable-rejection",
    )
    conflict = restarted.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-stable-rejection",
    )

    assert rejected.disposition is AdmissionDisposition.REJECTED
    assert rejected.reason_code == "SCENARIO_NOT_ALLOWED"
    assert rejected.receipt_id is not None
    assert rejected.audit_recorded is True
    assert counts == {
        "applications": 0,
        "receipts": 1,
        "lifecycle_events": 0,
        "evidence_events": 0,
        "audit_events": 1,
        "jobs": 0,
        "attempts": 0,
        "runs": 0,
        "findings": 0,
        "outbox": 0,
    }
    assert replay.replayed is True
    assert replay.receipt_id == rejected.receipt_id
    assert replay_after_restart.replayed is True
    assert replay_after_restart.receipt_id == rejected.receipt_id
    assert conflict.reason_code == "IDEMPOTENCY_CONFLICT"
    assert conflict.receipt_id == rejected.receipt_id
    assert restarted.fact_counts() == counts


@pytest.mark.parametrize("invalid_fields", (None, [1], "not-a-field-map"))
def test_malformed_document_fields_are_stably_rejected_without_business_facts(
    tmp_path: Path, invalid_fields: object
) -> None:
    fixture_root = tmp_path / "controlled"
    fixture_root.mkdir()
    (fixture_root / "app_r53_bad_engine.json").write_text(
        json.dumps(
            {
                "application_id": "UPSTREAM-MALFORMED-FIELDS",
                "meta": {"field_source": "synthetic"},
                "documents": [
                    {
                        "doc_id": "doc-1",
                        "doc_type": "registration",
                        "fields": invalid_fields,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    service = make_service(fixture_root)

    rejected = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-malformed-fields",
    )

    assert rejected.disposition is AdmissionDisposition.REJECTED
    assert rejected.reason_code == "INVALID_CANONICAL_ENVELOPE"
    assert rejected.lifecycle_revision == 0
    assert rejected.evidence_revision == 0
    assert service.fact_counts() == {
        "applications": 0,
        "receipts": 1,
        "lifecycle_events": 0,
        "evidence_events": 0,
        "audit_events": 1,
        "jobs": 0,
        "attempts": 0,
        "runs": 0,
        "findings": 0,
        "outbox": 0,
    }
    assert service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject) == {
        "items": [],
        "projection_watermark": 0,
    }


def test_frozen_checker_is_unchanged_after_process_global_kb_mutation(
    tmp_path: Path,
) -> None:
    previous_kb_path = get_kb().path
    mutable_kb_path = tmp_path / "mutable-kb.json"
    mutable_kb_path.write_text(
        json.dumps(
            {
                "version": 1,
                "address_aliases": {},
                "org_aliases": {},
                "plate_prefixes": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    try:
        mutable_kb = reload_kb(mutable_kb_path)
        rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
        rules_digest = hashlib.sha256(rules_path.read_bytes()).hexdigest()
        release = TargetRelease.compile(load_rules(rules_path), rules_digest)
        checker = TargetChecker(release)
        run_spec = _complete_run_spec(
            release,
            [
                {
                    "document_id": "reg",
                    "document_role": "机动车登记证书",
                    "fields": {
                        "brand": _eligible_field(
                            "火星甲品牌",
                            observation_id="obs-reg-brand",
                            source_region="/documents/0/fields/brand",
                        )
                    },
                },
                {
                    "document_id": "pol",
                    "document_role": "交强险保单",
                    "fields": {
                        "brand": _eligible_field(
                            "火星乙品牌",
                            observation_id="obs-pol-brand",
                            source_region="/documents/1/fields/brand",
                        )
                    },
                },
            ],
            application_id="app_frozen_kb",
        )

        before = checker.run(run_spec)
        before_hash = hashlib.sha256(
            json.dumps(
                asdict(before),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        mutable_kb.add_alias("org_aliases", "火星甲品牌", "火星乙品牌")
        after = checker.run(run_spec)
        after_hash = hashlib.sha256(
            json.dumps(
                asdict(after),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        assert before_hash == after_hash
        assert before == after
        assert release.public_manifest()["knowledge_digest"]
    finally:
        reload_kb(previous_kb_path)


@pytest.mark.parametrize(
    "mutation",
    (
        "missing_release_id",
        "release_id",
        "release_digest",
        "checker_build",
        "limits",
        "baseline_release",
        "snapshot_digest",
    ),
)
def test_target_checker_rejects_incomplete_or_mismatched_frozen_context(
    mutation: str,
) -> None:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    release = TargetRelease.compile(
        load_rules(rules_path), hashlib.sha256(rules_path.read_bytes()).hexdigest()
    )
    checker = TargetChecker(release)
    run_spec = _complete_run_spec(
        release,
        [
            {
                "document_id": "reg",
                "document_role": "机动车登记证书",
                "fields": {
                    "model": _eligible_field(
                        "MODEL-A",
                        observation_id="obs-reg-model",
                        source_region="/documents/0/fields/model",
                    )
                },
            }
        ],
        application_id="app_run_spec_contract",
    )
    if mutation == "missing_release_id":
        run_spec.pop("release_id")
    elif mutation == "limits":
        run_spec["limits"] = {"max_documents": 999}
    elif mutation == "baseline_release":
        run_spec["baseline_release"]["knowledge_digest"] = "0" * 64
    elif mutation == "snapshot_digest":
        run_spec["evidence_snapshot_digest"] = "0" * 64
    else:
        run_spec[mutation] = f"mismatched-{mutation}"

    with pytest.raises(ValueError, match="RunSpec"):
        checker.run(run_spec)


def test_frozen_checker_ignores_process_global_normalizer_registration() -> None:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    release = TargetRelease.compile(
        load_rules(rules_path), hashlib.sha256(rules_path.read_bytes()).hexdigest()
    )
    checker = TargetChecker(release)
    run_spec = _complete_run_spec(
        release,
        [
            {
                "document_id": "reg",
                "document_role": "机动车登记证书",
                "fields": {
                    "model": _eligible_field(
                        "MODEL-A",
                        observation_id="obs-reg-model",
                        source_region="/documents/0/fields/model",
                    )
                },
            },
            {
                "document_id": "pol",
                "document_role": "交强险保单",
                "fields": {
                    "model": _eligible_field(
                        "MODEL-B",
                        observation_id="obs-pol-model",
                        source_region="/documents/1/fields/model",
                    )
                },
            },
        ],
        application_id="app_frozen_normalizer",
    )

    before = checker.run(run_spec)
    try:
        register_normalizer("model", lambda _raw: "POISONED-MODEL")
        after = checker.run(run_spec)
    finally:
        register_normalizer("model", normalize_model)

    before_model = next(check for check in before.checks if check.rule_id == "R_MODEL_CROSS")
    assert before_model.verdict == "inconsistent"
    assert after == before


def test_target_release_identity_is_bound_to_loaded_normalizer_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    config = load_rules(rules_path)
    rules_digest = hashlib.sha256(rules_path.read_bytes()).hexdigest()
    baseline = TargetRelease.compile(config, rules_digest)

    def replacement_normalize_model(raw: object) -> str:
        return normalize_model(raw)

    monkeypatch.setattr(s01_checker, "normalize_model", replacement_normalize_model)
    changed = TargetRelease.compile(config, rules_digest)

    assert changed.normalizer_digest != baseline.normalizer_digest
    assert changed.release_digest != baseline.release_digest


def test_target_checker_keeps_vin_ocr_repair_uncertain() -> None:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    release = TargetRelease.compile(
        load_rules(rules_path), hashlib.sha256(rules_path.read_bytes()).hexdigest()
    )
    roles_and_values = (
        ("机动车登记证书", "LSVAA4I82N5000054"),
        ("交强险保单", "LSVAA4182N5000054"),
        ("融资租赁合同", "LSVAA4182N5000054"),
        ("发票", "LSVAA4182N5000054"),
    )
    evidence = [
        {
            "document_id": f"doc-{index}",
            "document_role": role,
            "fields": {
                "vin": _eligible_field(
                    raw,
                    observation_id=f"obs-vin-{index}",
                    source_region=f"/documents/{index}/fields/vin",
                )
            },
        }
        for index, (role, raw) in enumerate(roles_and_values)
    ]
    run_spec = _complete_run_spec(
        release,
        evidence,
        application_id="app_vin_ocr_repair",
    )

    result = TargetChecker(release).run(run_spec)
    vin = next(check for check in result.checks if check.rule_id == "R_VIN_CROSS")

    assert (vin.verdict, vin.reason_codes) == (
        "uncertain",
        ("VIN_OCR_FIX_MERGE",),
    )


def test_low_confidence_gate_applies_to_conditional_and_list_checks() -> None:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    release = TargetRelease.compile(
        load_rules(rules_path), hashlib.sha256(rules_path.read_bytes()).hexdigest()
    )
    low_condition = _eligible_field(
        "182000",
        observation_id="obs-lease-financed-amount",
        source_region="/documents/0/fields/financed_amount",
    )
    low_required = _eligible_field(
        "320102199001012016",
        observation_id="obs-lease-id-number",
        source_region="/documents/0/fields/id_number",
    )
    low_container = _eligible_field(
        "苏A92054",
        observation_id="obs-pol-plate-list",
        source_region="/documents/1/fields/plate_list",
    )
    low_item = _eligible_field(
        "苏A92054",
        observation_id="obs-reg-plate-no",
        source_region="/documents/2/fields/plate_no",
    )
    for value in (low_condition, low_required, low_container, low_item):
        value["confidence"] = 0.01
    run_spec = _complete_run_spec(
        release,
        [
            {
                "document_id": "lease",
                "document_role": "融资租赁合同",
                "fields": {
                    "financed_amount": low_condition,
                    "id_number": low_required,
                },
            },
            {
                "document_id": "pol",
                "document_role": "交强险保单",
                "fields": {"plate_list": low_container},
            },
            {
                "document_id": "reg",
                "document_role": "机动车登记证书",
                "fields": {"plate_no": low_item},
            },
        ],
        application_id="app_low_confidence_gate",
    )

    result = TargetChecker(release).run(run_spec)
    outcomes = {
        check.rule_id: (check.verdict, check.reason_codes)
        for check in result.checks
        if check.rule_id in {"R_ID_REQUIRED_IF_AMOUNT", "R_PLATE_IN_LIST"}
    }

    assert outcomes == {
        "R_ID_REQUIRED_IF_AMOUNT": ("uncertain", ("LOW_CONF",)),
        "R_PLATE_IN_LIST": ("uncertain", ("LOW_CONF",)),
    }


def test_critical_exact_check_preserves_governed_low_confidence_comparison() -> None:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    release = TargetRelease.compile(
        load_rules(rules_path), hashlib.sha256(rules_path.read_bytes()).hexdigest()
    )
    roles = ("机动车登记证书", "交强险保单", "融资租赁合同", "发票")
    evidence = []
    for index, role in enumerate(roles):
        value = _eligible_field(
            "LSVAA4182N5000055" if index == 1 else "LSVAA4182N5000054",
            observation_id=f"obs-low-confidence-vin-{index}",
            source_region=f"/documents/{index}/fields/vin",
        )
        if index == 1:
            value["confidence"] = 0.01
        evidence.append(
            {
                "document_id": f"doc-{index}",
                "document_role": role,
                "fields": {"vin": value},
            }
        )
    run_spec = _complete_run_spec(
        release,
        evidence,
        application_id="app_critical_low_confidence_mismatch",
    )

    result = TargetChecker(release).run(run_spec)
    vin = next(check for check in result.checks if check.rule_id == "R_VIN_CROSS")

    assert (vin.verdict, vin.reason_codes) == (
        "inconsistent",
        ("VIN_MISMATCH",),
    )


def test_numeric_check_preserves_approximate_money_uncertainty() -> None:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    release = TargetRelease.compile(
        load_rules(rules_path), hashlib.sha256(rules_path.read_bytes()).hexdigest()
    )
    run_spec = _complete_run_spec(
        release,
        [
            {
                "document_id": "lease",
                "document_role": "融资租赁合同",
                "fields": {
                    "financed_amount": _eligible_field(
                        "约18.2万",
                        observation_id="obs-lease-approx-amount",
                        source_region="/documents/0/fields/financed_amount",
                    )
                },
            },
            {
                "document_id": "invoice",
                "document_role": "发票",
                "fields": {
                    "invoice_amount": _eligible_field(
                        "182000",
                        observation_id="obs-invoice-exact-amount",
                        source_region="/documents/1/fields/invoice_amount",
                    )
                },
            },
        ],
        application_id="app_approximate_money",
    )

    result = TargetChecker(release).run(run_spec)
    amount = next(check for check in result.checks if check.rule_id == "R_AMOUNT_TOL")

    assert (amount.verdict, amount.reason_codes) == (
        "uncertain",
        ("AMOUNT_APPROX",),
    )


def test_run_result_preserves_normalization_and_selection_outcomes() -> None:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    release = TargetRelease.compile(
        load_rules(rules_path), hashlib.sha256(rules_path.read_bytes()).hexdigest()
    )
    run_spec = _complete_run_spec(
        release,
        [
            {
                "document_id": "lease",
                "document_role": "融资租赁合同",
                "fields": {
                    "financed_amount": _eligible_field(
                        "约18.2万",
                        observation_id="obs-run-result-approx",
                        source_region="/documents/0/fields/financed_amount",
                    )
                },
            },
            {
                "document_id": "invoice",
                "document_role": "发票",
                "fields": {
                    "invoice_amount": _eligible_field(
                        "182000",
                        observation_id="obs-run-result-exact",
                        source_region="/documents/1/fields/invoice_amount",
                    )
                },
            },
        ],
        application_id="app_run_result_outcomes",
    )

    result = TargetChecker(release).run(run_spec)
    normalization = next(
        outcome
        for outcome in result.normalization_outcomes
        if outcome.rule_id == "R_AMOUNT_TOL"
        and outcome.observation_id == "obs-run-result-approx"
    )
    selections = [
        outcome
        for outcome in result.selection_outcomes
        if outcome.rule_id == "R_AMOUNT_TOL"
    ]

    assert normalization.normalized == "182000"
    assert normalization.notes == ("money_approx",)
    assert [
        (outcome.observation_id, outcome.selected, outcome.reason_code)
        for outcome in selections
    ] == [
        ("obs-run-result-approx", True, "SELECTED_FOR_CHECK"),
        ("obs-run-result-exact", True, "SELECTED_FOR_CHECK"),
    ]


def test_completed_run_persists_normalization_and_selection_outcomes(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "run-outcomes.sqlite3"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-run-outcomes",
    )

    completed = service.process_next_job()

    assert completed.status == "complete"
    with sqlite3.connect(state_path) as connection:
        encoded = next(
            payload
            for (payload,) in connection.execute("SELECT payload FROM runs")
            if json.loads(payload).get("run_id") == completed.run_id
            and json.loads(payload).get("status") == "complete"
        )
    run = json.loads(encoded)
    assert run["normalization_outcomes"]
    assert run["selection_outcomes"]
    assert len(run["finding_ids"]) == 13
    assert all("raw" not in outcome for outcome in run["normalization_outcomes"])
    assert all("raw" not in outcome for outcome in run["selection_outcomes"])
    assert {
        outcome["rule_id"] for outcome in run["selection_outcomes"]
    }.issuperset({"R_ENGINE_CROSS", "R_VIN_CROSS", "R_PLATE_IN_LIST"})


def test_name_check_preserves_governed_used_car_transfer_policy() -> None:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    release = TargetRelease.compile(
        load_rules(rules_path), hashlib.sha256(rules_path.read_bytes()).hexdigest()
    )
    role_field_values = (
        ("机动车登记证书", "owner_name", "旧车主甲"),
        ("交强险保单", "insured_name", "新承租乙"),
        ("融资租赁合同", "lessee_name", "新承租乙"),
        ("身份证", "owner_name", "新承租乙"),
        ("发票", "buyer_name", "新承租乙"),
    )
    evidence = [
        {
            "document_id": f"doc-{index}",
            "document_role": role,
            "fields": {
                field: _eligible_field(
                    raw,
                    observation_id=f"obs-transfer-name-{index}",
                    source_region=f"/documents/{index}/fields/{field}",
                )
            },
        }
        for index, (role, field, raw) in enumerate(role_field_values)
    ]
    run_spec = _complete_run_spec(
        release,
        evidence,
        application_id="app_used_car_name_transfer",
    )

    result = TargetChecker(release).run(run_spec)
    name = next(check for check in result.checks if check.rule_id == "R_NAME_FUZZY")

    assert (name.verdict, name.reason_codes) == (
        "uncertain",
        ("USED_CAR_NAME_TRANSFER", "NAME_NEAR_UNCERTAIN"),
    )


def test_conflicting_alias_candidates_are_preserved_as_evidence_conflict() -> None:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    release = TargetRelease.compile(
        load_rules(rules_path), hashlib.sha256(rules_path.read_bytes()).hexdigest()
    )
    roles = ("机动车登记证书", "交强险保单", "融资租赁合同", "发票")
    evidence = []
    for index, role in enumerate(roles):
        fields = {
            "vin": _eligible_field(
                "LSVAA4182N5000054",
                observation_id=f"obs-alias-vin-{index}",
                source_region=f"/documents/{index}/fields/vin",
            )
        }
        if index == 0:
            fields["vehicle_id"] = _eligible_field(
                "LSVAA4182N5000055",
                observation_id="obs-conflicting-vehicle-id",
                source_region="/documents/0/fields/vehicle_id",
            )
        evidence.append(
            {
                "document_id": f"doc-{index}",
                "document_role": role,
                "fields": fields,
            }
        )
    run_spec = _complete_run_spec(
        release,
        evidence,
        application_id="app_conflicting_alias_candidates",
    )

    result = TargetChecker(release).run(run_spec)
    vin = next(check for check in result.checks if check.rule_id == "R_VIN_CROSS")

    assert (vin.verdict, vin.reason_codes) == (
        "uncertain",
        ("EVIDENCE_CONFLICT",),
    )
    assert {link.field for link in vin.evidence_links if link.document_id == "doc-0"} == {
        "vin",
        "vehicle_id",
    }


def test_list_check_keeps_present_normalization_failure_uncertain() -> None:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    release = TargetRelease.compile(
        load_rules(rules_path), hashlib.sha256(rules_path.read_bytes()).hexdigest()
    )
    run_spec = _complete_run_spec(
        release,
        [
            {
                "document_id": "pol",
                "document_role": "交强险保单",
                "fields": {
                    "plate_list": _eligible_field(
                        "苏A92054",
                        observation_id="obs-pol-plate-list-normalize",
                        source_region="/documents/0/fields/plate_list",
                    )
                },
            },
            {
                "document_id": "reg",
                "document_role": "机动车登记证书",
                "fields": {
                    "plate_no": _eligible_field(
                        "---",
                        observation_id="obs-reg-plate-no-normalize",
                        source_region="/documents/1/fields/plate_no",
                    )
                },
            },
        ],
        application_id="app_list_normalization_failure",
    )

    result = TargetChecker(release).run(run_spec)
    plate = next(check for check in result.checks if check.rule_id == "R_PLATE_IN_LIST")

    assert (plate.verdict, plate.reason_codes) == (
        "uncertain",
        ("NORMALIZE_FAIL",),
    )


def test_list_check_keeps_container_normalization_failure_uncertain() -> None:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    release = TargetRelease.compile(
        load_rules(rules_path), hashlib.sha256(rules_path.read_bytes()).hexdigest()
    )
    run_spec = _complete_run_spec(
        release,
        [
            {
                "document_id": "pol",
                "document_role": "交强险保单",
                "fields": {
                    "plate_list": _eligible_field(
                        "---",
                        observation_id="obs-pol-invalid-plate-list",
                        source_region="/documents/0/fields/plate_list",
                    )
                },
            },
            {
                "document_id": "reg",
                "document_role": "机动车登记证书",
                "fields": {
                    "plate_no": _eligible_field(
                        "苏A92054",
                        observation_id="obs-reg-valid-plate",
                        source_region="/documents/1/fields/plate_no",
                    )
                },
            },
        ],
        application_id="app_list_container_normalization_failure",
    )

    result = TargetChecker(release).run(run_spec)
    plate = next(check for check in result.checks if check.rule_id == "R_PLATE_IN_LIST")

    assert (plate.verdict, plate.reason_codes) == (
        "uncertain",
        ("NORMALIZE_FAIL",),
    )


def test_conditional_check_ignores_required_evidence_when_trigger_is_absent() -> None:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    release = TargetRelease.compile(
        load_rules(rules_path), hashlib.sha256(rules_path.read_bytes()).hexdigest()
    )
    id_number = _eligible_field(
        "320102199001012016",
        observation_id="obs-lease-id-without-trigger",
        source_region="/documents/0/fields/id_number",
    )
    id_number["confidence"] = 0.01
    run_spec = _complete_run_spec(
        release,
        [
            {
                "document_id": "lease",
                "document_role": "融资租赁合同",
                "fields": {"id_number": id_number},
            }
        ],
        application_id="app_conditional_trigger_absent",
    )

    result = TargetChecker(release).run(run_spec)
    conditional = next(
        check for check in result.checks if check.rule_id == "R_ID_REQUIRED_IF_AMOUNT"
    )

    assert (conditional.verdict, conditional.reason_codes) == (
        "consistent",
        ("CONSISTENT",),
    )


def test_worker_routes_low_confidence_conditional_evidence_to_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = ROOT / "fixtures" / "applications" / "app_r53_bad_engine.json"
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    payload = json.loads(source.read_text(encoding="utf-8"))
    registration = next(
        document for document in payload["documents"] if document["doc_id"] == "reg"
    )
    invoice = next(
        document for document in payload["documents"] if document["doc_id"] == "inv"
    )
    invoice["fields"]["engine_no"]["raw"] = registration["fields"]["engine_no"][
        "raw"
    ]
    fixture_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    (fixture_root / source.name).write_bytes(fixture_bytes)
    monkeypatch.setattr(
        ControlledScenarioService,
        "_C_DEMO_PROVENANCE_SOURCE_SHA256",
        hashlib.sha256(fixture_bytes).hexdigest(),
    )

    rules_payload = yaml.safe_load(
        (ROOT / "configs" / "rules_auto_lease.yaml").read_text(encoding="utf-8")
    )
    conditional_rule = next(
        rule
        for rule in rules_payload["rules"]
        if rule["id"] == "R_ID_REQUIRED_IF_AMOUNT"
    )
    conditional_rule["min_confidence"] = 1.0
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        yaml.safe_dump(rules_payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    service = ControlledScenarioService(
        fixture_root=fixture_root,
        rules_path=rules_path,
        state_path=tmp_path / "low-confidence-worker.sqlite3",
    )
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id=source.name,
        idempotency_key="s01-low-confidence-conditional",
    )

    result = service.process_next_job()
    service.refresh_projection()
    workspace = service.workspace_view(
        admission.application_id or "", role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
    )
    conditional_finding = next(
        finding
        for finding in workspace["mandatory_blockers"]
        if finding["rule_id"] == "R_ID_REQUIRED_IF_AMOUNT"
    )

    assert result.status == "complete"
    assert result.lifecycle_phases[-1] == "Manual Review"
    assert workspace["route"] == "manual_review"
    assert conditional_finding["verdict"] == "uncertain"
    assert conditional_finding["reason_code"] == "LOW_CONF"


def test_worker_auto_completes_only_when_every_mandatory_check_is_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = ROOT / "fixtures" / "applications" / "app_r53_bad_engine.json"
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    payload = json.loads(source.read_text(encoding="utf-8"))
    registration = next(
        document for document in payload["documents"] if document["doc_id"] == "reg"
    )
    invoice = next(
        document for document in payload["documents"] if document["doc_id"] == "inv"
    )
    invoice["fields"]["engine_no"]["raw"] = registration["fields"]["engine_no"][
        "raw"
    ]
    for document in payload["documents"]:
        document["fields"].pop("brand", None)
        document["fields"].pop("model", None)
    fixture_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    (fixture_root / source.name).write_bytes(fixture_bytes)
    monkeypatch.setattr(
        ControlledScenarioService,
        "_C_DEMO_PROVENANCE_SOURCE_SHA256",
        hashlib.sha256(fixture_bytes).hexdigest(),
    )
    service = ControlledScenarioService(
        fixture_root=fixture_root,
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "mandatory-allowlist.sqlite3",
    )
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id=source.name,
        idempotency_key="s01-mandatory-auto-completion-allowlist",
    )

    result = service.process_next_job()

    assert result.status == "complete"
    assert result.lifecycle_phases[-1] == "Manual Review"
    service.refresh_projection()
    workspace = service.workspace_view(
        admission.application_id or "", role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
    )
    skipped = {
        finding["rule_id"]: (finding["verdict"], finding["reason_code"])
        for finding in workspace["mandatory_blockers"]
        if finding["rule_id"] in {"R_BRAND_CROSS", "R_MODEL_CROSS"}
    }
    assert workspace["route"] == "manual_review"
    assert skipped == {
        "R_BRAND_CROSS": ("skipped", "SKIPPED"),
        "R_MODEL_CROSS": ("skipped", "SKIPPED"),
    }


def test_completed_run_durably_retains_every_terminal_check(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "complete-run-results.sqlite3"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-complete-run-result-set",
    )
    expected_checks = len(load_rules(ROOT / "configs" / "rules_auto_lease.yaml").rules)

    result = service.process_next_job()
    service.refresh_projection()

    assert result.status == "complete"
    assert service.fact_counts()["findings"] == expected_checks
    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    assert restarted.fact_counts()["findings"] == expected_checks
    workspace = restarted.workspace_view(
        admission.application_id or "", role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
    )
    assert [
        finding["rule_id"] for finding in workspace["mandatory_blockers"]
    ] == ["R_ENGINE_CROSS"]


def test_duplicate_admission_uses_immutable_identity_after_mutable_row_tamper(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "immutable-admission-identity.sqlite3"
    first = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    admitted = first.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-immutable-identity-first",
    )
    assert admitted.application_id is not None
    with sqlite3.connect(state_path) as connection:
        encoded = connection.execute(
            "SELECT payload FROM applications WHERE item_id = ?",
            (admitted.application_id,),
        ).fetchone()[0]
        application = json.loads(encoded)
        application["upstream_application_reference"] = "FORGED-UPSTREAM-REFERENCE"
        connection.execute(
            "UPDATE applications SET payload = ? WHERE item_id = ?",
            (
                json.dumps(
                    application,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                admitted.application_id,
            ),
        )
        connection.commit()

    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    duplicate = restarted.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-immutable-identity-second",
    )

    assert duplicate.disposition is AdmissionDisposition.REJECTED
    assert duplicate.reason_code == "APPLICATION_ALREADY_ADMITTED"
    assert duplicate.lifecycle_revision == 0
    assert duplicate.evidence_revision == 0
    assert restarted.fact_counts()["applications"] == 1


@pytest.mark.parametrize("tamper", ("rewrite", "delete"))
def test_worker_rebuilds_admitted_evidence_from_immutable_facts_after_restart(
    tmp_path: Path, tamper: str
) -> None:
    fixture_root = ROOT / "fixtures" / "applications"
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    baseline = ControlledScenarioService(
        fixture_root=fixture_root,
        rules_path=rules_path,
        state_path=tmp_path / "baseline.sqlite3",
    )
    baseline.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=f"baseline-{tamper}",
    )
    expected = baseline.process_next_job()

    state_path = tmp_path / f"tampered-{tamper}.sqlite3"
    first = ControlledScenarioService(
        fixture_root=fixture_root,
        rules_path=rules_path,
        state_path=state_path,
    )
    admission = first.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=f"tampered-{tamper}",
    )
    assert admission.application_id is not None
    with sqlite3.connect(state_path) as connection:
        encoded = connection.execute(
            "SELECT payload FROM applications WHERE item_id = ?",
            (admission.application_id,),
        ).fetchone()[0]
        application = json.loads(encoded)
        if tamper == "rewrite":
            if application.get("evidence"):
                invoice = next(
                    document
                    for document in application["evidence"]
                    if document["document_role"] == "发票"
                )
                invoice["fields"]["engine_no"]["raw"] = "S2ENG54A"
            else:
                application["evidence"] = []
        else:
            application.pop("evidence", None)
        connection.execute(
            "UPDATE applications SET payload = ? WHERE item_id = ?",
            (
                json.dumps(
                    application,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                admission.application_id,
            ),
        )
        connection.commit()

    restarted = ControlledScenarioService(
        fixture_root=fixture_root,
        rules_path=rules_path,
        state_path=state_path,
    )
    actual = restarted.process_next_job()
    restarted.refresh_projection()
    queue = restarted.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject)

    assert actual.status == "complete"
    assert actual.evidence_snapshot_digest == expected.evidence_snapshot_digest
    assert len(queue["items"]) == 1
    assert queue["items"][0]["route"] == "manual_review"
    assert queue["items"][0]["mandatory_blockers"][0]["rule_id"] == "R_ENGINE_CROSS"


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("cycle", 2),
        ("phase", "Assembly"),
        ("lifecycle_revision", 2),
        ("phase_history", ["Intake", "Assembly"]),
        ("evidence_ready", True),
        ("route", "auto_complete"),
    ),
)
def test_worker_rejects_mutable_lifecycle_state_that_disagrees_with_immutable_events(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    state_path = tmp_path / f"lifecycle-authority-{field}.sqlite3"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=f"s01-lifecycle-authority-{field}",
    )
    assert admission.application_id is not None

    with sqlite3.connect(state_path) as connection:
        encoded = connection.execute(
            "SELECT payload FROM applications WHERE item_id = ?",
            (admission.application_id,),
        ).fetchone()[0]
        application = json.loads(encoded)
        application[field] = replacement
        connection.execute(
            "UPDATE applications SET payload = ? WHERE item_id = ?",
            (
                json.dumps(
                    application,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                admission.application_id,
            ),
        )
        connection.commit()

    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    before = restarted.fact_counts()

    stopped = restarted.process_next_job()

    assert stopped.status == "stopped"
    assert stopped.reason_code == "APPLICATION_STATE_AUTHORITY_UNAVAILABLE"
    assert restarted.fact_counts() == {
        **before,
        "audit_events": before["audit_events"] + 1,
    }
    assert restarted._store.audit_events[-1]["action"] == "controlled_cohort_stop"
    assert restarted._store.audit_events[-1]["result"] == "stopped"
    assert restarted._store.audit_events[-1]["failure_reason_code"] == (
        "APPLICATION_STATE_AUTHORITY_UNAVAILABLE"
    )
    assert restarted.cohort_status() == {
        "track": "C-DEMO",
        "admission": "stopped",
        "reason_code": "S01_RUNTIME_UNHEALTHY",
        "failure_reason_code": "APPLICATION_STATE_AUTHORITY_UNAVAILABLE",
    }


def test_fixed_adapter_exposes_minimized_eligible_synthetic_provenance() -> None:
    service = make_service()
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-provenance-links",
    )
    service.process_next_job()
    assert service.refresh_projection() == {
        "updated": 1,
        "projection_watermark": 1,
    }

    workspace = service.workspace_view(
        admission.application_id or "", role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
    )
    links = workspace["selected_finding"]["evidence_links"]
    source_sha256 = admission.source_sha256
    assert {link["document_id"] for link in links} == {"reg", "pol", "inv"}
    assert all(link["evidence_eligible"] is True for link in links)
    assert all(
        link["eligibility_reason"] == "REGISTERED_SOURCE_PROVENANCE_VERIFIED"
        for link in links
    )
    assert all(link["source_sha256"] == source_sha256 for link in links)
    assert all(link["raw_masked"] == "[REDACTED]" for link in links)
    assert all(str(link["observation_id"]).startswith("observation_") for link in links)
    assert len({link["provenance_manifest_digest"] for link in links}) == 1
    assert len(links[0]["provenance_manifest_digest"]) == 64
    registered_trace_keys = {
        "source_page",
        "source_region",
        "producer_id",
        "producer_family",
        "producer_run_id",
        "model_id",
        "model_version",
        "source_receipt_id",
    }
    correction_trace_keys = {"source_page", "source_region"}
    assert all(
        (registered_trace_keys - correction_trace_keys).isdisjoint(link)
        and "source_object_ref" not in link
        for link in links
    )
    assert all(type(link["source_page"]) is int for link in links)
    assert all(str(link["source_region"]).startswith("region:") for link in links)
    public_surface = json.dumps(workspace, sort_keys=True)
    assert "/documents/" not in public_surface
    assert "c-demo-object:" not in public_surface


@pytest.mark.parametrize(
    "damage",
    (
        "missing_entry",
        "duplicate_entry",
        "malformed_entry",
        "source_sha_mismatch",
        "unlisted_field",
    ),
)
def test_adapter_fails_closed_when_registered_provenance_is_not_exact(
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    target = ("pol", "engine_no", 2, "/documents/1/fields/engine_no")
    entries = list(ControlledScenarioService._C_DEMO_PROVENANCE_ENTRIES)
    target_index = entries.index(target)

    if damage == "missing_entry":
        entries.pop(target_index)
    elif damage == "duplicate_entry":
        entries.append(target)
    elif damage == "malformed_entry":
        entries[target_index] = (
            "pol",
            "engine_no",
            "2",
            "/documents/1/fields/engine_no",
        )
    elif damage == "source_sha_mismatch":
        monkeypatch.setattr(
            ControlledScenarioService,
            "_C_DEMO_PROVENANCE_SOURCE_SHA256",
            "0" * 64,
        )
    elif damage == "unlisted_field":
        entries[target_index] = (
            "pol",
            "unlisted_engine_no",
            2,
            "/documents/1/fields/engine_no",
        )

    monkeypatch.setattr(
        ControlledScenarioService,
        "_C_DEMO_PROVENANCE_ENTRIES",
        tuple(entries),
    )
    service = make_service()
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=f"s01-provenance-{damage}",
    )

    result = service.process_next_job()
    service.refresh_projection()
    workspace = service.workspace_view(
        admission.application_id or "", role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
    )
    finding = next(
        item
        for item in workspace["mandatory_blockers"]
        if item["rule_id"] == "R_ENGINE_CROSS"
    )
    affected_link = next(
        link for link in finding["evidence_links"] if link["document_id"] == "pol"
    )

    assert result.status == "complete"
    assert finding["verdict"] == "uncertain"
    assert finding["reason_code"] == "PROVENANCE_INELIGIBLE"
    assert affected_link["evidence_eligible"] is False
    assert affected_link["eligibility_reason"] == "PROVENANCE_INELIGIBLE"
    assert affected_link["provenance_manifest_digest"] is None
    assert not any(
        item["rule_id"] == "R_ENGINE_CROSS"
        and item["reason_code"] == "ENGINE_MISMATCH"
        for item in workspace["mandatory_blockers"]
    )


def test_legacy_checker_report_cannot_route_ineligible_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = ("pol", "engine_no", 2, "/documents/1/fields/engine_no")
    entries = list(ControlledScenarioService._C_DEMO_PROVENANCE_ENTRIES)
    entries.remove(target)
    monkeypatch.setattr(
        ControlledScenarioService,
        "_C_DEMO_PROVENANCE_ENTRIES",
        tuple(entries),
    )
    legacy_runner = RuleEngine(
        load_rules(ROOT / "configs" / "rules_auto_lease.yaml")
    ).run
    service = make_service(checker_runner=legacy_runner)
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-legacy-runner-ineligible-provenance",
    )

    result = service.process_next_job()
    service.refresh_projection()
    workspace = service.workspace_view(
        admission.application_id or "", role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
    )
    finding = next(
        item
        for item in workspace["mandatory_blockers"]
        if item["rule_id"] == "R_ENGINE_CROSS"
    )

    assert result.status == "complete"
    assert finding["verdict"] == "uncertain"
    assert finding["reason_code"] == "PROVENANCE_INELIGIBLE"
    assert next(
        link for link in finding["evidence_links"] if link["document_id"] == "pol"
    )["evidence_eligible"] is False


def test_checker_marks_provenance_free_observations_ineligible() -> None:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    release = TargetRelease.compile(
        load_rules(rules_path), hashlib.sha256(rules_path.read_bytes()).hexdigest()
    )
    checker = TargetChecker(release)
    result = checker.run(
        _complete_run_spec(
            release,
            [
                {
                    "document_id": "reg",
                    "document_role": "机动车登记证书",
                    "fields": {"brand": {"raw": "甲品牌", "confidence": 0.99}},
                },
                {
                    "document_id": "pol",
                    "document_role": "交强险保单",
                    "fields": {"brand": {"raw": "乙品牌", "confidence": 0.99}},
                },
            ],
            application_id="app_missing_provenance",
        )
    )
    finding = next(check for check in result.checks if check.rule_id == "R_BRAND_CROSS")

    assert finding.verdict == "uncertain"
    assert finding.reason_codes == ("PROVENANCE_INELIGIBLE",)
    assert finding.evidence_links
    assert all(link.evidence_eligible is False for link in finding.evidence_links)


def test_worker_pins_snapshot_release_and_routes_mandatory_blocker() -> None:
    service = make_service()
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-run",
    )

    result = service.process_next_job()

    assert result.status == "complete"
    assert result.application_id == admission.application_id
    assert result.run_id
    assert result.lifecycle_phases == (
        "Intake",
        "Assembly",
        "Evidence Ready",
        "Checking",
        "Routing Determination",
        "Manual Review",
    )
    assert result.lifecycle_revision == 6
    before_projection = service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject)
    assert before_projection == {"items": [], "projection_watermark": 0}
    with pytest.raises(QueryNotFound):
        service.workspace_view(
            admission.application_id, role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
        )

    projected = service.refresh_projection()
    queue = service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject)

    assert projected == {"updated": 1, "projection_watermark": 1}
    assert queue["projection_watermark"] == 1
    assert len(queue["items"]) == 1
    item = queue["items"][0]
    assert item["phase"] == "Manual Review"
    assert item["route"] == "manual_review"
    assert item["evidence_ready"] is True
    assert item["mandatory_blockers"]
    assert item["mandatory_blockers"][0]["rule_id"] == "R_ENGINE_CROSS"

    workspace = service.workspace_view(
        admission.application_id, role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
    )
    assert workspace["phase"] == "Manual Review"
    assert workspace["selected_finding"]["run_id"] == result.run_id
    assert workspace["selected_finding"]["evidence_links"]
    assert all(link["raw_masked"] == "[REDACTED]" for link in workspace["selected_finding"]["evidence_links"] if link["value_state"] == "present")
    assert workspace["actions"] == ["read_evidence"]
    assert service.fact_counts()["runs"] == 1


@pytest.mark.parametrize("rules_path_change", ("modified", "removed"))
def test_worker_executes_frozen_release_after_rules_path_changes(
    tmp_path, rules_path_change: str
) -> None:
    rules_source = ROOT / "configs" / "rules_auto_lease.yaml"
    frozen_rules = tmp_path / "rules.yaml"
    original_rules = rules_source.read_bytes()
    frozen_rules.write_bytes(original_rules)
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=frozen_rules,
        state_path=tmp_path / "frozen-release.sqlite3",
    )
    if rules_path_change == "modified":
        frozen_rules.write_bytes(
            original_rules.replace(b"id: R_ENGINE_CROSS", b"id: R_ENGINE_MUTATED")
        )
    else:
        frozen_rules.unlink()

    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-frozen-release",
    )
    result = service.process_next_job()

    assert admission.disposition is AdmissionDisposition.ACCEPTED
    assert result.status == "complete"
    assert result.release_id == "auto_lease@1.9.0"
    assert result.release_digest == (
        "21d6fe9e4bbd3c3cd8625e774cdf8aaafe08852aff85cfc65730da6548ab8aef"
    )
    assert result.checker_build == "s01-target-checker/6"
    assert result.fence == 1
    assert service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject) == {
        "items": [],
        "projection_watermark": 0,
    }
    assert service.refresh_projection() == {
        "updated": 1,
        "projection_watermark": 1,
    }
    assert service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject)["items"][0][
        "mandatory_blockers"
    ][0]["rule_id"] == "R_ENGINE_CROSS"


def test_startup_binds_release_identity_and_policy_to_one_rules_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules_source = ROOT / "configs" / "rules_auto_lease.yaml"
    deployed_rules = tmp_path / "rules.yaml"
    original_rules = rules_source.read_bytes()
    deployed_rules.write_bytes(original_rules)
    original_read_bytes = Path.read_bytes
    replaced = False

    def read_then_replace(path: Path) -> bytes:
        nonlocal replaced
        rules_bytes = original_read_bytes(path)
        if path.resolve() == deployed_rules.resolve() and not replaced:
            replaced = True
            deployed_rules.write_bytes(
                original_rules.replace(
                    b"package: auto_lease", b"package: drifted_auto_lease"
                )
            )
        return rules_bytes

    monkeypatch.setattr(Path, "read_bytes", read_then_replace)
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=deployed_rules,
        state_path=tmp_path / "coherent-release.sqlite3",
    )
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-coherent-release",
    )
    result = service.process_next_job()

    assert replaced is True
    assert admission.disposition is AdmissionDisposition.ACCEPTED
    assert result.status == "complete"
    assert result.release_id == "auto_lease@1.9.0"
    assert result.release_digest == (
        "21d6fe9e4bbd3c3cd8625e774cdf8aaafe08852aff85cfc65730da6548ab8aef"
    )


def test_restart_fails_closed_until_the_admitted_release_is_available(
    tmp_path: Path,
) -> None:
    rules_source = ROOT / "configs" / "rules_auto_lease.yaml"
    deployed_rules = tmp_path / "rules.yaml"
    original_rules = rules_source.read_bytes()
    deployed_rules.write_bytes(original_rules)
    state_path = tmp_path / "release-binding.sqlite3"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=deployed_rules,
        state_path=state_path,
    )
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-release-binding-restart",
    )

    deployed_rules.write_bytes(
        original_rules.replace(
            b"low_confidence_threshold: 0.6",
            b"low_confidence_threshold: 1.0",
        )
    )
    drifted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=deployed_rules,
        state_path=state_path,
    )
    before = drifted.fact_counts()

    stopped = drifted.process_next_job()

    assert admission.disposition is AdmissionDisposition.ACCEPTED
    assert stopped.status == "stopped"
    assert stopped.reason_code == "PINNED_RELEASE_UNAVAILABLE"
    assert drifted.fact_counts() == {
        **before,
        "audit_events": before["audit_events"] + 1,
    }
    assert drifted._store.audit_events[-1]["action"] == "controlled_cohort_stop"
    assert drifted._store.audit_events[-1]["failure_reason_code"] == (
        "PINNED_RELEASE_UNAVAILABLE"
    )
    assert drifted.cohort_status() == {
        "track": "C-DEMO",
        "admission": "stopped",
        "reason_code": "S01_RUNTIME_UNHEALTHY",
        "failure_reason_code": "PINNED_RELEASE_UNAVAILABLE",
    }

    deployed_rules.write_bytes(original_rules)
    restored = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=deployed_rules,
        state_path=state_path,
    )
    recovery = restored.recover_runtime(
        principal=TEST_OPERATOR,
        expected_failure_reason_code="PINNED_RELEASE_UNAVAILABLE"
    )
    completed = restored.process_next_job()

    assert recovery["recovery"] == "scheduled"
    assert completed.status == "complete"
    assert completed.release_digest == (
        "21d6fe9e4bbd3c3cd8625e774cdf8aaafe08852aff85cfc65730da6548ab8aef"
    )


def test_mutable_application_manifest_cannot_rebind_the_admitted_release(
    tmp_path: Path,
) -> None:
    rules_source = ROOT / "configs" / "rules_auto_lease.yaml"
    deployed_rules = tmp_path / "rules.yaml"
    original_rules = rules_source.read_bytes()
    deployed_rules.write_bytes(original_rules)
    state_path = tmp_path / "release-owner.sqlite3"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=deployed_rules,
        state_path=state_path,
    )
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-release-owner",
    )
    assert admission.application_id is not None

    deployed_rules.write_bytes(
        original_rules.replace(
            b"low_confidence_threshold: 0.6",
            b"low_confidence_threshold: 1.0",
        )
    )
    drifted_release = TargetRelease.compile(
        load_rules(deployed_rules),
        hashlib.sha256(deployed_rules.read_bytes()).hexdigest(),
    ).public_manifest()
    with sqlite3.connect(state_path) as connection:
        encoded = connection.execute(
            "SELECT payload FROM applications WHERE item_id = ?",
            (admission.application_id,),
        ).fetchone()[0]
        application = json.loads(encoded)
        application["artifact_manifest"] = {
            "release_id": drifted_release["release_id"],
            "release_digest": drifted_release["digest"],
            "checker_build": drifted_release["checker_build"],
        }
        connection.execute(
            "UPDATE applications SET payload = ? WHERE item_id = ?",
            (
                json.dumps(
                    application,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                admission.application_id,
            ),
        )
        connection.commit()

    drifted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=deployed_rules,
        state_path=state_path,
    )
    before = drifted.fact_counts()

    stopped = drifted.process_next_job()

    assert stopped.status == "stopped"
    assert stopped.reason_code == "PINNED_RELEASE_UNAVAILABLE"
    assert drifted.fact_counts() == {
        **before,
        "audit_events": before["audit_events"] + 1,
    }
    assert drifted._store.audit_events[-1]["action"] == "controlled_cohort_stop"
    assert drifted._store.audit_events[-1]["failure_reason_code"] == (
        "PINNED_RELEASE_UNAVAILABLE"
    )


def test_adapter_public_evidence_preserves_source_and_excludes_evaluation_labels(
    tmp_path,
) -> None:
    source = ROOT / "fixtures" / "applications" / "app_r53_bad_engine.json"
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    copied_source = fixture_root / source.name
    original_bytes = source.read_bytes()
    copied_source.write_bytes(original_bytes)
    service = make_service(fixture_root)

    admitted = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id=source.name,
        idempotency_key="s01-m-evidence",
    )
    replay = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id=source.name,
        idempotency_key="s01-m-evidence",
    )
    driver = worker_test_driver(service)
    stale = driver.process_next_job(now=0, cas_fault="release_digest")
    recovered = driver.process_next_job(now=31)

    assert admitted.envelope_version == "c-demo-envelope/1"
    assert admitted.schema_version == "1"
    assert admitted.semantic_version == "1"
    assert admitted.adapter_id == "legacy-fixture-c-demo"
    assert admitted.adapter_version == "1"
    assert admitted.source_sha256 == (
        "8f3bf94619690887fbbb3a5c4fa3bfdb815f178874e0b0dda2469b69454b2a58"
    )
    assert admitted.envelope_fingerprint == (
        "890c61b3d211c13b9eb839a67395b67f9818cf7e7432a60ccb36cbbd533b64f4"
    )
    assert replay.replayed is True
    assert stale.status == "stale"
    assert recovered.status == "complete"
    assert service.fact_counts() == {
        "applications": 1,
        "receipts": 1,
        "lifecycle_events": 9,
        "evidence_events": 3,
        "audit_events": 2,
        "jobs": 1,
        "attempts": 2,
        "runs": 2,
        "findings": 13,
        "outbox": 2,
    }
    assert [event["action"] for event in service._store.audit_events] == [
        "controlled_admission",
        "controlled_run_result",
    ]
    assert copied_source.read_bytes() == original_bytes
    assert service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject) == {
        "items": [],
        "projection_watermark": 0,
    }
    assert service.refresh_projection() == {
        "updated": 1,
        "projection_watermark": 1,
    }

    public_payload = json.dumps(
        {
            "admission": admitted.__dict__,
            "stale": stale.__dict__,
            "recovered": recovered.__dict__,
            "queue": service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject),
            "workspace": service.workspace_view(
                admitted.application_id or "", role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
            ),
        },
        default=str,
        ensure_ascii=False,
    )
    assert "label" not in public_payload
    assert "expected_verdicts" not in public_payload


def test_complete_result_exposes_legacy_semantic_differential() -> None:
    service = make_service()
    service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-m-differential",
    )

    result = service.process_next_job()

    assert result.semantic_differential == {
        "oracle": "legacy-rule-engine",
        "scope": "one-c-demo-fixture",
        "checks_compared": 13,
        "mismatches": [],
        "status": "match",
    }


@pytest.mark.parametrize(
    "command_name", ("submit_demo", "stop_new_cohort", "recover_runtime")
)
def test_command_without_principal_fails_closed_without_changing_authority(
    command_name: str,
) -> None:
    service = make_service()
    if command_name == "recover_runtime":
        service.stop_new_cohort(
            reason_code="S01_RUNTIME_UNHEALTHY",
            failure_reason_code="S01_BACKGROUND_RUNTIME_EXCEPTION",
            principal=S01CommandPrincipal(
                subject="s01-test-runtime",
                role="operator",
                scope="C-DEMO",
                source_id="s01-target-worker",
            ),
        )
    counts_before = service.fact_counts()
    cohort_before = service.cohort_status()

    if command_name == "submit_demo":
        result = service.submit_demo(
            scenario_id="app_r53_bad_engine.json",
            idempotency_key="s01-missing-principal",
        )
        rejected = result.disposition is AdmissionDisposition.REJECTED
    elif command_name == "stop_new_cohort":
        result = service.stop_new_cohort()
        rejected = result.get("stop") == "rejected"
    else:
        result = service.recover_runtime(
            expected_failure_reason_code="S01_BACKGROUND_RUNTIME_EXCEPTION"
        )
        rejected = result.get("recovery") == "rejected"

    assert rejected is True
    assert service.fact_counts() == counts_before
    assert service.cohort_status() == cohort_before


def test_stop_new_cohort_rejects_new_admission_without_business_revision() -> None:
    service = make_service()
    accepted = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-r-existing",
    )
    counts_before_stop = service.fact_counts()

    stopped = service.stop_new_cohort(principal=TEST_OPERATOR)
    rejected = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-r-new",
    )

    assert accepted.disposition is AdmissionDisposition.ACCEPTED
    assert stopped == {
        "track": "C-DEMO",
        "admission": "stopped",
        "reason_code": "S01_NEW_COHORT_STOPPED",
    }
    assert rejected.disposition is AdmissionDisposition.REJECTED
    assert rejected.reason_code == "S01_NEW_COHORT_STOPPED"
    assert rejected.lifecycle_revision == 0
    assert rejected.evidence_revision == 0
    assert service.fact_counts() == {
        **counts_before_stop,
        "receipts": counts_before_stop["receipts"] + 1,
        "audit_events": counts_before_stop["audit_events"] + 2,
    }
    assert [event["action"] for event in service._store.audit_events[-2:]] == [
        "controlled_cohort_stop",
        "controlled_admission",
    ]
    assert service._store.audit_events[-1]["result"] == "rejected"


def test_stop_new_cohort_changes_no_authority_when_required_audit_is_unavailable(
    tmp_path: Path,
) -> None:
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        audit_available=False,
        state_path=tmp_path / "stop-audit-unavailable.sqlite3",
    )
    before = service.fact_counts()

    rejected = service.stop_new_cohort(principal=TEST_OPERATOR)

    assert rejected == {
        "track": "C-DEMO",
        "stop": "rejected",
        "reason_code": "AUDIT_UNAVAILABLE",
    }
    assert service.cohort_status() == {"track": "C-DEMO", "admission": "open"}
    assert service.fact_counts() == before


def test_stop_new_cohort_atomically_records_operator_and_result(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "audited-cohort-stop.sqlite3"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    operator = S01CommandPrincipal(
        subject="c-demo-operator-a",
        role="operator",
        scope="C-DEMO",
        source_id="c-demo-operator-console",
    )

    stopped = service.stop_new_cohort(principal=operator)

    assert stopped == {
        "track": "C-DEMO",
        "admission": "stopped",
        "reason_code": "S01_NEW_COHORT_STOPPED",
    }
    assert service.fact_counts()["audit_events"] == 1
    with sqlite3.connect(state_path) as connection:
        records = [
            json.loads(row[0])
            for row in connection.execute("SELECT payload FROM audit_events")
        ]
    stop_events = [
        record for record in records if record.get("action") == "controlled_cohort_stop"
    ]
    assert len(stop_events) == 1
    stop_event = stop_events[0]
    assert stop_event["event_id"].startswith("audit_")
    assert type(stop_event["event_time"]) is int
    assert stop_event["event_sequence"] == 1
    assert stop_event["event_time_key"] == (
        f"{stop_event['event_time']:020d}:0000000001"
    )
    assert {
        key: value
        for key, value in stop_event.items()
        if key
        not in {"event_id", "event_time", "event_sequence", "event_time_key"}
    } == {
        "action": "controlled_cohort_stop",
        "subject": "c-demo-operator-a",
        "role": "operator",
        "scope": "C-DEMO",
        "source_id": "c-demo-operator-console",
        "result": "stopped",
        "reason_code": "S01_NEW_COHORT_STOPPED",
        "admission_after_stop": stopped,
        "cohort_stop_authority": stopped,
    }
    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    assert restarted.cohort_status() == stopped
    assert restarted.fact_counts()["audit_events"] == 1


def test_authorized_audit_timeline_rebuilds_required_event_order(
    tmp_path: Path,
) -> None:
    current_time = [100]
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "audit-timeline.sqlite3",
        clock=lambda: current_time[0],
    )
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-audit-timeline",
    )
    current_time[0] = 200
    completed = service.process_next_job()
    current_time[0] = 300
    stopped = service.stop_new_cohort(
        principal=TEST_OPERATOR,
        reason_code="S01_RUNTIME_UNHEALTHY",
        failure_reason_code="S01_BACKGROUND_RUNTIME_EXCEPTION",
    )
    current_time[0] = 400
    recovery = service.recover_runtime(
        principal=TEST_OPERATOR,
        expected_failure_reason_code="S01_BACKGROUND_RUNTIME_EXCEPTION"
    )

    assert admission.disposition is AdmissionDisposition.ACCEPTED
    assert completed.status == "complete"
    assert stopped["reason_code"] == "S01_RUNTIME_UNHEALTHY"
    assert recovery["recovery"] == "scheduled"
    assert [event["event_time"] for event in service._store.audit_events] == [
        100,
        200,
        300,
        400,
    ]
    keys = [event["event_time_key"] for event in service._store.audit_events]
    assert keys == sorted(keys)

    auditor = S01CommandPrincipal(
        subject="registered-auditor",
        role="auditor",
        scope="C-DEMO",
        source_id="c-demo-audit-console",
    )
    timeline = service.audit_timeline(
        principal=auditor,
        application_id=admission.application_id,
    )
    assert [event["action"] for event in timeline["events"]] == [
        "controlled_admission",
        "controlled_run_result",
        "controlled_cohort_stop",
        "runtime_recovery",
    ]
    assert timeline["integrity"] == "verified"
    with pytest.raises(QueryNotFound):
        service.audit_timeline(
            principal=S01CommandPrincipal(
                subject="assigned-reviewer",
                role="reviewer",
                scope="C-DEMO",
                source_id="c-demo-review-console",
            ),
            application_id=admission.application_id,
        )


def test_cohort_stop_rebuilds_from_immutable_authority_after_meta_clear(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "cohort-stop-meta-clear.sqlite3"
    owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    stopped = owner.stop_new_cohort(principal=TEST_OPERATOR)
    with sqlite3.connect(state_path) as connection:
        connection.execute("UPDATE s01_meta SET cohort_stop = NULL WHERE id = 1")

    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    rejected = restarted.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-after-meta-clear",
    )

    assert restarted.cohort_status() == stopped
    assert rejected.disposition is AdmissionDisposition.REJECTED
    assert rejected.reason_code == "S01_NEW_COHORT_STOPPED"


def test_stop_new_cohort_rebases_over_an_unrelated_concurrent_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "stop-contention.sqlite3"
    owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    concurrent_owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    original_persist = owner._store.persist
    contended = False

    def persist_after_concurrent_commit() -> None:
        nonlocal contended
        if not contended:
            contended = True
            concurrent_owner.issue_session(
                now=100,
                ttl_seconds=10,
                subject="concurrent-test-user",
                roles=("integrator", "reviewer"),
            )
        original_persist()

    monkeypatch.setattr(owner._store, "persist", persist_after_concurrent_commit)

    stopped = owner.stop_new_cohort(principal=TEST_OPERATOR)

    assert contended is True
    assert stopped == {
        "track": "C-DEMO",
        "admission": "stopped",
        "reason_code": "S01_NEW_COHORT_STOPPED",
    }
    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    assert restarted.cohort_status() == stopped


def test_stop_new_cohort_retries_repeated_contention_without_local_only_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "stop-repeated-contention.sqlite3"
    owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    concurrent_owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    original_persist = owner._store.persist
    persist_calls = 0

    def persist_with_repeated_contention() -> None:
        nonlocal persist_calls
        persist_calls += 1
        if persist_calls <= 3:
            concurrent_owner.issue_session(
                now=100 + persist_calls,
                ttl_seconds=10,
                subject="concurrent-test-user",
                roles=("integrator", "reviewer"),
            )
        original_persist()

    monkeypatch.setattr(owner._store, "persist", persist_with_repeated_contention)

    stopped = owner.stop_new_cohort(principal=TEST_OPERATOR)

    assert persist_calls >= 4
    assert stopped == {
        "track": "C-DEMO",
        "admission": "stopped",
        "reason_code": "S01_NEW_COHORT_STOPPED",
    }
    assert owner.cohort_status() == stopped
    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    assert restarted.cohort_status() == stopped


def test_stopped_cohort_replays_and_recovers_existing_job_without_legacy_write(
    tmp_path,
) -> None:
    source = ROOT / "fixtures" / "applications" / "app_r53_bad_engine.json"
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    copied_source = fixture_root / source.name
    copied_source.write_bytes(source.read_bytes())
    service = make_service(fixture_root)
    accepted = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id=source.name,
        idempotency_key="s01-r-forward",
    )
    stopped = service.stop_new_cohort(principal=TEST_OPERATOR)
    changed_bytes = b"source remains an adapter input; no backfill\n"
    copied_source.write_bytes(changed_bytes)

    replay = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id=source.name,
        idempotency_key="s01-r-forward",
    )
    driver = worker_test_driver(service)
    stale = driver.process_next_job(now=0, cas_fault="fence")
    recovered = driver.process_next_job(now=31)

    assert service.cohort_status() == stopped
    assert accepted.application_id == replay.application_id
    assert replay.replayed is True
    assert stale.status == "stale"
    assert recovered.status == "complete"
    assert copied_source.read_bytes() == changed_bytes


def test_partial_crashed_and_stale_attempts_never_become_current() -> None:
    service = make_service()
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-faults",
    )

    driver = worker_test_driver(service)
    crashed = driver.process_next_job(worker_id="worker-crash", now=0, crash=True)
    assert crashed.status == "crashed"
    partial = driver.process_next_job(worker_id="worker-partial", now=31, partial=True)
    assert partial.status == "partial"
    stale = driver.process_next_job(worker_id="worker-stale", now=62, stale=True)
    assert stale.status == "stale"
    assert stale.cas_mismatches == ("lifecycle_revision",)
    assert service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject)["items"] == []
    assert service.fact_counts()["runs"] == 1
    assert service.fact_counts()["findings"] == 0
    complete = driver.process_next_job(worker_id="worker-recovery", now=93)
    assert complete.status == "complete"
    assert complete.application_id == admission.application_id


@pytest.mark.parametrize(
    "cas_fault",
    (
        "cycle",
        "lifecycle_revision",
        "evidence_revision",
        "release_id",
        "release_digest",
        "checker_build",
        "fence",
    ),
)
def test_complete_result_requires_matching_frozen_context_and_recovers(
    cas_fault: str,
) -> None:
    service = make_service()
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=f"s01-cas-{cas_fault}",
    )

    driver = worker_test_driver(service)
    stale = driver.process_next_job(
        worker_id="worker-stale",
        now=0,
        cas_fault=cas_fault,
    )

    assert stale.status == "stale"
    assert stale.reason_code == "STALE_COMPARE_AND_SET"
    assert stale.cas_mismatches == (cas_fault,)
    assert service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject) == {
        "items": [],
        "projection_watermark": 0,
    }
    assert service.refresh_projection() == {
        "updated": 0,
        "projection_watermark": 0,
    }
    assert service.fact_counts()["runs"] == 1
    assert service.fact_counts()["findings"] == 0
    assert service.fact_counts()["outbox"] == 1

    recovered = driver.process_next_job(worker_id="worker-recovery", now=31)
    assert recovered.status == "complete"
    assert recovered.application_id == admission.application_id
    assert recovered.run_id == stale.run_id
    assert recovered.lifecycle_phases == (
        "Intake",
        "Assembly",
        "Evidence Ready",
        "Checking",
        "Assembly",
        "Evidence Ready",
        "Checking",
        "Routing Determination",
        "Manual Review",
    )
    assert service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject) == {
        "items": [],
        "projection_watermark": 0,
    }
    assert service.fact_counts()["outbox"] == 2
    assert service.refresh_projection() == {
        "updated": 1,
        "projection_watermark": 1,
    }
    assert service.refresh_projection() == {
        "updated": 0,
        "projection_watermark": 1,
    }
    assert service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject)["items"][0][
        "application_id"
    ] == admission.application_id


def test_unauthorized_or_cross_scope_workspace_hides_existence() -> None:
    service = make_service()
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-auth",
    )
    service.process_next_job()

    for kwargs in ({"role": "integrator"}, {"scope": "other-tenant"}):
        try:
            service.workspace_view(admission.application_id, **kwargs)
        except QueryNotFound:
            pass
        else:
            raise AssertionError("out-of-scope query must hide existence")


def test_manual_review_requires_lifecycle_owned_assigned_work_item() -> None:
    service = make_service()
    reviewer = S01CommandPrincipal(
        subject="assigned-reviewer",
        role="integrator",
        scope="C-DEMO",
        source_id="c-demo-review-console",
    )
    admission = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-assigned-review-work",
        principal=reviewer,
    )
    completed = worker_test_driver(service).process_next_job(now=100)
    service.refresh_projection()

    assert completed.status == "complete"
    assert len(service._store.work_items) == 1
    work_item = service._store.work_items[0]
    assert work_item["owner"] == "Lifecycle"
    assert work_item["application_id"] == admission.application_id
    assert work_item["run_id"] == completed.run_id
    assert work_item["assigned_subject"] == "assigned-reviewer"
    assert work_item["claim_subject"] is None
    assert work_item["claim_fence"] == 0
    assert work_item["claim_started_at"] == 0
    assert work_item["claim_expires_at"] == 0

    assert service.queue_view(
        role="reviewer",
        scope="C-DEMO",
        subject="other-reviewer",
        now=101,
    ) == {"items": [], "projection_watermark": 0}
    with pytest.raises(QueryNotFound):
        service.workspace_view(
            admission.application_id or "",
            role="reviewer",
            scope="C-DEMO",
            subject="other-reviewer",
            now=101,
        )
    assigned_queue = service.queue_view(
        role="reviewer",
        scope="C-DEMO",
        subject="assigned-reviewer",
        now=101,
    )
    assert assigned_queue["items"][0]["work_item_id"] == work_item["work_item_id"]
    assert assigned_queue["items"][0]["claim_fence"] == 0


def test_missing_scope_queries_default_deny_without_changing_facts() -> None:
    service = make_service()
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-missing-query-scope",
    )
    service.process_next_job()
    counts = service.fact_counts()

    assert service.queue_view() == {"items": [], "projection_watermark": 0}
    assert service.queue_view(role="reviewer") == {
        "items": [],
        "projection_watermark": 0,
    }
    with pytest.raises(QueryNotFound):
        service.workspace_view(admission.application_id or "")
    with pytest.raises(QueryNotFound):
        service.workspace_view(admission.application_id or "", role="reviewer")
    assert service.fact_counts() == counts


def test_unauthorized_queue_never_discloses_authoritative_activity_watermark() -> None:
    service = make_service()
    hidden_before = service.queue_view(role="reviewer", scope="other-scope")

    service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-hidden-watermark",
    )
    hidden_after_admission = service.queue_view(role="reviewer", scope="other-scope")
    worker_test_driver(service).process_next_job(now=0)
    hidden_after_result = service.queue_view(role="reviewer", scope="other-scope")
    service.refresh_projection()
    hidden_after_projection = service.queue_view(role="integrator", scope="C-DEMO")

    assert hidden_before == {"items": [], "projection_watermark": 0}
    assert hidden_after_admission == hidden_before
    assert hidden_after_result == hidden_before
    assert hidden_after_projection == hidden_before


def test_target_facts_and_queued_job_survive_service_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "s01-target.sqlite3"
    first = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )

    admitted = first.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-durable-restart",
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    application_id = admitted.application_id
    receipt_id = admitted.receipt_id

    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )

    replay = restarted.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-durable-restart",
    )
    assert replay.replayed is True
    assert replay.application_id == application_id
    assert replay.receipt_id == receipt_id
    assert restarted.fact_counts()["applications"] == 1
    assert restarted.fact_counts()["jobs"] == 1

    processed = restarted.process_next_job()
    assert processed.status == "complete"
    assert processed.application_id == application_id


def test_restart_rebuilds_missing_admission_job_from_immutable_outbox(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "s01-missing-job.sqlite3"
    first = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    admitted = first.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-missing-job-recovery",
    )
    assert admitted.application_id is not None

    with sqlite3.connect(state_path) as connection:
        connection.execute("DELETE FROM jobs WHERE item_id = ?", (admitted.job_id,))
        connection.commit()

    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )

    assert restarted.fact_counts()["jobs"] == 1
    processed = restarted.process_next_job()

    assert processed.status == "complete"
    assert processed.job_id == admitted.job_id
    assert processed.application_id == admitted.application_id


def test_restart_rebuilds_forged_terminal_job_from_immutable_outbox(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "s01-forged-terminal-job.sqlite3"
    first = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    admitted = first.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-forged-terminal-job-recovery",
    )
    assert admitted.application_id is not None

    with sqlite3.connect(state_path) as connection:
        encoded = connection.execute(
            "SELECT payload FROM jobs WHERE item_id = ?", (admitted.job_id,)
        ).fetchone()[0]
        job = json.loads(encoded)
        job["status"] = "complete"
        connection.execute(
            "UPDATE jobs SET payload = ? WHERE item_id = ?",
            (
                json.dumps(
                    job,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                admitted.job_id,
            ),
        )
        connection.commit()

    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    processed = restarted.process_next_job()

    assert processed.status == "complete"
    assert processed.job_id == admitted.job_id
    assert processed.application_id == admitted.application_id


def test_restart_persists_fail_closed_when_admission_job_authority_is_missing(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "s01-missing-admission-authority.sqlite3"
    first = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    admitted = first.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-missing-admission-authority",
    )
    assert admitted.application_id is not None

    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "DELETE FROM applications WHERE item_id = ?", (admitted.application_id,)
        )
        connection.commit()

    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )

    assert restarted.cohort_status() == {
        "track": "C-DEMO",
        "admission": "stopped",
        "reason_code": "S01_RUNTIME_UNHEALTHY",
        "failure_reason_code": "ADMISSION_JOB_RECOVERY_UNAVAILABLE",
    }
    recovery_stop_event = restarted._store.audit_events[-1]
    assert recovery_stop_event["action"] == "controlled_cohort_stop"
    assert recovery_stop_event["subject"] == "s01-admission-recovery"
    assert recovery_stop_event["role"] == "system"
    assert recovery_stop_event["result"] == "stopped"
    assert recovery_stop_event["failure_reason_code"] == (
        "ADMISSION_JOB_RECOVERY_UNAVAILABLE"
    )
    stopped = restarted.process_next_job()
    assert stopped.status == "stopped"
    assert stopped.reason_code == "ADMISSION_JOB_RECOVERY_UNAVAILABLE"
    with sqlite3.connect(state_path) as connection:
        encoded = connection.execute(
            "SELECT cohort_stop FROM s01_meta WHERE id = 1"
        ).fetchone()[0]
    assert json.loads(encoded)["failure_reason_code"] == (
        "ADMISSION_JOB_RECOVERY_UNAVAILABLE"
    )


def test_service_requires_an_explicit_deployment_owned_state_path() -> None:
    with pytest.raises(ValueError, match="state_path"):
        ControlledScenarioService(
            fixture_root=ROOT / "fixtures" / "applications",
            rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        )


def test_expired_session_is_removed_from_persisted_demo_authority(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "expired-session.sqlite3"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    token, issued = service.issue_session(
        now=100,
        ttl_seconds=10,
        subject="registered-test-user",
        roles=("integrator", "reviewer"),
    )

    assert issued["expires_at"] == 110
    assert service.resolve_session(token, now=110) is None
    with sqlite3.connect(state_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_session_issuance_removes_all_abandoned_expired_sessions(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "abandoned-sessions.sqlite3"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    for _ in range(5):
        service.issue_session(
            now=0,
            ttl_seconds=10,
            subject="registered-test-user",
            roles=("integrator", "reviewer"),
        )

    service.issue_session(
        now=100,
        ttl_seconds=10,
        subject="registered-test-user",
        roles=("integrator", "reviewer"),
    )

    with sqlite3.connect(state_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_restart_removes_abandoned_expired_sessions_without_token_access(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "restart-expired-sessions.sqlite3"
    first = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    for _ in range(5):
        first.issue_session(
            now=0,
            ttl_seconds=10,
            subject="registered-test-user",
            roles=("integrator", "reviewer"),
        )

    ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
        clock=lambda: 100,
    )

    with sqlite3.connect(state_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_public_demo_scope_is_governed_deleted_at_24_hour_boundary(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "public-demo-retention.sqlite3"
    issued_at = 100
    cleanup_due_at = issued_at + 24 * 60 * 60
    owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
        clock=lambda: issued_at,
    )
    _, session = owner.issue_session(
        now=issued_at,
        ttl_seconds=10,
        subject="registered-demo-reviewer",
        roles=("integrator", "reviewer"),
    )
    admission = owner.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-public-demo-retention",
        principal=S01CommandPrincipal(
            subject="registered-demo-reviewer",
            role="integrator",
            scope=session["scope"],
            source_id="c-demo-web-session",
        ),
    )
    assert worker_test_driver(owner).process_next_job(now=issued_at).status == "complete"
    owner.refresh_projection()

    before_boundary = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
        clock=lambda: cleanup_due_at - 1,
    )
    assert before_boundary.fact_counts()["applications"] == 1

    deleted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
        clock=lambda: cleanup_due_at,
    )

    assert deleted.fact_counts() == {
        "applications": 0,
        "receipts": 0,
        "lifecycle_events": 0,
        "evidence_events": 0,
        "audit_events": 0,
        "jobs": 0,
        "attempts": 0,
        "runs": 0,
        "findings": 0,
        "outbox": 0,
    }
    with sqlite3.connect(state_path) as connection:
        for table in (
            "applications",
            "receipts",
            "lifecycle_events",
            "evidence_events",
            "audit_events",
            "jobs",
            "idempotency",
            "attempts",
            "runs",
            "findings",
            "work_items",
            "outbox",
            "projections",
            "sessions",
            "demo_sessions",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        receipt_payload = connection.execute(
            "SELECT payload FROM deletion_receipts"
        ).fetchone()[0]
        catalog_count = connection.execute(
            "SELECT COUNT(*) FROM s01_immutable_catalog"
        ).fetchone()[0]
    deletion_receipt = json.loads(receipt_payload)
    assert deletion_receipt["policy"] == "public-demo-retention/1"
    assert deletion_receipt["cleanup_due_at"] == cleanup_due_at
    assert deletion_receipt["deleted_at"] == cleanup_due_at
    assert deletion_receipt["result"] == "deleted"
    assert catalog_count == 1
    minimized = json.dumps(deletion_receipt, sort_keys=True)
    assert (admission.application_id or "") not in minimized
    assert "registered-demo-reviewer" not in minimized


def test_background_runtime_purges_due_public_demo_without_client_traffic(
    tmp_path: Path,
) -> None:
    from task4_consistency.web.app import S01BackgroundRuntime

    state_path = tmp_path / "background-public-demo-retention.sqlite3"
    issued_at = 100
    cleanup_due_at = issued_at + 24 * 60 * 60
    clock = {"now": issued_at}
    owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
        clock=lambda: clock["now"],
    )
    _, session = owner.issue_session(
        now=issued_at,
        ttl_seconds=10,
        subject="registered-runtime-demo-reviewer",
        roles=("integrator", "reviewer"),
    )
    admission = owner.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-background-public-demo-retention",
        principal=S01CommandPrincipal(
            subject="registered-runtime-demo-reviewer",
            role="integrator",
            scope=session["scope"],
            source_id="c-demo-web-session",
        ),
    )
    assert admission.application_id is not None

    runtime = S01BackgroundRuntime(owner)
    runtime.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if owner.fact_counts()["findings"] > 0:
                break
            time.sleep(0.01)
        else:
            pytest.fail("background runtime did not complete the public demo")

        clock["now"] = cleanup_due_at
        expected_fact_counts = {
            "applications": 0,
            "receipts": 0,
            "lifecycle_events": 0,
            "evidence_events": 0,
            "audit_events": 0,
            "jobs": 0,
            "attempts": 0,
            "runs": 0,
            "findings": 0,
            "outbox": 0,
        }
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if owner.fact_counts() == expected_fact_counts:
                break
            time.sleep(0.01)
        else:
            pytest.fail("background runtime did not purge the due public demo")
    finally:
        runtime.stop()

    with sqlite3.connect(state_path) as connection:
        for table in (
            "applications",
            "receipts",
            "lifecycle_events",
            "evidence_events",
            "audit_events",
            "jobs",
            "idempotency",
            "attempts",
            "runs",
            "findings",
            "work_items",
            "outbox",
            "projections",
            "sessions",
            "demo_sessions",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        receipt_payload = connection.execute(
            "SELECT payload FROM deletion_receipts"
        ).fetchone()[0]
        catalog_count = connection.execute(
            "SELECT COUNT(*) FROM s01_immutable_catalog"
        ).fetchone()[0]
    deletion_receipt = json.loads(receipt_payload)
    assert deletion_receipt["policy"] == "public-demo-retention/1"
    assert deletion_receipt["cleanup_due_at"] == cleanup_due_at
    assert deletion_receipt["deleted_at"] == cleanup_due_at
    assert deletion_receipt["result"] == "deleted"
    assert catalog_count == 1
    minimized = json.dumps(deletion_receipt, sort_keys=True)
    assert admission.application_id not in minimized
    assert "registered-runtime-demo-reviewer" not in minimized


@pytest.mark.parametrize("tamper", ("update", "delete", "watermark"))
def test_published_projection_is_rebuilt_from_authoritative_facts_after_restart(
    tmp_path: Path, tamper: str
) -> None:
    state_path = tmp_path / f"projection-{tamper}.sqlite3"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=f"projection-{tamper}",
    )
    service.process_next_job()
    service.refresh_projection()
    assert admission.application_id is not None

    with sqlite3.connect(state_path) as connection:
        if tamper == "update":
            encoded = connection.execute(
                "SELECT payload FROM projections WHERE item_id = ?",
                (admission.application_id,),
            ).fetchone()[0]
            projection = json.loads(encoded)
            projection.update(
                {
                    "phase": "Verification Completed",
                    "route": "auto_complete",
                    "mandatory_blockers": [],
                }
            )
            connection.execute(
                "UPDATE projections SET payload = ? WHERE item_id = ?",
                (
                    json.dumps(
                        projection,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    admission.application_id,
                ),
            )
        elif tamper == "delete":
            connection.execute(
                "DELETE FROM projections WHERE item_id = ?",
                (admission.application_id,),
            )
        else:
            encoded = connection.execute(
                "SELECT payload FROM projections WHERE item_id = ?",
                (admission.application_id,),
            ).fetchone()[0]
            projection = json.loads(encoded)
            projection["projection_watermark"] = 999
            connection.execute(
                "UPDATE projections SET payload = ? WHERE item_id = ?",
                (
                    json.dumps(
                        projection,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    admission.application_id,
                ),
            )
        connection.commit()

    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    queue = restarted.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject)
    workspace = restarted.workspace_view(
        admission.application_id, role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
    )

    assert len(queue["items"]) == 1
    assert queue["items"][0]["phase"] == "Manual Review"
    assert queue["items"][0]["route"] == "manual_review"
    assert queue["items"][0]["mandatory_blockers"][0]["rule_id"] == (
        "R_ENGINE_CROSS"
    )
    assert workspace["selected_finding"]["rule_id"] == "R_ENGINE_CROSS"
    assert queue["projection_watermark"] == 1
    assert workspace["projection_watermark"] == 1
    assert restarted._store.projection_watermark == 2
    assert queue["items"][0]["projection_watermark"] == 1
    with sqlite3.connect(state_path) as connection:
        repaired = json.loads(
            connection.execute(
                "SELECT payload FROM projections WHERE item_id = ?",
                (admission.application_id,),
            ).fetchone()[0]
        )
    assert repaired["projection_version"] == "s01-review-projection/2"
    assert len(repaired["authority_digest"]) == 64
    assert repaired["projection_watermark"] == 1


def test_orphan_projection_cannot_fabricate_a_reviewer_queue_item(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "projection-orphan.sqlite3"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    admission = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="projection-orphan",
    )
    service.process_next_job()
    service.refresh_projection()
    assert admission.application_id is not None

    orphan_id = "app_orphan_projection_only"
    orphan_projection = {
        "application_id": orphan_id,
        "track": "C-DEMO",
        "phase": "Manual Review",
        "route": "manual_review",
        "evidence_ready": True,
        "lifecycle_revision": 99,
        "evidence_revision": 99,
        "current_run_id": "run_orphan",
        "evidence_snapshot_id": "snapshot_orphan",
        "evidence_snapshot_digest": "0" * 64,
        "mandatory_blockers": [],
        "lifecycle_event_id": "lifecycle_orphan",
        "projection_version": "s01-review-projection/2",
        "authority_digest": "0" * 64,
        "projection_watermark": 99,
    }
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "INSERT INTO projections (item_id, payload) VALUES (?, ?)",
            (
                orphan_id,
                json.dumps(
                    orphan_projection,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        connection.commit()

    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    queue = restarted.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject)

    assert [item["application_id"] for item in queue["items"]] == [
        admission.application_id
    ]
    with sqlite3.connect(state_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM projections WHERE item_id = ?", (orphan_id,)
        ).fetchone()[0] == 0


def test_worker_uses_content_addressed_immutable_evidence_snapshot(
    tmp_path: Path,
) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    source = ROOT / "fixtures" / "applications" / "app_r53_bad_engine.json"
    controlled_source = fixture_root / source.name
    controlled_source.write_bytes(source.read_bytes())
    service = ControlledScenarioService(
        fixture_root=fixture_root,
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "snapshot.sqlite3",
    )
    admitted = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id=source.name,
        idempotency_key="s01-content-addressed-snapshot",
    )
    controlled_source.unlink()

    processed = service.process_next_job()
    assert service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject) == {
        "items": [],
        "projection_watermark": 0,
    }
    with pytest.raises(QueryNotFound):
        service.workspace_view(
            admitted.application_id or "", role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
        )
    assert service.refresh_projection() == {
        "updated": 1,
        "projection_watermark": 1,
    }
    workspace = service.workspace_view(
        admitted.application_id or "", role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
    )

    assert processed.status == "complete"
    assert processed.evidence_snapshot_digest is not None
    assert len(processed.evidence_snapshot_digest) == 64
    assert processed.evidence_snapshot_id == (
        f"snapshot_sha256_{processed.evidence_snapshot_digest}"
    )
    assert workspace["evidence_snapshot_id"] == processed.evidence_snapshot_id
    assert workspace["evidence_snapshot_digest"] == processed.evidence_snapshot_digest


def test_legacy_oracle_disagreement_cannot_control_target_route() -> None:
    legacy = RuleEngine(load_rules(ROOT / "configs" / "rules_auto_lease.yaml"))

    def disagreeing_oracle(application: object) -> object:
        report = legacy.run(application)  # type: ignore[arg-type]
        checks = [
            replace(
                check,
                verdict=Verdict.CONSISTENT,
                reason_codes=["CONSISTENT"],
            )
            if check.rule_id == "R_ENGINE_CROSS"
            else check
            for check in report.checks
        ]
        return replace(report, checks=checks)

    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        legacy_oracle_runner=disagreeing_oracle,
        state_path=Path(tempfile.mkdtemp(prefix="xiaopeng-s01-oracle-"))
        / "target.sqlite3",
    )
    admitted = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-independent-target-checker",
    )

    processed = service.process_next_job()
    assert service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject) == {
        "items": [],
        "projection_watermark": 0,
    }
    assert service.refresh_projection() == {
        "updated": 1,
        "projection_watermark": 1,
    }
    workspace = service.workspace_view(
        admitted.application_id or "", role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject
    )

    assert processed.status == "complete"
    assert workspace["phase"] == "Manual Review"
    assert [
        blocker["rule_id"] for blocker in workspace["mandatory_blockers"]
    ] == ["R_ENGINE_CROSS"]
    assert processed.semantic_differential is not None
    assert processed.semantic_differential["status"] == "mismatch"
    assert [
        mismatch["rule_id"]
        for mismatch in processed.semantic_differential["mismatches"]
    ] == ["R_ENGINE_CROSS"]


def test_idempotency_binding_conflicts_when_fresh_owner_manifest_content_changes(
    tmp_path: Path,
) -> None:
    fixture_a = tmp_path / "fixture-a"
    fixture_b = tmp_path / "fixture-b"
    fixture_a.mkdir()
    fixture_b.mkdir()
    source = ROOT / "fixtures" / "applications" / "app_r53_bad_engine.json"
    original = json.loads(source.read_text(encoding="utf-8"))
    (fixture_a / source.name).write_text(
        json.dumps(original, ensure_ascii=False), encoding="utf-8"
    )
    changed = json.loads(json.dumps(original))
    changed["documents"][0]["fields"]["engine_no"]["raw"] = "MANIFEST-CHANGED"
    (fixture_b / source.name).write_text(
        json.dumps(changed, ensure_ascii=False), encoding="utf-8"
    )
    state_path = tmp_path / "manifest.sqlite3"
    first_owner = ControlledScenarioService(
        fixture_root=fixture_a,
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    accepted = first_owner.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id=source.name,
        idempotency_key="s01-content-bound-idempotency",
    )
    counts = first_owner.fact_counts()

    changed_owner = ControlledScenarioService(
        fixture_root=fixture_b,
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    conflict = changed_owner.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id=source.name,
        idempotency_key="s01-content-bound-idempotency",
    )

    assert accepted.disposition is AdmissionDisposition.ACCEPTED
    assert conflict.disposition is AdmissionDisposition.REJECTED
    assert conflict.reason_code == "IDEMPOTENCY_CONFLICT"
    assert conflict.replayed is False
    assert changed_owner.fact_counts() == counts


def test_public_worker_cycle_uses_server_owned_identity_and_clock() -> None:
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        worker_identity="trusted-worker",
        clock=lambda: 101,
        state_path=Path(tempfile.mkdtemp(prefix="xiaopeng-s01-worker-"))
        / "target.sqlite3",
    )
    service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-server-owned-worker-context",
    )

    with pytest.raises(TypeError):
        service.process_next_job(worker_id="caller-worker")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        service.process_next_job(now=0)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        service.process_next_job(crash=True)  # type: ignore[call-arg]

    processed = service.process_next_job()

    assert processed.status == "complete"
    assert processed.fence == 1


def test_worker_fault_controls_are_available_only_through_test_driver() -> None:
    from task4_consistency.controlled.s01 import ControlledScenarioTestDriver

    service = make_service()
    service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-test-only-worker-faults",
    )
    driver = ControlledScenarioTestDriver(service)

    crashed = driver.process_next_job(
        worker_id="test-crash-worker",
        now=0,
        crash=True,
    )
    before_expiry = driver.process_next_job(
        worker_id="test-recovery-worker",
        now=29,
    )
    recovered = driver.process_next_job(
        worker_id="test-recovery-worker",
        now=30,
    )

    assert crashed.status == "crashed"
    assert before_expiry.status == "idle"
    assert recovered.status == "complete"
    with pytest.raises(TypeError):
        service.process_next_job(stale=True)  # type: ignore[call-arg]


def test_reviewer_query_changes_only_after_independent_projection_consumes_outbox(
) -> None:
    service = make_service()
    admitted = service.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s01-async-projection",
    )

    processed = service.process_next_job()
    before_projection = service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject)
    projected = service.refresh_projection()
    after_projection = service.queue_view(role="reviewer", scope="C-DEMO", subject=TEST_INTEGRATOR.subject)

    assert processed.status == "complete"
    assert processed.projection_pending is True
    assert before_projection == {"items": [], "projection_watermark": 0}
    assert projected == {"updated": 1, "projection_watermark": 1}
    assert after_projection["projection_watermark"] == 1
    assert after_projection["items"][0]["application_id"] == admitted.application_id
    assert after_projection["items"][0]["lifecycle_revision"] == (
        processed.lifecycle_revision
    )
