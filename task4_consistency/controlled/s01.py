"""S01 controlled-scenario admission and processing-cycle authority.

This is deliberately a small in-process target seam.  It owns target facts for
the walking skeleton; legacy JSON remains a read-only adapter input.
"""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from task4_consistency.models import (
    Application,
    CheckResult,
    FieldSnapshot,
    Report,
    Severity,
    Verdict,
)
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules

from .s01_checker import (
    TargetChecker,
    TargetCheckResult as _RunCheckResult,
    TargetEvidenceLink as _RunEvidenceLink,
    TargetRelease,
    TargetRunResult as _RunResult,
)
from .s01_store import SQLiteTargetStore, StaleStoreRevision


class AdmissionDisposition(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class AdmissionResult:
    disposition: AdmissionDisposition
    application_id: str | None = None
    receipt_id: str | None = None
    job_id: str | None = None
    reason_code: str | None = None
    replayed: bool = False
    lifecycle_revision: int = 0
    evidence_revision: int = 0
    audit_recorded: bool = False
    envelope_version: str | None = None
    schema_version: str | None = None
    semantic_version: str | None = None
    envelope_id: str | None = None
    stream_id: str | None = None
    source_revision_id: str | None = None
    batch_id: str | None = None
    envelope_fingerprint: str | None = None
    idempotency_identity: str | None = None
    idempotency_key_digest: str | None = None
    occurred_at: str | None = None
    occurred_at_status: str | None = None
    produced_at: str | None = None
    produced_at_status: str | None = None
    observed_at: str | None = None
    observed_at_status: str | None = None
    received_at: str | None = None
    received_at_status: str | None = None
    adapter_id: str | None = None
    adapter_version: str | None = None
    source_sha256: str | None = None
    artifact_manifest_digest: str | None = None


@dataclass(frozen=True)
class WorkerResult:
    status: str
    application_id: str | None = None
    job_id: str | None = None
    attempt_id: str | None = None
    run_id: str | None = None
    reason_code: str | None = None
    lifecycle_revision: int = 0
    evidence_revision: int = 0
    replayed: bool = False
    projection_pending: bool = False
    lifecycle_phases: tuple[str, ...] = ()
    cas_mismatches: tuple[str, ...] = ()
    release_id: str | None = None
    release_digest: str | None = None
    checker_build: str | None = None
    fence: int = 0
    evidence_snapshot_id: str | None = None
    evidence_snapshot_digest: str | None = None
    semantic_differential: dict[str, Any] | None = None
    retry_after_seconds: int = 0


class QueryNotFound(LookupError):
    """Existence-hiding query result for unauthorized/cross-scope reads."""


class _StoreWriteFailure(RuntimeError):
    """One staged in-memory store write failed before owner publication."""


class _InvalidRunResult(ValueError):
    """Checker output could not become one complete typed target result."""


class _PinnedReleaseUnavailable(RuntimeError):
    """The release fixed at admission cannot be resolved by this worker."""


class _ApplicationStateAuthorityUnavailable(RuntimeError):
    """Mutable application state disagrees with its immutable authorities."""


@dataclass(frozen=True)
class CanonicalEnvelope:
    envelope_version: str
    schema_version: str
    semantic_version: str
    command_type: str
    scenario_id: str
    upstream_application_reference: str
    envelope_id: str
    stream_id: str
    source_revision_id: str
    batch_id: str
    idempotency_identity: str
    idempotency_key_digest: str
    fingerprint: str
    payload: dict[str, Any]
    oracle_outcomes: tuple[tuple[str, str, tuple[str, ...]], ...] = ()


@dataclass(frozen=True)
class S01CommandPrincipal:
    subject: str
    role: str
    scope: str
    source_id: str


@dataclass(frozen=True)
class S01ArtifactManifest:
    scenario_id: str
    source_sha256: str
    source_provenance_manifest_version: str
    source_provenance_manifest_digest: str
    release_id: str
    release_digest: str
    checker_build: str
    adapter_id: str
    adapter_version: str
    envelope_version: str
    schema_version: str
    semantic_version: str
    digest: str


_TargetStore = SQLiteTargetStore


class ControlledScenarioService:
    """Public S01 processing-cycle command surface for fixed ``C-DEMO`` input."""

    ENVELOPE_VERSION = "c-demo-envelope/1"
    SCHEMA_VERSION = "1"
    SEMANTIC_VERSION = "1"
    COMMAND_TYPE = "demo_scenario_submission"
    _ALLOWED_SCENARIOS = frozenset({"app_r53_bad_engine.json"})
    _ALLOWED_PHASE_SUCCESSORS = {
        "Intake": frozenset({"Assembly"}),
        "Assembly": frozenset({"Evidence Ready"}),
        "Evidence Ready": frozenset({"Checking"}),
        "Checking": frozenset({"Routing Determination", "Assembly"}),
        "Routing Determination": frozenset({"Manual Review", "Verification Completed"}),
    }
    _CAS_CONTEXT_FIELDS = (
        "cycle",
        "lifecycle_revision",
        "evidence_revision",
        "release_id",
        "release_digest",
        "checker_build",
        "fence",
    )
    _MAX_COMPLETE_RESULT_ATTEMPTS = 3
    _MAX_FAILURE_ATTEMPTS = 3
    _RUNTIME_STOP_REASON = "S01_RUNTIME_UNHEALTHY"
    _PINNED_RELEASE_FAILURE = "PINNED_RELEASE_UNAVAILABLE"
    _APPLICATION_STATE_FAILURE = "APPLICATION_STATE_AUTHORITY_UNAVAILABLE"
    _ADMISSION_JOB_RECOVERY_FAILURE = "ADMISSION_JOB_RECOVERY_UNAVAILABLE"
    _RESUME_STOP_KEY = "_resume_stop"
    _SESSION_SCOPE_PREFIX = "C-DEMO/session/"
    _REVIEW_CLAIM_TTL_SECONDS = 900
    _DEMO_RETENTION_SECONDS = 24 * 60 * 60
    _C_DEMO_PROVENANCE_SCHEMA = "c-demo-source-provenance/1"
    _C_DEMO_RECEIVED_AT = "2000-01-01T00:00:00Z"
    _C_DEMO_PROVENANCE_SOURCE_SHA256 = (
        "8f3bf94619690887fbbb3a5c4fa3bfdb815f178874e0b0dda2469b69454b2a58"
    )
    _C_DEMO_PROVENANCE_ENTRIES = (
        ("reg", "address", 1, "/documents/0/fields/address"),
        ("reg", "brand", 1, "/documents/0/fields/brand"),
        ("reg", "engine_no", 1, "/documents/0/fields/engine_no"),
        ("reg", "model", 1, "/documents/0/fields/model"),
        ("reg", "owner_name", 1, "/documents/0/fields/owner_name"),
        ("reg", "plate_no", 1, "/documents/0/fields/plate_no"),
        ("reg", "reg_cert_no", 1, "/documents/0/fields/reg_cert_no"),
        ("reg", "reg_date", 1, "/documents/0/fields/reg_date"),
        ("reg", "vin", 1, "/documents/0/fields/vin"),
        ("pol", "brand", 2, "/documents/1/fields/brand"),
        ("pol", "engine_no", 2, "/documents/1/fields/engine_no"),
        ("pol", "insured_name", 2, "/documents/1/fields/insured_name"),
        ("pol", "model", 2, "/documents/1/fields/model"),
        ("pol", "plate_list", 2, "/documents/1/fields/plate_list"),
        ("pol", "plate_no", 2, "/documents/1/fields/plate_no"),
        ("pol", "vin", 2, "/documents/1/fields/vin"),
        ("lease", "brand", 3, "/documents/2/fields/brand"),
        ("lease", "financed_amount", 3, "/documents/2/fields/financed_amount"),
        ("lease", "id_number", 3, "/documents/2/fields/id_number"),
        ("lease", "lessee_name", 3, "/documents/2/fields/lessee_name"),
        ("lease", "model", 3, "/documents/2/fields/model"),
        ("lease", "reg_cert_no", 3, "/documents/2/fields/reg_cert_no"),
        ("lease", "reg_date", 3, "/documents/2/fields/reg_date"),
        ("lease", "vin", 3, "/documents/2/fields/vin"),
        ("inv", "brand", 4, "/documents/3/fields/brand"),
        ("inv", "engine_no", 4, "/documents/3/fields/engine_no"),
        ("inv", "invoice_amount", 4, "/documents/3/fields/invoice_amount"),
        ("inv", "model", 4, "/documents/3/fields/model"),
        ("inv", "vin", 4, "/documents/3/fields/vin"),
        ("id", "address", 5, "/documents/4/fields/address"),
        ("id", "id_number", 5, "/documents/4/fields/id_number"),
        ("id", "owner_name", 5, "/documents/4/fields/owner_name"),
    )

    def __init__(
        self,
        *,
        fixture_root: str | Path,
        rules_path: str | Path,
        audit_writer: Callable[[dict[str, Any]], bool] | None = None,
        audit_available: bool = True,
        storage_available: bool = True,
        fault_injector: Callable[[str], None] | None = None,
        checker_runner: Callable[[Application], Any] | None = None,
        legacy_oracle_runner: Callable[[Application], Any] | None = None,
        application_id_allocator: Callable[[], str] | None = None,
        state_path: str | Path | None = None,
        worker_identity: str = "s01-worker",
        clock: Callable[[], int] | None = None,
    ) -> None:
        if not worker_identity or worker_identity.strip() != worker_identity:
            raise ValueError("worker identity must be a non-empty canonical value")
        self.fixture_root = Path(fixture_root).resolve()
        self.rules_path = Path(rules_path).resolve()
        self.audit_available = audit_available
        self.storage_available = storage_available
        self._audit_writer = audit_writer
        self._fault_injector = fault_injector
        self._checker_runner = checker_runner
        self._legacy_oracle_runner = legacy_oracle_runner
        self._application_id_allocator = (
            application_id_allocator or self._default_application_id
        )
        self._worker_identity = worker_identity
        self._clock = clock or (lambda: int(time.time()))
        if state_path is None:
            raise ValueError("state_path is required for the S01 target authority")
        self._store = _TargetStore(state_path)
        self._hydrate_admission_results()
        self._restore_cohort_stop_authority()
        self._lock = threading.RLock()
        self._local_cohort_stop: dict[str, Any] | None = None
        self._reconcile_admission_jobs()
        self._purge_expired_sessions(now=float(self._clock()))
        self._release = self._load_baseline_release()
        self._target_checker = TargetChecker(self._release["target_release"])
        self._source_provenance_manifest = self._load_source_provenance_manifest()
        self._manifest = self._build_artifact_manifest()

    def submit_demo(
        self,
        *,
        scenario_id: str,
        idempotency_key: str,
        principal: S01CommandPrincipal | None = None,
    ) -> AdmissionResult:
        """Convert one fixed fixture to a canonical envelope and admit it.

        The adapter only reads an allowlisted source.  It cannot choose policy,
        write a legacy report, or mutate the source fixture.
        """
        if principal is None or not self._valid_principal(principal):
            return self._rejected("FORBIDDEN")
        command_principal = principal
        if not self._valid_idempotency_key(idempotency_key):
            return self._rejected("INVALID_IDEMPOTENCY_KEY")
        command_fingerprint = self._command_fingerprint(scenario_id)
        binding_key = self._idempotency_binding_key(
            command_principal, idempotency_key
        )

        with self._lock:
            if self._local_cohort_stop is not None:
                return self._rejected(self._local_cohort_stop["reason_code"])
            self._reload_store()
            previous = self._store.idempotency.get(binding_key)
            if previous is not None:
                previous_fingerprint, previous_result = previous
                if previous_fingerprint == command_fingerprint:
                    return AdmissionResult(**{**previous_result.__dict__, "replayed": True})
                return self._idempotency_conflict(previous_result)
            if self._store.cohort_stop is not None:
                return self._record_rejection(
                    reason_code=self._store.cohort_stop["reason_code"],
                    command_fingerprint=command_fingerprint,
                    binding_key=binding_key,
                    principal=command_principal,
                )
        if scenario_id not in self._ALLOWED_SCENARIOS:
            with self._lock:
                self._reload_store()
                return self._record_rejection(
                    reason_code="SCENARIO_NOT_ALLOWED",
                    command_fingerprint=command_fingerprint,
                    binding_key=binding_key,
                    principal=command_principal,
                )

        try:
            payload, source_sha256 = self._read_fixed_scenario(scenario_id)
            envelope = self._canonicalize(
                payload,
                scenario_id,
                idempotency_key,
                source_sha256=source_sha256,
                principal=command_principal,
                idempotency_identity=binding_key,
            )
        except (OSError, ValueError, json.JSONDecodeError):
            with self._lock:
                self._reload_store()
                return self._record_rejection(
                    reason_code="INVALID_CANONICAL_ENVELOPE",
                    command_fingerprint=command_fingerprint,
                    binding_key=binding_key,
                    principal=command_principal,
                )

        with self._lock:
            if self._local_cohort_stop is not None:
                return self._rejected(self._local_cohort_stop["reason_code"])
            self._reload_store()
            previous = self._store.idempotency.get(binding_key)
            if previous is not None:
                previous_fingerprint, previous_result = previous
                if previous_fingerprint == command_fingerprint:
                    return AdmissionResult(**{**previous_result.__dict__, "replayed": True})
                return self._idempotency_conflict(previous_result)
            if self._store.cohort_stop is not None:
                return self._record_rejection(
                    reason_code=self._store.cohort_stop["reason_code"],
                    command_fingerprint=command_fingerprint,
                    binding_key=binding_key,
                    principal=command_principal,
                )

            if not self.audit_available:
                return self._rejected("AUDIT_UNAVAILABLE")
            if not self.storage_available:
                return self._rejected("STORAGE_UNAVAILABLE")
            result = self._admit(
                envelope,
                principal=command_principal,
                binding_key=binding_key,
                command_fingerprint=command_fingerprint,
            )
            if result.disposition is AdmissionDisposition.REJECTED and result.reason_code in {
                "APPLICATION_ALREADY_ADMITTED",
            }:
                result = self._record_rejection(
                    reason_code=result.reason_code,
                    command_fingerprint=command_fingerprint,
                    binding_key=binding_key,
                    principal=command_principal,
                )
            if result.disposition is AdmissionDisposition.ACCEPTED:
                self._replicate_admission_audit(
                    envelope, result, principal=command_principal
                )
            return result

    def stop_new_cohort(
        self,
        *,
        reason_code: str = "S01_NEW_COHORT_STOPPED",
        failure_reason_code: str | None = None,
        principal: S01CommandPrincipal | None = None,
    ) -> dict[str, str]:
        """Stop only new C-DEMO admissions; accepted target facts remain live."""
        operator = principal
        if (
            operator is None
            or operator.role != "operator"
            or not self.is_c_demo_scope(operator.scope)
            or not operator.subject
            or operator.subject.strip() != operator.subject
            or not operator.source_id
            or operator.source_id.strip() != operator.source_id
        ):
            return {
                "track": "C-DEMO",
                "stop": "rejected",
                "reason_code": "FORBIDDEN",
            }
        if not self.audit_available:
            return {
                "track": "C-DEMO",
                "stop": "rejected",
                "reason_code": "AUDIT_UNAVAILABLE",
            }
        requested_stop = {
            "track": "C-DEMO",
            "admission": "stopped",
            "reason_code": reason_code,
        }
        if failure_reason_code is not None:
            requested_stop["failure_reason_code"] = failure_reason_code
        with self._lock:
            self._local_cohort_stop = copy.deepcopy(requested_stop)
            last_contention: StaleStoreRevision | None = None
            for _ in range(8):
                self._reload_store()
                current_stop = self._store.cohort_stop
                next_stop = copy.deepcopy(current_stop)
                if current_stop is None:
                    next_stop = copy.deepcopy(requested_stop)
                elif reason_code == self._RUNTIME_STOP_REASON:
                    if current_stop.get("reason_code") != self._RUNTIME_STOP_REASON:
                        next_stop = self._runtime_stop_with_resume(
                            requested_stop, current_stop
                        )
                elif current_stop.get("reason_code") == self._RUNTIME_STOP_REASON:
                    next_stop[self._RESUME_STOP_KEY] = copy.deepcopy(requested_stop)

                self._local_cohort_stop = copy.deepcopy(next_stop)
                if next_stop != current_stop:
                    self._store.cohort_stop = copy.deepcopy(next_stop)
                    self._append_cohort_stop_audit(
                        self._store,
                        principal=operator,
                        reason_code=reason_code,
                        failure_reason_code=failure_reason_code,
                        cohort_stop=next_stop,
                    )
                    try:
                        self._store.persist()
                    except StaleStoreRevision as error:
                        last_contention = error
                        continue
                result = self._public_cohort_stop(next_stop)
                self._local_cohort_stop = None
                return result
            if last_contention is not None:
                self._local_cohort_stop = None
                self._reload_store()
                raise last_contention
            raise StaleStoreRevision("could not persist S01 cohort stop")

    def cohort_status(self) -> dict[str, str]:
        """Read the authoritative C-DEMO admission control state."""
        with self._lock:
            if self._local_cohort_stop is not None:
                return self._public_cohort_stop(self._local_cohort_stop)
            self._reload_store()
            if self._store.cohort_stop is None:
                return {"track": "C-DEMO", "admission": "open"}
            return self._public_cohort_stop(self._store.cohort_stop)

    def recover_runtime(
        self,
        *,
        expected_failure_reason_code: str,
        principal: S01CommandPrincipal | None = None,
    ) -> dict[str, str | int]:
        """Requeue terminal diagnostics after an operator verifies the repair."""
        operator = principal
        if (
            not expected_failure_reason_code
            or expected_failure_reason_code.strip() != expected_failure_reason_code
            or operator is None
            or operator.role != "operator"
            or not self.is_c_demo_scope(operator.scope)
            or not operator.subject
            or operator.subject.strip() != operator.subject
            or not operator.source_id
            or operator.source_id.strip() != operator.source_id
        ):
            return {
                "track": "C-DEMO",
                "recovery": "rejected",
                "reason_code": "S01_RUNTIME_RECOVERY_PRECONDITION_FAILED",
                "failure_reason_code": "",
                "requeued_jobs": 0,
            }
        if not self.audit_available:
            return {
                "track": "C-DEMO",
                "recovery": "rejected",
                "reason_code": "AUDIT_UNAVAILABLE",
                "failure_reason_code": expected_failure_reason_code,
                "requeued_jobs": 0,
            }

        with self._lock:
            for _ in range(2):
                self._reload_store()
                cohort_stop = self._local_cohort_stop or self._store.cohort_stop
                actual_failure = (
                    str(cohort_stop.get("failure_reason_code") or "")
                    if cohort_stop is not None
                    else ""
                )
                if (
                    cohort_stop is None
                    or cohort_stop.get("reason_code") != "S01_RUNTIME_UNHEALTHY"
                    or actual_failure != expected_failure_reason_code
                ):
                    return {
                        "track": "C-DEMO",
                        "recovery": "rejected",
                        "reason_code": "S01_RUNTIME_RECOVERY_PRECONDITION_FAILED",
                        "failure_reason_code": actual_failure,
                        "requeued_jobs": 0,
                    }
                repair_evidence = self._verify_runtime_repair(actual_failure)
                if repair_evidence is None:
                    return {
                        "track": "C-DEMO",
                        "recovery": "rejected",
                        "reason_code": "S01_RUNTIME_REPAIR_NOT_VERIFIED",
                        "failure_reason_code": actual_failure,
                        "requeued_jobs": 0,
                    }

                staged = copy.deepcopy(self._store)
                requeued_jobs = 0
                for job in staged.jobs:
                    if (
                        job.get("status") == "diagnostic"
                        and job.get("terminal_reason_code") == actual_failure
                    ):
                        job["status"] = "queued"
                        job["recovery_reason"] = "RUNTIME_RECOVERY_REPLAY"
                        for key in (
                            "terminal_reason_code",
                            "retry_not_before",
                            "worker_id",
                            "lease_until",
                        ):
                            job.pop(key, None)
                        requeued_jobs += 1
                resume_stop = cohort_stop.get(self._RESUME_STOP_KEY)
                staged.cohort_stop = (
                    copy.deepcopy(resume_stop)
                    if isinstance(resume_stop, dict)
                    else None
                )
                admission_after_recovery = (
                    self._public_cohort_stop(staged.cohort_stop)
                    if staged.cohort_stop is not None
                    else {"track": "C-DEMO", "admission": "open"}
                )
                recovery_index = 1 + sum(
                    event.get("action") == "runtime_recovery"
                    for event in staged.audit_events
                )
                staged.audit_events.append(
                    {
                        "event_id": self._stable_id(
                            "audit", f"runtime_recovery:{recovery_index}:{actual_failure}"
                        ),
                        "action": "runtime_recovery",
                        "subject": operator.subject,
                        "role": operator.role,
                        "scope": operator.scope,
                        "source_id": operator.source_id,
                        "result": "scheduled",
                        "failure_reason_code": actual_failure,
                        "requeued_jobs": requeued_jobs,
                        "repair_evidence": copy.deepcopy(repair_evidence),
                        "admission_after_recovery": admission_after_recovery,
                        "cohort_stop_authority": copy.deepcopy(staged.cohort_stop),
                        **self._audit_time_fields(staged),
                    }
                )
                try:
                    staged.persist()
                except StaleStoreRevision:
                    continue
                self._store = staged
                self._local_cohort_stop = None
                return {
                    "track": "C-DEMO",
                    "recovery": "scheduled",
                    "reason_code": "S01_RUNTIME_RECOVERY_SCHEDULED",
                    "failure_reason_code": actual_failure,
                    "requeued_jobs": requeued_jobs,
                }
        return {
            "track": "C-DEMO",
            "recovery": "rejected",
            "reason_code": "S01_RUNTIME_RECOVERY_PRECONDITION_FAILED",
            "failure_reason_code": expected_failure_reason_code,
            "requeued_jobs": 0,
        }

    @classmethod
    def _runtime_stop_with_resume(
        cls,
        runtime_stop: dict[str, str],
        current_stop: dict[str, Any] | None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = copy.deepcopy(runtime_stop)
        if current_stop is None:
            return result
        if current_stop.get("reason_code") == cls._RUNTIME_STOP_REASON:
            resume_stop = current_stop.get(cls._RESUME_STOP_KEY)
            if isinstance(resume_stop, dict):
                result[cls._RESUME_STOP_KEY] = copy.deepcopy(resume_stop)
        else:
            result[cls._RESUME_STOP_KEY] = copy.deepcopy(current_stop)
        return result

    @classmethod
    def _public_cohort_stop(cls, cohort_stop: dict[str, Any]) -> dict[str, str]:
        return {
            key: value
            for key, value in copy.deepcopy(cohort_stop).items()
            if key != cls._RESUME_STOP_KEY and isinstance(value, str)
        }

    def _append_cohort_stop_audit(
        self,
        store: _TargetStore,
        *,
        principal: S01CommandPrincipal,
        reason_code: str,
        failure_reason_code: str | None,
        cohort_stop: dict[str, Any],
    ) -> None:
        stop_index = 1 + sum(
            event.get("action") == "controlled_cohort_stop"
            for event in store.audit_events
        )
        stop_event = {
            "event_id": self._stable_id(
                "audit",
                "controlled_cohort_stop:"
                f"{stop_index}:{principal.subject}:{reason_code}:"
                f"{failure_reason_code or ''}",
            ),
            "action": "controlled_cohort_stop",
            "subject": principal.subject,
            "role": principal.role,
            "scope": principal.scope,
            "source_id": principal.source_id,
            "result": "stopped",
            "reason_code": reason_code,
            "admission_after_stop": self._public_cohort_stop(cohort_stop),
            "cohort_stop_authority": copy.deepcopy(cohort_stop),
            **self._audit_time_fields(store),
        }
        if failure_reason_code is not None:
            stop_event["failure_reason_code"] = failure_reason_code
        store.audit_events.append(stop_event)

    def _audit_time_fields(
        self,
        store: _TargetStore,
        *,
        now: float | None = None,
    ) -> dict[str, int | str]:
        records = [*store.audit_events, *store.deletion_receipts]
        sequence = 1 + max(
            (
                int(record.get("event_sequence", 0))
                for record in records
                if isinstance(record.get("event_sequence"), int)
                and not isinstance(record.get("event_sequence"), bool)
            ),
            default=0,
        )
        observed = int(self._clock() if now is None else now)
        event_time = max(
            observed,
            max(
                (
                    int(record.get("event_time", 0))
                    for record in records
                    if isinstance(record.get("event_time"), int)
                    and not isinstance(record.get("event_time"), bool)
                ),
                default=observed,
            ),
        )
        return {
            "event_time": event_time,
            "event_sequence": sequence,
            "event_time_key": f"{event_time:020d}:{sequence:010d}",
        }

    def _runtime_repair_probe_run_spec(self) -> dict[str, Any]:
        probe_job = next(
            (
                job
                for job in self._store.jobs
                if job.get("status") not in {"complete", "diagnostic"}
            ),
            None,
        )
        if probe_job is not None:
            app = self._store.applications.get(str(probe_job.get("application_id")))
            if app is None:
                raise RuntimeError("runtime repair probe job has no application")
            self._require_admitted_release(app)
            application_id = str(app["application_id"])
            cycle = int(app["cycle"])
            lifecycle_revision = int(app["lifecycle_revision"])
            evidence_revision = int(app["evidence_revision"])
            fence = max(1, int(probe_job.get("fence", 0)) + 1)
            evidence = self._admitted_evidence(app)
            probe_identity = str(probe_job.get("job_id") or application_id)
        else:
            payload, source_sha256 = self._read_fixed_scenario(
                self._manifest.scenario_id
            )
            source = {
                "adapter_id": self._manifest.adapter_id,
                "adapter_version": self._manifest.adapter_version,
                "scenario_id": self._manifest.scenario_id,
                "source_sha256": source_sha256,
                "source_object_ref": f"c-demo-object:sha256:{source_sha256}",
                "upstream_application_reference": str(payload["application_id"]),
                "artifact_manifest_digest": self._manifest.digest,
                "source_provenance_manifest_version": (
                    self._manifest.source_provenance_manifest_version
                ),
                "source_provenance_manifest_digest": (
                    self._manifest.source_provenance_manifest_digest
                ),
            }
            evidence = self._adapt_application(
                payload,
                source=source,
                provenance_manifest=self._source_provenance_manifest,
            )["evidence"]
            application_id = "s01_runtime_repair_probe"
            cycle = lifecycle_revision = evidence_revision = fence = 1
            probe_identity = self._manifest.digest

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
        return {
            "run_id": self._stable_id(
                "probe", f"runtime-repair:{probe_identity}:{self._release['digest']}"
            ),
            "application_id": application_id,
            "cycle": cycle,
            "lifecycle_revision": lifecycle_revision,
            "evidence_snapshot_id": f"snapshot_sha256_{snapshot_digest}",
            "evidence_snapshot_digest": snapshot_digest,
            "evidence_snapshot": snapshot,
            "evidence_revision": evidence_revision,
            "evidence_readiness_policy": "c-demo-readiness/1",
            "baseline_release": copy.deepcopy(
                {
                    key: value
                    for key, value in self._release.items()
                    if key not in {"target_release", "legacy_oracle"}
                }
            ),
            "release_id": self._release["release_id"],
            "release_digest": self._release["digest"],
            "checker_build": self._release["checker_build"],
            "fence": fence,
            "limits": copy.deepcopy(self._release["limits"]),
            "applicable_check_ids": self._release["applicable_check_ids"],
            "applicable_check_count": self._release["applicable_check_count"],
        }

    def _verify_runtime_repair(self, failure_reason_code: str) -> dict[str, Any] | None:
        diagnostic_jobs = [
            job
            for job in self._store.jobs
            if job.get("status") == "diagnostic"
            and job.get("terminal_reason_code") == failure_reason_code
        ]
        if not diagnostic_jobs:
            if failure_reason_code not in {
                "S01_BACKGROUND_RUNTIME_EXCEPTION",
                self._PINNED_RELEASE_FAILURE,
                self._APPLICATION_STATE_FAILURE,
            }:
                return None
            try:
                probe_run_spec = self._runtime_repair_probe_run_spec()
                checker_probe = self._convert_run_result(
                    self._run_checker(probe_run_spec), probe_run_spec
                )
                projection_probe = self._probe_projection_boundary()
            except Exception:
                return None
            if (
                not isinstance(checker_probe, _RunResult)
                or not checker_probe.checks
            ):
                return None
            payload = {
                "kind": "background_runtime_boundary_probe",
                "checker_run_id": probe_run_spec["run_id"],
                "checker_result_count": len(checker_probe.checks),
                "projection_updated": projection_probe["updated"],
                "projection_watermark": projection_probe["projection_watermark"],
                "store_revision": self._store._store_revision,
                "release_digest": self._release["digest"],
                "checker_build": self._release["checker_build"],
            }
            encoded = json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            return {**payload, "probe_digest": hashlib.sha256(encoded).hexdigest()}

        probes = []
        try:
            for job in diagnostic_jobs:
                stopped_fence = int(job.get("fence", 0))
                app = self._store.applications.get(str(job.get("application_id")))
                self._require_admitted_release(app)
                attempts = [
                    attempt
                    for attempt in self._store.attempts
                    if attempt.get("job_id") == job.get("job_id")
                    and int(attempt.get("fence", 0)) == stopped_fence
                    and isinstance(attempt.get("run_spec"), dict)
                ]
                if not attempts:
                    return None
                stopped_run_spec = attempts[-1]["run_spec"]
                probe_run_spec = copy.deepcopy(stopped_run_spec)
                probe_run_spec.update(
                    {
                        "run_id": self._stable_id(
                            "probe",
                            f"{job['job_id']}:{stopped_fence}:{self._release['digest']}",
                        ),
                        "baseline_release": copy.deepcopy(
                            {
                                key: value
                                for key, value in self._release.items()
                                if key not in {"target_release", "legacy_oracle"}
                            }
                        ),
                        "release_id": self._release["release_id"],
                        "release_digest": self._release["digest"],
                        "checker_build": self._release["checker_build"],
                        "fence": stopped_fence + 1,
                        "limits": copy.deepcopy(self._release["limits"]),
                        "applicable_check_ids": self._release[
                            "applicable_check_ids"
                        ],
                        "applicable_check_count": self._release[
                            "applicable_check_count"
                        ],
                    }
                )
                report = self._run_checker(probe_run_spec)
                run_result = self._convert_run_result(report, probe_run_spec)
                probes.append(
                    {
                        "job_id": job["job_id"],
                        "stopped_fence": stopped_fence,
                        "probe_fence": stopped_fence + 1,
                        "release_digest": self._release["digest"],
                        "checker_build": self._release["checker_build"],
                        "check_signature": self._check_signature(run_result.checks),
                    }
                )
            projection_probe = self._probe_projection_boundary()
        except Exception:
            return None
        probe_payload = {
            "jobs": probes,
            "projection_updated": projection_probe["updated"],
            "projection_watermark": projection_probe["projection_watermark"],
        }
        encoded = json.dumps(
            probe_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return {
            "kind": "frozen_checker_probe",
            "verified_targets": len(probes),
            "release_digest": self._release["digest"],
            "checker_build": self._release["checker_build"],
            "projection_updated": projection_probe["updated"],
            "projection_watermark": projection_probe["projection_watermark"],
            "probe_digest": hashlib.sha256(encoded).hexdigest(),
            "jobs": probes,
        }

    def _probe_projection_boundary(self) -> dict[str, int]:
        probe = self.refresh_projection()
        if (
            not isinstance(probe, dict)
            or not isinstance(probe.get("updated"), int)
            or isinstance(probe.get("updated"), bool)
            or not isinstance(probe.get("projection_watermark"), int)
            or isinstance(probe.get("projection_watermark"), bool)
        ):
            raise RuntimeError("projection repair probe returned an invalid result")
        return probe

    def fact_counts(self) -> dict[str, int]:
        with self._lock:
            self._reload_store()
            return {
                "applications": len(self._store.applications),
                "receipts": len(self._store.receipts),
                "lifecycle_events": len(self._store.lifecycle_events),
                "evidence_events": len(self._store.evidence_events),
                "audit_events": len(self._store.audit_events),
                "jobs": len(self._store.jobs),
                "attempts": len(self._store.attempts),
                "runs": len(self._store.runs),
                "findings": len(self._store.findings),
                "outbox": len(self._store.outbox),
            }

    def issue_session(
        self,
        *,
        now: float,
        ttl_seconds: int,
        subject: str,
        roles: tuple[str, ...],
    ) -> tuple[str, dict[str, Any]]:
        if ttl_seconds <= 0:
            raise ValueError("session TTL must be positive")
        if not subject or subject.strip() != subject:
            raise ValueError("session subject must be a non-empty canonical value")
        session_roles = tuple(dict.fromkeys(roles))
        if not session_roles or set(session_roles) - {"integrator", "reviewer"}:
            raise ValueError("session roles must be registered demo roles")
        self._purge_expired_sessions(now=float(now))
        for _ in range(3):
            with self._lock:
                self._reload_store()
                token = secrets.token_urlsafe(32)
                token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
                session_id = secrets.token_hex(16)
                principal = {
                    "demo_session_id": session_id,
                    "subject": subject,
                    "roles": list(session_roles),
                    "scope": f"{self._SESSION_SCOPE_PREFIX}{session_id}",
                    "issued_at": float(now),
                    "expires_at": float(now) + ttl_seconds,
                    "cleanup_due_at": float(now) + self._DEMO_RETENTION_SECONDS,
                    "status": "active",
                }
                staged = copy.deepcopy(self._store)
                self._remove_expired_sessions(staged, now=float(now))
                staged.sessions[token_digest] = principal
                staged.demo_sessions.append(
                    {
                        "demo_session_id": session_id,
                        "scope": principal["scope"],
                        "token_digest": token_digest,
                        "issued_at": principal["issued_at"],
                        "expires_at": principal["expires_at"],
                        "cleanup_due_at": principal["cleanup_due_at"],
                        "policy": "public-demo-retention/1",
                    }
                )
                try:
                    staged.persist()
                except StaleStoreRevision:
                    continue
                self._store = staged
                return token, copy.deepcopy(principal)
        raise RuntimeError("could not persist S01 session")

    @staticmethod
    def _remove_expired_sessions(store: _TargetStore, *, now: float) -> int:
        expired = [
            token_digest
            for token_digest, principal in store.sessions.items()
            if not isinstance(principal, dict)
            or principal.get("status") != "active"
            or isinstance(principal.get("expires_at"), bool)
            or not isinstance(principal.get("expires_at"), (int, float))
            or float(principal["expires_at"]) <= now
        ]
        for token_digest in expired:
            del store.sessions[token_digest]
        return len(expired)

    def _purge_expired_sessions(self, *, now: float) -> int:
        with self._lock:
            removed_total = 0
            for _ in range(16):
                staged = copy.deepcopy(self._store)
                due = sorted(
                    (
                        session
                        for session in staged.demo_sessions
                        if isinstance(session.get("cleanup_due_at"), (int, float))
                        and not isinstance(session.get("cleanup_due_at"), bool)
                        and float(session["cleanup_due_at"]) <= now
                    ),
                    key=lambda session: (
                        float(session["cleanup_due_at"]),
                        str(session.get("demo_session_id") or ""),
                    ),
                )
                if due:
                    plan, receipt = self._demo_deletion_plan(
                        staged, due[0], deleted_at=now
                    )
                    try:
                        staged.governed_delete(plan, receipt)
                    except StaleStoreRevision:
                        self._store.reload()
                        self._hydrate_admission_results()
                        self._restore_cohort_stop_authority()
                        continue
                    self._store = staged
                    self._hydrate_admission_results()
                    self._restore_cohort_stop_authority()
                    removed_total += 1
                    continue
                removed = self._remove_expired_sessions(staged, now=now)
                if removed == 0:
                    return removed_total
                try:
                    staged.persist()
                except StaleStoreRevision:
                    self._store.reload()
                    self._hydrate_admission_results()
                    self._restore_cohort_stop_authority()
                    continue
                self._store = staged
                return removed_total + removed
        raise StaleStoreRevision("could not purge expired S01 session data")

    def _demo_deletion_plan(
        self,
        store: _TargetStore,
        session: dict[str, Any],
        *,
        deleted_at: float,
    ) -> tuple[dict[str, set[str]], dict[str, Any]]:
        scope = session.get("scope")
        session_id = session.get("demo_session_id")
        cleanup_due_at = session.get("cleanup_due_at")
        if (
            not isinstance(scope, str)
            or not scope.startswith(self._SESSION_SCOPE_PREFIX)
            or not isinstance(session_id, str)
            or not session_id
            or isinstance(cleanup_due_at, bool)
            or not isinstance(cleanup_due_at, (int, float))
        ):
            raise RuntimeError("public demo retention authority is invalid")
        scoped_audit = [
            event for event in store.audit_events if event.get("scope") == scope
        ]
        application_ids = {
            str(event["application_id"])
            for event in scoped_audit
            if isinstance(event.get("application_id"), str)
            and event.get("application_id")
        }
        plan: dict[str, set[str]] = {
            "applications": set(store.applications).intersection(application_ids),
            "receipts": {
                str(event["receipt_id"])
                for event in scoped_audit
                if isinstance(event.get("receipt_id"), str)
                and event.get("receipt_id") in store.receipts
            },
            "lifecycle_events": {
                str(event["event_id"])
                for event in store.lifecycle_events
                if event.get("application_id") in application_ids
            },
            "evidence_events": {
                str(event["event_id"])
                for event in store.evidence_events
                if event.get("application_id") in application_ids
            },
            "audit_events": {
                str(event["event_id"])
                for event in scoped_audit
                if event.get("action")
                not in {"controlled_cohort_stop", "runtime_recovery"}
            },
            "jobs": {
                str(job["job_id"])
                for job in store.jobs
                if job.get("application_id") in application_ids
            },
            "idempotency": {
                str(event["idempotency_scope"])
                for event in scoped_audit
                if isinstance(event.get("idempotency_scope"), str)
                and event.get("idempotency_scope") in store.idempotency
            },
            "attempts": {
                str(attempt["attempt_id"])
                for attempt in store.attempts
                if attempt.get("application_id") in application_ids
            },
            "runs": {
                str(run["run_record_id"])
                for run in store.runs
                if run.get("application_id") in application_ids
            },
            "findings": {
                str(finding["finding_id"])
                for finding in store.findings
                if finding.get("application_id") in application_ids
            },
            "work_items": {
                str(item["work_item_id"])
                for item in store.work_items
                if item.get("application_id") in application_ids
            },
            "outbox": {
                str(event["event_id"])
                for event in store.outbox
                if event.get("application_id") in application_ids
            },
            "projections": set(store.projections).intersection(application_ids),
            "sessions": {
                token_digest
                for token_digest, principal in store.sessions.items()
                if principal.get("scope") == scope
            },
            "demo_sessions": {
                str(item["demo_session_id"])
                for item in store.demo_sessions
                if item.get("scope") == scope
            },
        }
        plan_bytes = json.dumps(
            {table: sorted(item_ids) for table, item_ids in sorted(plan.items())},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        receipt = {
            "deletion_receipt_id": self._stable_id(
                "deletion", f"{scope}:{cleanup_due_at}"
            ),
            "action": "governed_demo_deletion",
            "policy": "public-demo-retention/1",
            "scope_digest": hashlib.sha256(scope.encode("utf-8")).hexdigest(),
            "cleanup_due_at": cleanup_due_at,
            "deleted_at": int(deleted_at),
            "result": "deleted",
            "deleted_counts": {
                table: len(item_ids) for table, item_ids in sorted(plan.items())
            },
            "deleted_identity_digest": hashlib.sha256(plan_bytes).hexdigest(),
            "subject": "s01-retention-scheduler",
            "role": "system",
            **self._audit_time_fields(store, now=deleted_at),
        }
        return plan, receipt

    def resolve_session(self, token: str, *, now: float) -> dict[str, Any] | None:
        if not token:
            return None
        self._purge_expired_sessions(now=float(now))
        token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self._lock:
            self._reload_store()
            principal = self._store.sessions.get(token_digest)
            if principal is None or principal.get("status") != "active":
                return None
            if float(principal["expires_at"]) > float(now):
                return copy.deepcopy(principal)
            staged = copy.deepcopy(self._store)
            del staged.sessions[token_digest]
            try:
                staged.persist()
            except StaleStoreRevision:
                self._reload_store()
                return None
            self._store = staged
            return None

    def process_next_job(self) -> WorkerResult:
        """Run one cycle with server-owned worker identity and time."""
        now = float(self._clock())
        self._purge_expired_sessions(now=now)
        return self._process_next_job(
            worker_id=self._worker_identity,
            now=int(now),
        )

    def _process_next_job(
        self,
        *,
        worker_id: str = "s01-worker",
        now: int = 0,
        crash: bool = False,
        partial: bool = False,
        stale: bool = False,
        cas_fault: str | None = None,
        duplicate: bool = False,
    ) -> WorkerResult:
        """Claim and execute one durable job.

        Fault controls are deterministic C-DEMO inputs. ``stale`` is the
        backwards-compatible lifecycle-revision mismatch and still passes
        through the same completion comparator as every other result.
        """
        if not worker_id or worker_id.strip() != worker_id:
            return WorkerResult(status="rejected", reason_code="INVALID_WORKER")
        selected_cas_fault = cas_fault or ("lifecycle_revision" if stale else None)
        if selected_cas_fault not in (None, *self._CAS_CONTEXT_FIELDS):
            return WorkerResult(status="rejected", reason_code="INVALID_CAS_FAULT")
        with self._lock:
            local_stop = self._local_cohort_stop
            if (
                local_stop is not None
                and local_stop.get("reason_code") == "S01_RUNTIME_UNHEALTHY"
            ):
                return WorkerResult(
                    status="stopped",
                    reason_code=local_stop.get("failure_reason_code")
                    or "S01_RUNTIME_UNHEALTHY",
                )
            self._reload_store()
            durable_stop = self._store.cohort_stop
            if (
                durable_stop is not None
                and durable_stop.get("reason_code") == "S01_RUNTIME_UNHEALTHY"
            ):
                return WorkerResult(
                    status="stopped",
                    reason_code=durable_stop.get("failure_reason_code")
                    or "S01_RUNTIME_UNHEALTHY",
                )
            if duplicate:
                return self._record_duplicate_result(worker_id)
            try:
                claimed = self._claim_job(worker_id, now)
            except (
                _PinnedReleaseUnavailable,
                _ApplicationStateAuthorityUnavailable,
            ) as error:
                failure_reason = (
                    self._APPLICATION_STATE_FAILURE
                    if isinstance(error, _ApplicationStateAuthorityUnavailable)
                    else self._PINNED_RELEASE_FAILURE
                )
                self.stop_new_cohort(
                    reason_code=self._RUNTIME_STOP_REASON,
                    failure_reason_code=failure_reason,
                    principal=S01CommandPrincipal(
                        subject=worker_id,
                        role="operator",
                        scope="C-DEMO",
                        source_id="s01-target-worker",
                    ),
                )
                return WorkerResult(
                    status="stopped",
                    reason_code=failure_reason,
                )
            if claimed is None:
                return WorkerResult(status="idle", reason_code="NO_READY_JOB")
            job, attempt, run_spec = claimed
            attempt_id = attempt["attempt_id"]
            if crash:
                return WorkerResult(
                    status="crashed",
                    application_id=job["application_id"],
                    job_id=job["job_id"],
                    attempt_id=attempt_id,
                    reason_code="WORKER_CRASH",
                )

            app = self._store.applications[job["application_id"]]
            if partial:
                job["status"] = "queued"
                self._prepare_retry(app, "PARTIAL_RESULT_RECORDED")
                self._store.persist()
                return WorkerResult(
                    status="partial",
                    application_id=job["application_id"],
                    job_id=job["job_id"],
                    attempt_id=attempt_id,
                    reason_code="PARTIAL_RESULT",
                    lifecycle_revision=app["lifecycle_revision"],
                    evidence_revision=app["evidence_revision"],
                )

            try:
                report = self._run_checker(run_spec)
                run_result = self._convert_run_result(report, run_spec)
                semantic_differential = self._semantic_differential(app, run_result)
            except _InvalidRunResult:
                return self._record_checker_failure(
                    app,
                    job,
                    attempt,
                    run_spec,
                    now=now,
                    reason_code="INVALID_RUN_RESULT",
                )
            except Exception:
                return self._record_checker_failure(
                    app, job, attempt, run_spec, now=now
                )
            completion_context = self._completion_context(run_spec)
            if selected_cas_fault is not None:
                completion_context = self._with_cas_fault(
                    completion_context, selected_cas_fault
                )

            result = self._commit_complete_result(
                app,
                job,
                attempt,
                run_spec,
                run_result,
                completion_context,
                semantic_differential,
                now=now,
            )
            return result

    def _projection_from_authority(
        self, application_id: str, *, projection_watermark: int
    ) -> dict[str, Any]:
        visibility_scope = self._application_visibility_scope(application_id)
        lifecycle = sorted(
            (
                event
                for event in self._store.lifecycle_events
                if event.get("application_id") == application_id
            ),
            key=lambda event: int(event["revision"]),
        )
        if not lifecycle:
            raise RuntimeError("projection lifecycle authority is unavailable")
        revisions = [int(event["revision"]) for event in lifecycle]
        if revisions != list(range(1, revisions[-1] + 1)):
            raise RuntimeError("projection lifecycle authority is not contiguous")
        current_event = lifecycle[-1]
        run_id = current_event.get("run_id")
        matching_runs = [
            run
            for run in self._store.runs
            if run.get("application_id") == application_id
            and run.get("run_id") == run_id
            and run.get("status") == "complete"
        ]
        if len(matching_runs) != 1:
            raise RuntimeError("projection current run authority is unavailable")
        run = matching_runs[0]
        spec = run.get("spec")
        if not isinstance(spec, dict):
            raise RuntimeError("projection RunSpec authority is invalid")
        blockers = [
            self._finding_projection(finding)
            for finding in self._store.findings
            if finding.get("application_id") == application_id
            and finding.get("run_id") == run_id
            and finding.get("mandatory") is True
            and finding.get("verdict") != "consistent"
        ]
        phase = str(current_event["phase"])
        if phase == "Manual Review":
            route = "manual_review"
            work_items = [
                item
                for item in self._store.work_items
                if item.get("application_id") == application_id
                and item.get("run_id") == run_id
            ]
            if len(work_items) != 1:
                raise RuntimeError("projection review work authority is unavailable")
            work_item = work_items[0]
            if (
                work_item.get("owner") != "Lifecycle"
                or work_item.get("status") != "active"
                or work_item.get("visibility_scope") != visibility_scope
                or work_item.get("lifecycle_revision") != int(current_event["revision"])
                or work_item.get("evidence_revision") != int(spec["evidence_revision"])
                or work_item.get("evidence_snapshot_id")
                != spec["evidence_snapshot_id"]
                or work_item.get("finding_ids")
                != [finding["finding_id"] for finding in blockers]
            ):
                raise RuntimeError("projection review work authority is invalid")
        elif phase == "Verification Completed":
            route = "auto_complete"
            work_item = None
        else:
            raise RuntimeError("published projection has no terminal lifecycle route")
        authoritative = {
            "application_id": application_id,
            "track": "C-DEMO",
            "visibility_scope": visibility_scope,
            "phase": phase,
            "route": route,
            "evidence_ready": True,
            "lifecycle_revision": int(current_event["revision"]),
            "evidence_revision": int(spec["evidence_revision"]),
            "current_run_id": run_id,
            "evidence_snapshot_id": spec["evidence_snapshot_id"],
            "evidence_snapshot_digest": spec["evidence_snapshot_digest"],
            "mandatory_blockers": blockers,
            "lifecycle_event_id": current_event["event_id"],
        }
        if work_item is not None:
            authoritative.update(
                {
                    key: copy.deepcopy(work_item[key])
                    for key in (
                        "work_item_id",
                        "assigned_subject",
                        "claim_subject",
                        "claim_fence",
                        "claim_expires_at",
                    )
                }
            )
        authority_bytes = json.dumps(
            authoritative,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            **authoritative,
            "projection_version": "s01-review-projection/2",
            "authority_digest": hashlib.sha256(authority_bytes).hexdigest(),
            "projection_watermark": projection_watermark,
        }

    def _repair_published_projections(self) -> None:
        published = {
            str(event["application_id"]): event
            for event in self._store.outbox
            if event.get("kind") == "review_projection_requested"
            and event.get("status") == "published"
        }
        repaired = False
        orphaned = set(self._store.projections).difference(published)
        for application_id in sorted(orphaned):
            self._store.projection_watermark += 1
            del self._store.projections[application_id]
            repaired = True
        for application_id in sorted(published):
            current = self._store.projections.get(application_id)
            publication = published[application_id]
            source_watermark = publication.get("projection_watermark")
            if (
                not isinstance(source_watermark, int)
                or isinstance(source_watermark, bool)
                or source_watermark < 1
            ):
                raise RuntimeError("projection publication watermark is invalid")
            expected = self._projection_from_authority(
                application_id,
                projection_watermark=source_watermark,
            )
            if current == expected:
                continue
            self._store.projection_watermark += 1
            self._store.projections[application_id] = self._projection_from_authority(
                application_id,
                projection_watermark=source_watermark,
            )
            application = self._store.applications.get(application_id)
            if application is not None:
                application["projection_pending"] = False
                application["projection_visible"] = True
            repaired = True
        if repaired:
            self._store.persist()

    def refresh_projection(self) -> dict[str, int]:
        """Advance the minimized queue projection without changing business facts."""
        with self._lock:
            last_contention: StaleStoreRevision | None = None
            for _ in range(3):
                self._reload_store()
                pending = [
                    event
                    for event in self._store.outbox
                    if event.get("kind") == "review_projection_requested"
                    and event.get("status") == "pending"
                ]
                updated = 0
                for event in pending:
                    application_id = str(event["application_id"])
                    source_watermark = event.get("projection_watermark")
                    if (
                        not isinstance(source_watermark, int)
                        or isinstance(source_watermark, bool)
                        or source_watermark < 1
                    ):
                        raise RuntimeError(
                            "projection publication watermark is invalid"
                        )
                    self._store.projection_watermark += 1
                    self._store.projections[application_id] = (
                        self._projection_from_authority(
                            application_id,
                            projection_watermark=source_watermark,
                        )
                    )
                    application = self._store.applications.get(application_id)
                    if application is not None:
                        application["projection_pending"] = False
                        application["projection_visible"] = True
                    event["status"] = "published"
                    updated += 1
                if updated:
                    try:
                        self._store.persist()
                    except StaleStoreRevision as error:
                        last_contention = error
                        continue
                return {
                    "updated": updated,
                    "projection_watermark": self._store.projection_watermark,
                }
            if last_contention is not None:
                raise last_contention
            raise RuntimeError("projection publication retry exhausted")

    def queue_view(
        self,
        *,
        role: str = "",
        scope: str = "",
        subject: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        """Return a minimized Reviewer queue projection, hiding unauthorized scope."""
        if role != "reviewer" or not self.is_c_demo_scope(scope):
            return {"items": [], "projection_watermark": 0}
        with self._lock:
            self._reload_store()
            self._repair_published_projections()
            query_subject = subject
            query_time = float(self._clock()) if now is None else float(now)
            visible_scopes = {scope}
            if scope.startswith(self._SESSION_SCOPE_PREFIX):
                visible_scopes.add("C-DEMO")
            items: list[dict[str, Any]] = []
            visible_watermark = 0
            for projection in self._store.projections.values():
                if projection.get("visibility_scope") not in visible_scopes:
                    continue
                if projection["phase"] != "Manual Review":
                    continue
                if (
                    projection.get("assigned_subject") != query_subject
                    or projection.get("claim_subject") != query_subject
                    or float(projection.get("claim_expires_at", 0)) <= query_time
                ):
                    continue
                visible_watermark = max(
                    visible_watermark, int(projection["projection_watermark"])
                )
                items.append(
                    {
                        "application_id": projection["application_id"],
                        "work_item_id": projection["work_item_id"],
                        "assigned_subject": projection["assigned_subject"],
                        "claim_fence": projection["claim_fence"],
                        "claim_expires_at": projection["claim_expires_at"],
                        "phase": projection["phase"],
                        "route": projection["route"],
                        "evidence_ready": projection["evidence_ready"],
                        "mandatory_blockers": [
                            {
                                "finding_id": finding["finding_id"],
                                "rule_id": finding["rule_id"],
                                "reason_code": finding["reason_code"],
                                "severity": finding["severity"],
                            }
                            for finding in projection["mandatory_blockers"]
                        ],
                        "lifecycle_revision": projection["lifecycle_revision"],
                        "evidence_revision": projection["evidence_revision"],
                        "projection_watermark": projection["projection_watermark"],
                    }
                )
            return {
                "items": items,
                "projection_watermark": visible_watermark,
            }

    def audit_timeline(
        self,
        *,
        principal: S01CommandPrincipal,
        application_id: str,
    ) -> dict[str, Any]:
        """Return an authorized, minimized timeline from immutable audit facts."""
        if (
            principal.role != "auditor"
            or principal.scope != "C-DEMO"
            or not principal.subject
            or principal.subject.strip() != principal.subject
            or not principal.source_id
            or principal.source_id.strip() != principal.source_id
        ):
            raise QueryNotFound(application_id)

        context_keys = (
            "application_id",
            "receipt_id",
            "reason_code",
            "failure_reason_code",
            "command_type",
            "envelope_fingerprint",
            "job_id",
            "attempt_id",
            "run_id",
            "route",
            "lifecycle_revision",
            "evidence_revision",
            "evidence_snapshot_id",
            "evidence_snapshot_digest",
            "release_id",
            "release_digest",
            "checker_build",
            "fence",
            "finding_count",
            "mandatory_blocker_count",
            "work_item_id",
            "assigned_subject",
            "admission_after_stop",
            "requeued_jobs",
            "admission_after_recovery",
        )
        with self._lock:
            self._reload_store()
            if application_id not in self._store.applications:
                raise QueryNotFound(application_id)

            seen_keys: set[str] = set()
            records: list[dict[str, Any]] = []
            for event in self._store.audit_events:
                event_time = event.get("event_time")
                event_sequence = event.get("event_sequence")
                event_time_key = event.get("event_time_key")
                if (
                    type(event_time) is not int
                    or event_time < 0
                    or type(event_sequence) is not int
                    or event_sequence < 1
                    or event_time_key
                    != f"{event_time:020d}:{event_sequence:010d}"
                    or event_time_key in seen_keys
                ):
                    raise RuntimeError("audit event time metadata is invalid")
                seen_keys.add(event_time_key)
                if (
                    event.get("application_id") != application_id
                    and event.get("action")
                    not in {"controlled_cohort_stop", "runtime_recovery"}
                ):
                    continue
                records.append(
                    {
                        "event_id": event["event_id"],
                        "event_time": event_time,
                        "event_sequence": event_sequence,
                        "event_time_key": event_time_key,
                        "actor": {
                            "subject": event.get("subject"),
                            "role": event.get("role"),
                            "scope": event.get("scope"),
                            "source_id": event.get("source_id"),
                        },
                        "action": event.get("action"),
                        "result": event.get("result"),
                        "context": {
                            key: copy.deepcopy(event[key])
                            for key in context_keys
                            if key in event
                        },
                    }
                )
            records.sort(key=lambda event: event["event_time_key"])
            return {
                "application_id": application_id,
                "events": records,
                "integrity": "verified",
            }

    def workspace_view(
        self,
        application_id: str,
        *,
        role: str = "",
        scope: str = "",
        subject: str = "",
        now: float | None = None,
        finding_id: str | None = None,
    ) -> dict[str, Any]:
        """Return finding-first minimized workspace data for an in-scope Reviewer."""
        if role != "reviewer" or not self.is_c_demo_scope(scope):
            raise QueryNotFound(application_id)
        with self._lock:
            self._reload_store()
            self._repair_published_projections()
            projection = self._store.projections.get(application_id)
            query_subject = subject
            query_time = float(self._clock()) if now is None else float(now)
            visible_scopes = {scope}
            if scope.startswith(self._SESSION_SCOPE_PREFIX):
                visible_scopes.add("C-DEMO")
            if (
                projection is None
                or projection.get("visibility_scope") not in visible_scopes
                or projection["phase"] != "Manual Review"
                or projection.get("assigned_subject") != query_subject
                or projection.get("claim_subject") != query_subject
                or float(projection.get("claim_expires_at", 0)) <= query_time
            ):
                raise QueryNotFound(application_id)
            findings = copy.deepcopy(projection["mandatory_blockers"])
            selected = next((f for f in findings if f["finding_id"] == finding_id), None)
            if selected is None and findings:
                selected = findings[0]
            return {
                "application_id": application_id,
                "work_item_id": projection["work_item_id"],
                "assigned_subject": projection["assigned_subject"],
                "claim_fence": projection["claim_fence"],
                "claim_expires_at": projection["claim_expires_at"],
                "track": "C-DEMO",
                "phase": projection["phase"],
                "route": projection["route"],
                "evidence_ready": projection["evidence_ready"],
                "lifecycle_revision": projection["lifecycle_revision"],
                "evidence_revision": projection["evidence_revision"],
                "current_run_id": projection["current_run_id"],
                "evidence_snapshot_id": projection["evidence_snapshot_id"],
                "evidence_snapshot_digest": projection[
                    "evidence_snapshot_digest"
                ],
                "projection_watermark": projection["projection_watermark"],
                "mandatory_blockers": findings,
                "selected_finding": selected,
                "actions": ["read_evidence"],
            }

    def _read_fixed_scenario(self, scenario_id: str) -> tuple[dict[str, Any], str]:
        source = (self.fixture_root / scenario_id).resolve()
        if self.fixture_root not in source.parents or source.name != scenario_id:
            raise ValueError("scenario escapes controlled fixture root")
        if source.suffix != ".json" or not source.is_file():
            raise ValueError("scenario is not a JSON file")
        source_bytes = source.read_bytes()
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        if (
            scenario_id != self._manifest.scenario_id
            or source_sha256 != self._manifest.source_sha256
        ):
            raise ValueError("controlled source does not match frozen artifact manifest")
        data = json.loads(source_bytes.decode("utf-8"))
        if not isinstance(data, dict) or not data.get("application_id"):
            raise ValueError("scenario must contain application_id")
        if not isinstance(data.get("documents"), list) or not data["documents"]:
            raise ValueError("scenario must contain documents")
        meta = data.get("meta")
        if not isinstance(meta, dict) or meta.get("field_source") != "synthetic":
            raise ValueError("S01 accepts only explicitly synthetic C-DEMO data")
        return copy.deepcopy(data), source_sha256

    def _canonicalize(
        self,
        payload: dict[str, Any],
        scenario_id: str,
        idempotency_key: str,
        *,
        source_sha256: str,
        principal: S01CommandPrincipal,
        idempotency_identity: str,
    ) -> CanonicalEnvelope:
        upstream_application_reference = str(payload["application_id"])
        source = {
            "adapter_id": "legacy-fixture-c-demo",
            "adapter_version": "1",
            "scenario_id": scenario_id,
            "source_sha256": source_sha256,
            "source_object_ref": f"c-demo-object:sha256:{source_sha256}",
            "upstream_application_reference": upstream_application_reference,
            "artifact_manifest_digest": self._manifest.digest,
            "source_provenance_manifest_version": (
                self._manifest.source_provenance_manifest_version
            ),
            "source_provenance_manifest_digest": (
                self._manifest.source_provenance_manifest_digest
            ),
        }
        document_references = sorted(
            str(document.get("doc_id") or "")
            for document in payload["documents"]
            if isinstance(document, dict) and str(document.get("doc_id") or "")
        )
        submission_scope = {
            "mode": "full",
            "track": "C-DEMO",
            "upstream_application_reference": upstream_application_reference,
            "document_references": document_references,
            "fact_kinds": ["application_identity", "field_observations"],
        }
        stream_identity = json.dumps(
            {
                "command_type": self.COMMAND_TYPE,
                "scope": principal.scope,
                "source_id": principal.source_id,
                "upstream_application_reference": upstream_application_reference,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        stream_id = self._stable_id("stream", stream_identity)
        source_revision_id = self._stable_id(
            "source_revision", f"{stream_id}:{source_sha256}:1"
        )
        batch_id = self._stable_id(
            "batch", f"{stream_id}:{source_revision_id}:1"
        )
        batch_manifest_material = json.dumps(
            {
                "batch_id": batch_id,
                "final_sequence": 1,
                "item_count": 1,
                "scope": submission_scope,
                "source_revision_ids": [source_revision_id],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        batch = {
            "batch_id": batch_id,
            "item_sequence": 1,
            "item_count": 1,
            "final_sequence": 1,
            "manifest_digest": hashlib.sha256(batch_manifest_material).hexdigest(),
            "scope_mode": "full",
            "closed": True,
        }
        envelope_id = self._stable_id(
            "envelope", f"{stream_id}:{source_revision_id}:{batch_id}:1"
        )
        idempotency_key_digest = hashlib.sha256(
            idempotency_key.encode("utf-8")
        ).hexdigest()
        envelope_metadata = {
            "version": self.ENVELOPE_VERSION,
            "schema_version": self.SCHEMA_VERSION,
            "semantic_version": self.SEMANTIC_VERSION,
            "command_type": self.COMMAND_TYPE,
            "scenario_id": scenario_id,
            "upstream_application_reference": upstream_application_reference,
            "envelope_id": envelope_id,
            "stream_id": stream_id,
            "source_revision_id": source_revision_id,
            "source_revision_sequence": 1,
            "previous_source_revision_id": None,
            "idempotency_identity": idempotency_identity,
            "idempotency_key_digest": idempotency_key_digest,
            "occurred_at": None,
            "occurred_at_status": "unknown",
            "produced_at": None,
            "produced_at_status": "unknown",
            "observed_at": None,
            "observed_at_status": "unknown",
            "received_at": self._C_DEMO_RECEIVED_AT,
            "received_at_status": "fixed_c_demo_protocol_time",
            "must_understand": [],
            "authenticated_context": {
                "subject": principal.subject,
                "role": principal.role,
                "scope": principal.scope,
                "source_id": principal.source_id,
            },
            "scope": submission_scope,
            "batch": batch,
        }
        canonical_payload = {
            "track": "C-DEMO",
            "envelope": envelope_metadata,
            "source": source,
            "application": self._adapt_application(
                payload,
                source=source,
                provenance_manifest=self._source_provenance_manifest,
            ),
        }
        encoded = json.dumps(
            canonical_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        fingerprint = hashlib.sha256(encoded).hexdigest()
        oracle_application = Application.from_dict(
            {
                "application_id": str(payload["application_id"]),
                "documents": copy.deepcopy(payload["documents"]),
            }
        )
        try:
            oracle_report = (
                self._legacy_oracle_runner(oracle_application)
                if self._legacy_oracle_runner is not None
                else self._release["legacy_oracle"].run(oracle_application)
            )
            oracle_outcomes = self._check_signature(oracle_report.checks)
        except Exception:
            oracle_outcomes = ()
        return CanonicalEnvelope(
            envelope_version=self.ENVELOPE_VERSION,
            schema_version=self.SCHEMA_VERSION,
            semantic_version=self.SEMANTIC_VERSION,
            command_type=self.COMMAND_TYPE,
            scenario_id=scenario_id,
            upstream_application_reference=upstream_application_reference,
            envelope_id=envelope_id,
            stream_id=stream_id,
            source_revision_id=source_revision_id,
            batch_id=batch_id,
            idempotency_identity=idempotency_identity,
            idempotency_key_digest=idempotency_key_digest,
            fingerprint=fingerprint,
            payload=canonical_payload,
            oracle_outcomes=oracle_outcomes,
        )

    def _admit(
        self,
        envelope: CanonicalEnvelope,
        *,
        principal: S01CommandPrincipal,
        binding_key: str,
        command_fingerprint: str,
    ) -> AdmissionResult:
        try:
            accepted_admissions = self._accepted_admission_authorities()
        except _ApplicationStateAuthorityUnavailable:
            return self._rejected(self._APPLICATION_STATE_FAILURE)
        if any(
            event["envelope"]["upstream_application_reference"]
            == envelope.upstream_application_reference
            and event["scope"] == principal.scope
            for event in accepted_admissions
        ):
            return self._rejected("APPLICATION_ALREADY_ADMITTED")
        try:
            app_id = self._application_id_allocator()
            if (
                not isinstance(app_id, str)
                or not app_id.startswith("app_")
                or not 8 <= len(app_id) <= 100
                or app_id.strip() != app_id
                or app_id in self._store.applications
            ):
                return self._rejected("STORAGE_UNAVAILABLE")
        except Exception:
            return self._rejected("STORAGE_UNAVAILABLE")
        receipt_id = self._stable_id("receipt", envelope.fingerprint)
        job_id = self._stable_id("job", envelope.fingerprint)
        evidence_revision = 1
        lifecycle_revision = 1
        accepted_envelope = copy.deepcopy(envelope.payload["envelope"])
        accepted_envelope.update(
            {
                "fingerprint": envelope.fingerprint,
                "disposition": AdmissionDisposition.ACCEPTED.value,
            }
        )
        result = AdmissionResult(
            disposition=AdmissionDisposition.ACCEPTED,
            application_id=app_id,
            receipt_id=receipt_id,
            job_id=job_id,
            replayed=False,
            lifecycle_revision=lifecycle_revision,
            evidence_revision=evidence_revision,
            audit_recorded=True,
            envelope_version=envelope.envelope_version,
            schema_version=envelope.schema_version,
            semantic_version=envelope.semantic_version,
            envelope_id=envelope.envelope_id,
            stream_id=envelope.stream_id,
            source_revision_id=envelope.source_revision_id,
            batch_id=envelope.batch_id,
            envelope_fingerprint=envelope.fingerprint,
            idempotency_identity=envelope.idempotency_identity,
            idempotency_key_digest=envelope.idempotency_key_digest,
            occurred_at=accepted_envelope["occurred_at"],
            occurred_at_status=accepted_envelope["occurred_at_status"],
            produced_at=accepted_envelope["produced_at"],
            produced_at_status=accepted_envelope["produced_at_status"],
            observed_at=accepted_envelope["observed_at"],
            observed_at_status=accepted_envelope["observed_at_status"],
            received_at=accepted_envelope["received_at"],
            received_at_status=accepted_envelope["received_at_status"],
            adapter_id=envelope.payload["source"]["adapter_id"],
            adapter_version=envelope.payload["source"]["adapter_version"],
            source_sha256=envelope.payload["source"]["source_sha256"],
            artifact_manifest_digest=envelope.payload["source"][
                "artifact_manifest_digest"
            ],
        )
        staged = copy.deepcopy(self._store)
        admitted_evidence = {
            "schema_version": "s01-admitted-evidence/1",
            "evidence": copy.deepcopy(envelope.payload["application"]["evidence"]),
        }
        admitted_evidence_bytes = json.dumps(
            admitted_evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        admitted_evidence_digest = hashlib.sha256(admitted_evidence_bytes).hexdigest()
        evidence_event_id = self._stable_id(
            "evidence", f"{app_id}:admitted:{envelope.fingerprint}"
        )
        application = {
            "application_id": app_id,
            "upstream_application_reference": envelope.upstream_application_reference,
            "track": "C-DEMO",
            "phase": "Intake",
            "cycle": 1,
            "lifecycle_revision": lifecycle_revision,
            "evidence_revision": evidence_revision,
            "envelope": copy.deepcopy(accepted_envelope),
            "source": copy.deepcopy(envelope.payload["source"]),
            "artifact_manifest": {
                "digest": self._manifest.digest,
                "scenario_id": self._manifest.scenario_id,
                "source_sha256": self._manifest.source_sha256,
                "source_provenance_manifest_version": (
                    self._manifest.source_provenance_manifest_version
                ),
                "source_provenance_manifest_digest": (
                    self._manifest.source_provenance_manifest_digest
                ),
                "release_id": self._manifest.release_id,
                "release_digest": self._manifest.release_digest,
                "checker_build": self._manifest.checker_build,
            },
            "admitted_evidence_event_id": evidence_event_id,
            "legacy_oracle_outcomes": copy.deepcopy(envelope.oracle_outcomes),
            "evidence_ready": False,
            "route": "pending_check",
            "phase_history": ["Intake"],
            "current_run_id": None,
            "projection_visible": False,
            "projection_pending": False,
        }
        try:
            self._before_write("admission.application")
            staged.applications[app_id] = application
            self._before_write("admission.lifecycle_event")
            staged.lifecycle_events.append(
                {
                    "event_id": self._stable_id(
                        "lifecycle", f"{app_id}:1:{lifecycle_revision}"
                    ),
                    "application_id": app_id,
                    "revision": lifecycle_revision,
                    "phase": "Intake",
                    "cycle": 1,
                    "reason_code": "ADMISSION_ACCEPTED",
                }
            )
            self._before_write("admission.evidence_event")
            staged.evidence_events.append(
                {
                    "event_id": evidence_event_id,
                    "application_id": app_id,
                    "revision": evidence_revision,
                    "kind": "admitted_snapshot",
                    "fingerprint": envelope.fingerprint,
                    "content_sha256": admitted_evidence_digest,
                    "content_bytes": len(admitted_evidence_bytes),
                    "payload": admitted_evidence,
                }
            )
            self._before_write("admission.audit_event")
            staged.audit_events.append(
                {
                    "event_id": self._stable_id(
                        "audit", f"controlled_admission:{receipt_id}"
                    ),
                    "action": "controlled_admission",
                    "subject": principal.subject,
                    "role": principal.role,
                    "scope": principal.scope,
                    "source_id": principal.source_id,
                    "application_id": app_id,
                    "receipt_id": receipt_id,
                    "result": "accepted",
                    "reason_code": "ADMISSION_ACCEPTED",
                    "command_type": envelope.command_type,
                    "envelope_fingerprint": envelope.fingerprint,
                    "idempotency_scope": binding_key,
                    "envelope": copy.deepcopy(accepted_envelope),
                    **self._audit_time_fields(staged),
                }
            )
            self._before_write("admission.job")
            staged.jobs.append(
                self._admission_job_record(job_id, app_id, envelope.fingerprint)
            )
            self._before_write("admission.outbox")
            staged.outbox.append(
                {
                    "event_id": self._stable_id("outbox", job_id),
                    "kind": "controlled_check_requested",
                    "application_id": app_id,
                    "job_id": job_id,
                    "fingerprint": envelope.fingerprint,
                    "status": "pending",
                }
            )
            self._before_write("admission.idempotency_binding")
            staged.idempotency[binding_key] = (
                command_fingerprint,
                result,
            )
            self._before_write("admission.receipt")
            staged.receipts[receipt_id] = result
            self._before_write("admission.publish")
        except _StoreWriteFailure:
            return self._rejected("STORAGE_UNAVAILABLE")
        try:
            staged.persist()
        except StaleStoreRevision:
            self._reload_store()
            previous = self._store.idempotency.get(binding_key)
            if previous is None:
                try:
                    already_admitted = any(
                        event["envelope"]["upstream_application_reference"]
                        == envelope.upstream_application_reference
                        and event["scope"] == principal.scope
                        for event in self._accepted_admission_authorities()
                    )
                except _ApplicationStateAuthorityUnavailable:
                    return self._rejected(self._APPLICATION_STATE_FAILURE)
                if already_admitted:
                    return self._rejected("APPLICATION_ALREADY_ADMITTED")
                return self._rejected("STORAGE_UNAVAILABLE")
            previous_fingerprint, previous_result = previous
            if previous_fingerprint == command_fingerprint:
                return AdmissionResult(
                    **{**previous_result.__dict__, "replayed": True}
                )
            return self._idempotency_conflict(previous_result)
        except Exception:
            return self._rejected("STORAGE_UNAVAILABLE")
        self._store = staged
        return result

    def _before_write(self, write_point: str) -> None:
        if self._fault_injector is None:
            return
        try:
            self._fault_injector(write_point)
        except Exception as error:
            raise _StoreWriteFailure(write_point) from error

    def _replicate_admission_audit(
        self,
        envelope: CanonicalEnvelope,
        result: AdmissionResult,
        *,
        principal: S01CommandPrincipal,
    ) -> None:
        """Best-effort replica after the target audit fact has committed."""
        if self._audit_writer is None:
            return
        try:
            accepted_envelope = copy.deepcopy(envelope.payload["envelope"])
            accepted_envelope.update(
                {
                    "fingerprint": envelope.fingerprint,
                    "disposition": AdmissionDisposition.ACCEPTED.value,
                }
            )
            self._audit_writer(
                {
                    "action": "controlled_admission",
                    "track": "C-DEMO",
                    "subject": principal.subject,
                    "role": principal.role,
                    "scope": principal.scope,
                    "source_id": principal.source_id,
                    "application_id": result.application_id,
                    "receipt_id": result.receipt_id,
                    "envelope_fingerprint": envelope.fingerprint,
                    "envelope": accepted_envelope,
                }
            )
        except Exception:
            return

    @staticmethod
    def _stable_id(prefix: str, fingerprint: str) -> str:
        return f"{prefix}_{hashlib.sha256(fingerprint.encode()).hexdigest()[:24]}"

    @staticmethod
    def _default_application_id() -> str:
        return f"app_{secrets.token_hex(12)}"

    def _command_fingerprint(self, scenario_id: str) -> str:
        encoded = json.dumps(
            {
                "command_type": self.COMMAND_TYPE,
                "scenario_id": scenario_id,
                "artifact_manifest_digest": self._manifest.digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _load_source_provenance_manifest(self) -> dict[str, Any]:
        source_object_ref = (
            "c-demo-object:sha256:" + self._C_DEMO_PROVENANCE_SOURCE_SHA256
        )
        entries: list[dict[str, Any]] = []
        for item in copy.deepcopy(self._C_DEMO_PROVENANCE_ENTRIES):
            if isinstance(item, (list, tuple)) and len(item) == 4:
                document_id, field, source_page, source_region = item
                entries.append(
                    {
                        "document_id": document_id,
                        "field": field,
                        "source_object_ref": source_object_ref,
                        "source_sha256": self._C_DEMO_PROVENANCE_SOURCE_SHA256,
                        "source_page": source_page,
                        "source_region": source_region,
                        "producer_id": "c-demo-registered-source",
                        "producer_version": "1",
                    }
                )
            else:
                entries.append({"malformed_entry": item})
        values = {
            "schema_version": self._C_DEMO_PROVENANCE_SCHEMA,
            "scenario_id": next(iter(self._ALLOWED_SCENARIOS)),
            "source_kind": "synthetic-json-pages/1",
            "bound_source_sha256": self._C_DEMO_PROVENANCE_SOURCE_SHA256,
            "source_object_ref": source_object_ref,
            "producer_id": "c-demo-registered-source",
            "producer_version": "1",
            "entries": entries,
        }
        encoded = json.dumps(
            values,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {**values, "digest": hashlib.sha256(encoded).hexdigest()}

    def _build_artifact_manifest(self) -> S01ArtifactManifest:
        scenario_id = next(iter(self._ALLOWED_SCENARIOS))
        source = (self.fixture_root / scenario_id).resolve()
        if self.fixture_root not in source.parents or not source.is_file():
            raise FileNotFoundError("controlled scenario artifact is unavailable")
        source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        values = {
            "scenario_id": scenario_id,
            "source_sha256": source_sha256,
            "source_provenance_manifest_version": self._source_provenance_manifest[
                "schema_version"
            ],
            "source_provenance_manifest_digest": self._source_provenance_manifest[
                "digest"
            ],
            "release_id": self._release["release_id"],
            "release_digest": self._release["digest"],
            "checker_build": self._release["checker_build"],
            "adapter_id": "legacy-fixture-c-demo",
            "adapter_version": "1",
            "envelope_version": self.ENVELOPE_VERSION,
            "schema_version": self.SCHEMA_VERSION,
            "semantic_version": self.SEMANTIC_VERSION,
        }
        encoded = json.dumps(
            values,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return S01ArtifactManifest(
            **values,
            digest=hashlib.sha256(encoded).hexdigest(),
        )

    @staticmethod
    def _valid_idempotency_key(value: str) -> bool:
        return isinstance(value, str) and 1 <= len(value) <= 200 and value.strip() == value

    @classmethod
    def is_c_demo_scope(cls, scope: object) -> bool:
        if scope == "C-DEMO":
            return True
        if not isinstance(scope, str) or not scope.startswith(cls._SESSION_SCOPE_PREFIX):
            return False
        session_id = scope[len(cls._SESSION_SCOPE_PREFIX) :]
        return len(session_id) == 32 and all(character in "0123456789abcdef" for character in session_id)

    @classmethod
    def _valid_principal(cls, principal: S01CommandPrincipal) -> bool:
        return (
            isinstance(principal.subject, str)
            and bool(principal.subject)
            and principal.subject.strip() == principal.subject
            and principal.role == "integrator"
            and cls.is_c_demo_scope(principal.scope)
            and isinstance(principal.source_id, str)
            and bool(principal.source_id)
            and principal.source_id.strip() == principal.source_id
        )

    @classmethod
    def _idempotency_binding_key(
        cls, principal: S01CommandPrincipal, idempotency_key: str
    ) -> str:
        encoded = json.dumps(
            {
                "action": cls.COMMAND_TYPE,
                "key": idempotency_key,
                "role": principal.role,
                "scope": principal.scope,
                "source_id": principal.source_id,
                "subject": principal.subject,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"s01_idempotency_{hashlib.sha256(encoded).hexdigest()}"

    @staticmethod
    def _rejected(reason_code: str) -> AdmissionResult:
        return AdmissionResult(
            disposition=AdmissionDisposition.REJECTED,
            reason_code=reason_code,
        )

    @staticmethod
    def _idempotency_conflict(previous: AdmissionResult) -> AdmissionResult:
        return AdmissionResult(
            disposition=AdmissionDisposition.REJECTED,
            receipt_id=previous.receipt_id,
            reason_code="IDEMPOTENCY_CONFLICT",
            envelope_version=previous.envelope_version,
            schema_version=previous.schema_version,
            semantic_version=previous.semantic_version,
            envelope_id=previous.envelope_id,
            stream_id=previous.stream_id,
            source_revision_id=previous.source_revision_id,
            batch_id=previous.batch_id,
            envelope_fingerprint=previous.envelope_fingerprint,
            idempotency_identity=previous.idempotency_identity,
            idempotency_key_digest=previous.idempotency_key_digest,
            occurred_at=previous.occurred_at,
            occurred_at_status=previous.occurred_at_status,
            produced_at=previous.produced_at,
            produced_at_status=previous.produced_at_status,
            observed_at=previous.observed_at,
            observed_at_status=previous.observed_at_status,
            received_at=previous.received_at,
            received_at_status=previous.received_at_status,
            adapter_id=previous.adapter_id,
            adapter_version=previous.adapter_version,
            artifact_manifest_digest=previous.artifact_manifest_digest,
        )

    def _record_rejection(
        self,
        *,
        reason_code: str,
        command_fingerprint: str,
        binding_key: str,
        principal: S01CommandPrincipal,
    ) -> AdmissionResult:
        if not self.audit_available:
            return self._rejected("AUDIT_UNAVAILABLE")
        if not self.storage_available:
            return self._rejected("STORAGE_UNAVAILABLE")
        for _ in range(2):
            previous = self._store.idempotency.get(binding_key)
            if previous is not None:
                previous_fingerprint, previous_result = previous
                if previous_fingerprint == command_fingerprint:
                    return AdmissionResult(
                        **{**previous_result.__dict__, "replayed": True}
                    )
                return self._idempotency_conflict(previous_result)
            receipt_id = self._stable_id(
                "receipt", f"rejected:{binding_key}:{command_fingerprint}"
            )
            result = AdmissionResult(
                disposition=AdmissionDisposition.REJECTED,
                receipt_id=receipt_id,
                reason_code=reason_code,
                audit_recorded=True,
                envelope_version=self.ENVELOPE_VERSION,
                schema_version=self.SCHEMA_VERSION,
                semantic_version=self.SEMANTIC_VERSION,
                envelope_fingerprint=command_fingerprint,
                adapter_id="legacy-fixture-c-demo",
                adapter_version="1",
                artifact_manifest_digest=self._manifest.digest,
            )
            staged = copy.deepcopy(self._store)
            staged.audit_events.append(
                {
                    "event_id": self._stable_id(
                        "audit", f"controlled_rejection:{receipt_id}"
                    ),
                    "action": "controlled_admission",
                    "subject": principal.subject,
                    "role": principal.role,
                    "scope": principal.scope,
                    "source_id": principal.source_id,
                    "application_id": None,
                    "receipt_id": receipt_id,
                    "result": "rejected",
                    "reason_code": reason_code,
                    "command_type": self.COMMAND_TYPE,
                    "command_fingerprint": command_fingerprint,
                    "idempotency_scope": binding_key,
                    **self._audit_time_fields(staged),
                }
            )
            staged.idempotency[binding_key] = (command_fingerprint, result)
            staged.receipts[receipt_id] = result
            try:
                staged.persist()
            except StaleStoreRevision:
                self._reload_store()
                continue
            except Exception:
                return self._rejected("STORAGE_UNAVAILABLE")
            self._store = staged
            return result
        return self._rejected("STORAGE_UNAVAILABLE")

    def _load_baseline_release(self) -> dict[str, Any]:
        if not self.rules_path.is_file():
            raise FileNotFoundError(f"baseline release not found: {self.rules_path}")
        from task4_consistency.kb.store import EntityKB

        rules_bytes = self.rules_path.read_bytes()
        rules_digest = hashlib.sha256(rules_bytes).hexdigest()
        with tempfile.TemporaryDirectory(prefix="s01-rules-release-") as snapshot_dir:
            snapshot_path = Path(snapshot_dir) / "rules.yaml"
            snapshot_path.write_bytes(rules_bytes)
            snapshot_path.chmod(0o400)
            cfg = load_rules(snapshot_path)
        target_release = TargetRelease.compile(
            cfg, rules_digest, knowledge=EntityKB().to_dict()
        )
        manifest = target_release.public_manifest()
        return {
            "release_id": manifest["release_id"],
            "package": cfg.package,
            "version": str(cfg.version),
            "digest": manifest["digest"],
            "rules_digest": manifest["rules_digest"],
            "knowledge_digest": manifest["knowledge_digest"],
            "normalizer_digest": manifest["normalizer_digest"],
            "checker_build": manifest["checker_build"],
            "limits": manifest["limits"],
            "applicable_check_ids": manifest["applicable_check_ids"],
            "applicable_check_count": manifest["applicable_check_count"],
            "target_release": target_release,
            "legacy_oracle": RuleEngine(cfg),
        }

    def _admitted_evidence(self, app: dict[str, Any]) -> list[dict[str, Any]]:
        matching = [
            event
            for event in self._store.evidence_events
            if event.get("application_id") == app["application_id"]
            and event.get("revision") == app["evidence_revision"]
            and event.get("kind") == "admitted_snapshot"
        ]
        if len(matching) != 1:
            raise RuntimeError("admitted evidence authority is unavailable")
        event = matching[0]
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("schema_version") != (
            "s01-admitted-evidence/1"
        ):
            raise RuntimeError("admitted evidence authority is invalid")
        evidence = payload.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise RuntimeError("admitted evidence authority is empty")
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != event.get("content_sha256"):
            raise RuntimeError("admitted evidence content digest does not match")
        return copy.deepcopy(evidence)

    @classmethod
    def _verified_provenance_entries(
        cls,
        provenance_manifest: dict[str, Any],
        source: dict[str, Any],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        material = copy.deepcopy(provenance_manifest)
        supplied_digest = material.pop("digest", None)
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if (
            not isinstance(supplied_digest, str)
            or hashlib.sha256(encoded).hexdigest() != supplied_digest
            or provenance_manifest.get("schema_version")
            != cls._C_DEMO_PROVENANCE_SCHEMA
            or provenance_manifest.get("scenario_id") != source.get("scenario_id")
            or provenance_manifest.get("bound_source_sha256")
            != source.get("source_sha256")
            or provenance_manifest.get("source_object_ref")
            != source.get("source_object_ref")
            or provenance_manifest.get("producer_id")
            != "c-demo-registered-source"
            or provenance_manifest.get("producer_version") != "1"
            or source.get("source_provenance_manifest_version")
            != provenance_manifest.get("schema_version")
            or source.get("source_provenance_manifest_digest") != supplied_digest
            or not isinstance(provenance_manifest.get("entries"), list)
        ):
            return {}

        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for entry in provenance_manifest["entries"]:
            if not isinstance(entry, dict):
                continue
            key = (entry.get("document_id"), entry.get("field"))
            if not all(isinstance(value, str) and value for value in key):
                continue
            grouped.setdefault(key, []).append(entry)

        verified: dict[tuple[str, str], dict[str, Any]] = {}
        for key, entries in grouped.items():
            if len(entries) != 1:
                continue
            entry = entries[0]
            source_page = entry.get("source_page")
            if (
                isinstance(source_page, bool)
                or not isinstance(source_page, int)
                or source_page < 1
                or entry.get("source_object_ref") != source.get("source_object_ref")
                or entry.get("source_sha256") != source.get("source_sha256")
                or entry.get("producer_id") != provenance_manifest.get("producer_id")
                or entry.get("producer_version")
                != provenance_manifest.get("producer_version")
                or not isinstance(entry.get("source_region"), str)
                or not entry["source_region"]
            ):
                continue
            verified[key] = copy.deepcopy(entry)
        return verified

    @classmethod
    def _adapt_application(
        cls,
        payload: dict[str, Any],
        *,
        source: dict[str, Any],
        provenance_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        """Map the legacy fixture shape to target-owned evidence facts.

        Labels and expected verdicts are intentionally not copied into the
        target envelope; they remain development-only evaluation inputs.
        """
        verified_provenance = cls._verified_provenance_entries(
            provenance_manifest, source
        )
        evidence: list[dict[str, Any]] = []
        for document_index, document in enumerate(payload.get("documents") or []):
            if not isinstance(document, dict):
                raise ValueError("document must be an object")
            field_values = document.get("fields")
            if not isinstance(field_values, dict):
                raise ValueError("document fields must be an object")
            fields: dict[str, dict[str, Any]] = {}
            document_id = str(document.get("doc_id") or "")
            for field_name, value in field_values.items():
                if isinstance(value, str):
                    field_value = {"raw": value, "confidence": 1.0}
                elif isinstance(value, dict):
                    field_value = {
                        "raw": value.get("raw"),
                        "confidence": float(value.get("confidence", 1.0)),
                    }
                else:
                    raise ValueError("field value must be a string or object")
                expected_region = (
                    f"/documents/{document_index}/fields/"
                    f"{str(field_name).replace('~', '~0').replace('/', '~1')}"
                )
                provenance = verified_provenance.get(
                    (document_id, str(field_name))
                )
                if provenance is not None and (
                    provenance.get("source_region") != expected_region
                ):
                    provenance = None
                observation_material = json.dumps(
                    {
                        "source_sha256": source["source_sha256"],
                        "document_id": document_id,
                        "field": str(field_name),
                        "source_region": expected_region,
                        "provenance_manifest_digest": (
                            provenance_manifest.get("digest")
                        ),
                        "raw": field_value["raw"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                legacy_provenance = value if isinstance(value, dict) else {}
                field_value.update(
                    {
                        "observation_id": (
                            "observation_"
                            + hashlib.sha256(observation_material).hexdigest()[:24]
                        ),
                        "source_object_ref": (
                            provenance.get("source_object_ref")
                            if provenance is not None
                            else legacy_provenance.get("source_object_ref")
                        ),
                        "source_sha256": (
                            provenance.get("source_sha256")
                            if provenance is not None
                            else legacy_provenance.get("source_sha256")
                        ),
                        "source_page": (
                            provenance.get("source_page")
                            if provenance is not None
                            else legacy_provenance.get("source_page")
                        ),
                        "source_region": (
                            provenance.get("source_region")
                            if provenance is not None
                            else legacy_provenance.get("source_region")
                        ),
                        "producer_id": (
                            provenance.get("producer_id")
                            if provenance is not None
                            else legacy_provenance.get("producer_id")
                        ),
                        "producer_version": (
                            provenance.get("producer_version")
                            if provenance is not None
                            else legacy_provenance.get("producer_version")
                        ),
                        "provenance_manifest_digest": (
                            provenance_manifest["digest"]
                            if provenance is not None
                            else None
                        ),
                        "evidence_eligible": provenance is not None,
                        "eligibility_reason": (
                            "REGISTERED_SOURCE_PROVENANCE_VERIFIED"
                            if provenance is not None
                            else "PROVENANCE_INELIGIBLE"
                        ),
                    }
                )
                fields[str(field_name)] = field_value
            evidence.append(
                {
                    "document_id": document_id,
                    "document_role": str(document.get("doc_type") or ""),
                    "fields": fields,
                }
            )
        if not evidence or any(not x["document_id"] or not x["document_role"] for x in evidence):
            raise ValueError("evidence requires document IDs and roles")
        return {"evidence": evidence}

    def _claim_job(
        self, worker_id: str, now: int
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
        for _ in range(2):
            staged = copy.deepcopy(self._store)
            selected = None
            for job in staged.jobs:
                if job["status"] == "complete" or job["status"] == "diagnostic":
                    continue
                if job["status"] == "leased" and job.get("lease_until", 0) > now:
                    continue
                if int(job.get("retry_not_before", 0)) > now:
                    continue
                app = staged.applications.get(str(job.get("application_id")))
                self._require_admitted_release(app)
                if job["status"] == "leased":
                    job["recovery_reason"] = "LEASE_EXPIRED_RETRY"
                job["status"] = "leased"
                job.pop("retry_not_before", None)
                job["worker_id"] = worker_id
                job["fence"] = int(job.get("fence", 0)) + 1
                job["attempt_no"] = int(job.get("attempt_no", 0)) + 1
                job["lease_until"] = now + 30
                selected = job
                break
            if selected is None:
                return None
            app = staged.applications[selected["application_id"]]
            run_spec = self._freeze_run_spec(app, selected, store=staged)
            attempt_id = self._stable_id(
                "attempt",
                f"{selected['job_id']}:{selected['attempt_no']}:{worker_id}",
            )
            attempt = {
                "attempt_id": attempt_id,
                "job_id": selected["job_id"],
                "application_id": selected["application_id"],
                "worker_id": worker_id,
                "fence": selected["fence"],
                "status": "started",
                "run_spec": copy.deepcopy(run_spec),
            }
            staged.attempts.append(attempt)
            try:
                staged.persist()
            except StaleStoreRevision:
                self._reload_store()
                continue
            self._store = staged
            return selected, attempt, run_spec
        return None

    def _require_admitted_release(self, app: dict[str, Any] | None) -> None:
        self._require_application_state_authority(app)
        manifest = app.get("artifact_manifest") if isinstance(app, dict) else None
        application_id = app.get("application_id") if isinstance(app, dict) else None
        accepted_receipts = [
            receipt
            for receipt in self._store.receipts.values()
            if isinstance(receipt, AdmissionResult)
            and receipt.disposition is AdmissionDisposition.ACCEPTED
            and receipt.application_id == application_id
        ]
        if (
            len(accepted_receipts) != 1
            or accepted_receipts[0].artifact_manifest_digest != self._manifest.digest
        ):
            raise _PinnedReleaseUnavailable(self._PINNED_RELEASE_FAILURE)
        expected = (
            manifest.get("release_id"),
            manifest.get("release_digest"),
            manifest.get("checker_build"),
        ) if isinstance(manifest, dict) else ()
        actual = (
            self._release["release_id"],
            self._release["digest"],
            self._release["checker_build"],
        )
        if expected != actual:
            raise _PinnedReleaseUnavailable(self._PINNED_RELEASE_FAILURE)

    def _accepted_admission_authorities(self) -> list[dict[str, Any]]:
        authorities: list[dict[str, Any]] = []
        try:
            for event in self._store.audit_events:
                if (
                    event.get("action") != "controlled_admission"
                    or event.get("result") != "accepted"
                ):
                    continue
                application_id = event.get("application_id")
                envelope = event.get("envelope")
                authenticated_context = (
                    envelope.get("authenticated_context")
                    if isinstance(envelope, dict)
                    else None
                )
                if (
                    not isinstance(application_id, str)
                    or not application_id
                    or not isinstance(envelope, dict)
                    or not isinstance(
                        envelope.get("upstream_application_reference"), str
                    )
                    or not envelope["upstream_application_reference"]
                    or not self.is_c_demo_scope(event.get("scope"))
                    or not isinstance(authenticated_context, dict)
                    or authenticated_context.get("scope") != event.get("scope")
                    or event.get("envelope_fingerprint")
                    != envelope.get("fingerprint")
                ):
                    raise ValueError("accepted admission authority is invalid")
                authorities.append(event)
        except (KeyError, TypeError, ValueError) as error:
            raise _ApplicationStateAuthorityUnavailable(
                self._APPLICATION_STATE_FAILURE
            ) from error
        return authorities

    def _application_visibility_scope(self, application_id: str) -> str:
        authorities = [
            event
            for event in self._accepted_admission_authorities()
            if event["application_id"] == application_id
        ]
        if len(authorities) != 1:
            raise _ApplicationStateAuthorityUnavailable(
                self._APPLICATION_STATE_FAILURE
            )
        return str(authorities[0]["scope"])

    def _application_review_assignee(self, application_id: str) -> str:
        authorities = [
            event
            for event in self._accepted_admission_authorities()
            if event["application_id"] == application_id
        ]
        if len(authorities) != 1:
            raise _ApplicationStateAuthorityUnavailable(
                self._APPLICATION_STATE_FAILURE
            )
        subject = authorities[0].get("subject")
        if not isinstance(subject, str) or not subject or subject.strip() != subject:
            raise _ApplicationStateAuthorityUnavailable(
                self._APPLICATION_STATE_FAILURE
            )
        return subject

    def _require_application_state_authority(
        self, app: dict[str, Any] | None
    ) -> None:
        try:
            if not isinstance(app, dict):
                raise ValueError("application is unavailable")
            application_id = app.get("application_id")
            if not isinstance(application_id, str) or not application_id:
                raise ValueError("application identity is invalid")
            admission_authorities = [
                event
                for event in self._accepted_admission_authorities()
                if event["application_id"] == application_id
            ]
            if len(admission_authorities) != 1:
                raise ValueError("admission authority is unavailable")
            admitted_envelope = admission_authorities[0]["envelope"]
            if (
                app.get("upstream_application_reference")
                != admitted_envelope["upstream_application_reference"]
                or app.get("envelope") != admitted_envelope
            ):
                raise ValueError("mutable application identity disagrees with authority")

            lifecycle = [
                event
                for event in self._store.lifecycle_events
                if event.get("application_id") == application_id
            ]
            canonical_events: list[tuple[int, int, str]] = []
            for event in lifecycle:
                revision = event.get("revision")
                cycle = event.get("cycle")
                phase = event.get("phase")
                if (
                    isinstance(revision, bool)
                    or not isinstance(revision, int)
                    or isinstance(cycle, bool)
                    or not isinstance(cycle, int)
                    or not isinstance(phase, str)
                    or not phase
                ):
                    raise ValueError("lifecycle event is invalid")
                canonical_events.append((revision, cycle, phase))
            canonical_events.sort(key=lambda event: event[0])
            if not canonical_events:
                raise ValueError("lifecycle authority is unavailable")
            revisions = [event[0] for event in canonical_events]
            if revisions != list(range(1, len(canonical_events) + 1)):
                raise ValueError("lifecycle authority is not contiguous")
            if canonical_events[0] != (1, 1, "Intake"):
                raise ValueError("lifecycle admission event is invalid")
            for previous, current in zip(canonical_events, canonical_events[1:]):
                if current[1] != 1 or current[2] not in self._ALLOWED_PHASE_SUCCESSORS.get(
                    previous[2], frozenset()
                ):
                    raise ValueError("lifecycle transition history is invalid")

            phases = [event[2] for event in canonical_events]
            current_phase = phases[-1]
            expected_evidence_ready = current_phase not in {"Intake", "Assembly"}
            expected_route = {
                "Manual Review": "manual_review",
                "Verification Completed": "auto_complete",
            }.get(current_phase, "pending_check")
            observed_cycle = app.get("cycle")
            observed_revision = app.get("lifecycle_revision")
            if (
                isinstance(observed_cycle, bool)
                or not isinstance(observed_cycle, int)
                or observed_cycle != 1
                or isinstance(observed_revision, bool)
                or not isinstance(observed_revision, int)
                or observed_revision != revisions[-1]
                or app.get("phase") != current_phase
                or app.get("phase_history") != phases
                or app.get("evidence_ready") is not expected_evidence_ready
                or app.get("route") != expected_route
            ):
                raise ValueError("mutable lifecycle state disagrees with authority")

            admitted_events = [
                event
                for event in self._store.evidence_events
                if event.get("application_id") == application_id
                and event.get("kind") == "admitted_snapshot"
            ]
            if len(admitted_events) != 1:
                raise ValueError("admitted evidence authority is unavailable")
            evidence_revision = admitted_events[0].get("revision")
            observed_evidence_revision = app.get("evidence_revision")
            if (
                isinstance(evidence_revision, bool)
                or not isinstance(evidence_revision, int)
                or evidence_revision != 1
                or isinstance(observed_evidence_revision, bool)
                or not isinstance(observed_evidence_revision, int)
                or observed_evidence_revision != evidence_revision
                or any(
                    isinstance(event.get("revision"), bool)
                    or event.get("revision") != evidence_revision
                    for event in self._store.evidence_events
                    if event.get("application_id") == application_id
                )
            ):
                raise ValueError("evidence revision disagrees with authority")
            self._admitted_evidence(app)
        except _ApplicationStateAuthorityUnavailable:
            raise
        except (KeyError, TypeError, ValueError, RuntimeError) as error:
            raise _ApplicationStateAuthorityUnavailable(
                self._APPLICATION_STATE_FAILURE
            ) from error

    def _freeze_run_spec(
        self,
        app: dict[str, Any],
        job: dict[str, Any],
        *,
        store: _TargetStore | None = None,
    ) -> dict[str, Any]:
        owner = store or self._store
        self._require_admitted_release(app)
        recovery_reason = job.pop("recovery_reason", None)
        if app["phase"] == "Checking" and recovery_reason is not None:
            app["evidence_ready"] = False
            app["route"] = "pending_check"
            app["projection_visible"] = False
            app["projection_pending"] = False
            self._transition_lifecycle(app, "Assembly", recovery_reason, store=owner)
        if app["phase"] == "Intake":
            self._transition_lifecycle(
                app, "Assembly", "ADMITTED_EVIDENCE_ASSEMBLED", store=owner
            )
        if not app["evidence_ready"]:
            app["evidence_ready"] = True
            self._transition_lifecycle(
                app, "Evidence Ready", "EVIDENCE_SNAPSHOT_FROZEN", store=owner
            )
        self._transition_lifecycle(app, "Checking", "CHECK_JOB_STARTED", store=owner)
        snapshot_payload = {
            "schema_version": "s01-evidence-snapshot/1",
            "evidence": self._admitted_evidence(app),
        }
        snapshot_bytes = json.dumps(
            snapshot_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()
        snapshot_id = f"snapshot_sha256_{snapshot_digest}"
        run_id = self._stable_id(
            "run",
            ":".join(
                (
                    job["job_id"],
                    str(app["cycle"]),
                    snapshot_id,
                    self._release["digest"],
                    self._release["checker_build"],
                )
            ),
        )
        owner.evidence_events.append(
            {
                "event_id": self._stable_id(
                    "evidence",
                    f"{run_id}:snapshot:{snapshot_id}:fence:{job['fence']}",
                ),
                "application_id": app["application_id"],
                "revision": app["evidence_revision"],
                "snapshot_id": snapshot_id,
                "kind": "immutable_ready_snapshot",
                "content_sha256": snapshot_digest,
                "content_bytes": len(snapshot_bytes),
                "payload": snapshot_payload,
            }
        )
        return {
            "run_id": run_id,
            "application_id": app["application_id"],
            "cycle": app["cycle"],
            "lifecycle_revision": app["lifecycle_revision"],
            "evidence_snapshot_id": snapshot_id,
            "evidence_snapshot_digest": snapshot_digest,
            "evidence_snapshot": snapshot_payload,
            "evidence_revision": app["evidence_revision"],
            "evidence_readiness_policy": "c-demo-readiness/1",
            "baseline_release": copy.deepcopy(
                {
                    key: value
                    for key, value in self._release.items()
                    if key not in {"target_release", "legacy_oracle"}
                }
            ),
            "release_id": self._release["release_id"],
            "release_digest": self._release["digest"],
            "checker_build": self._release["checker_build"],
            "fence": job["fence"],
            "limits": copy.deepcopy(self._release["limits"]),
            "applicable_check_ids": self._release["applicable_check_ids"],
            "applicable_check_count": self._release["applicable_check_count"],
        }

    def _run_checker(self, run_spec: dict[str, Any]):
        documents = []
        for evidence in run_spec["evidence_snapshot"]["evidence"]:
            documents.append(
                {
                    "doc_id": evidence["document_id"],
                    "doc_type": evidence["document_role"],
                    "fields": copy.deepcopy(evidence["fields"]),
                }
            )
        application = Application.from_dict(
            {"application_id": run_spec["application_id"], "documents": documents}
        )
        if self._checker_runner is not None:
            probe_result = self._checker_runner(application)
            self._convert_run_result(probe_result, run_spec)
        return self._target_checker.run(run_spec)

    @staticmethod
    def _convert_run_result(report: Any, run_spec: dict[str, Any]) -> _RunResult:
        try:
            if isinstance(report, _RunResult):
                if report.application_id != run_spec["application_id"]:
                    raise _InvalidRunResult(
                        "target result application does not match RunSpec"
                    )
                if not report.checks:
                    raise _InvalidRunResult("target result checks must not be empty")
                for check in report.checks:
                    if not isinstance(check, _RunCheckResult):
                        raise _InvalidRunResult("target result contains an invalid check")
                    if check.verdict not in {
                        "consistent",
                        "inconsistent",
                        "uncertain",
                        "skipped",
                    }:
                        raise _InvalidRunResult(
                            "target result contains a non-terminal verdict"
                        )
                    if check.severity not in {"critical", "major", "minor", "info"}:
                        raise _InvalidRunResult("target result contains invalid severity")
                actual_ids = tuple(check.rule_id for check in report.checks)
                expected_ids = tuple(run_spec["applicable_check_ids"])
                if len(actual_ids) != run_spec["applicable_check_count"]:
                    raise _InvalidRunResult(
                        "target result cardinality does not match RunSpec"
                    )
                if len(set(actual_ids)) != len(actual_ids):
                    raise _InvalidRunResult("target result contains duplicate check IDs")
                if set(actual_ids) != set(expected_ids):
                    raise _InvalidRunResult("target result checks do not match RunSpec")
                return report
            if not isinstance(report, Report):
                raise _InvalidRunResult("checker result must be a Report")
            if report.application_id != run_spec["application_id"]:
                raise _InvalidRunResult("checker result application does not match RunSpec")
            if not isinstance(report.checks, list):
                raise _InvalidRunResult("checker result checks must be a list")
            if not report.checks:
                raise _InvalidRunResult("checker result checks must not be empty")
            if len(report.checks) > run_spec["limits"]["max_findings"]:
                raise _InvalidRunResult("checker result exceeds frozen finding limit")

            checks: list[_RunCheckResult] = []
            frozen_fields = {
                (str(document["document_id"]), str(field_name)): value
                for document in run_spec["evidence_snapshot"]["evidence"]
                for field_name, value in document["fields"].items()
                if isinstance(value, dict)
            }
            for check in report.checks:
                if not isinstance(check, CheckResult):
                    raise _InvalidRunResult("checker result contains an invalid check")
                if not isinstance(check.rule_id, str) or not check.rule_id:
                    raise _InvalidRunResult("checker result contains an invalid check ID")
                if not isinstance(check.verdict, Verdict):
                    raise _InvalidRunResult("checker result contains a non-terminal verdict")
                if not isinstance(check.severity, Severity):
                    raise _InvalidRunResult("checker result contains an invalid severity")
                if not isinstance(check.reason_codes, list) or any(
                    not isinstance(reason, str) for reason in check.reason_codes
                ):
                    raise _InvalidRunResult("checker result contains invalid reason codes")
                if not isinstance(check.snapshots, list):
                    raise _InvalidRunResult("checker result contains invalid evidence links")

                evidence_links: list[_RunEvidenceLink] = []
                for snapshot in check.snapshots:
                    if not isinstance(snapshot, FieldSnapshot):
                        raise _InvalidRunResult(
                            "checker result contains an invalid evidence snapshot"
                        )
                    if not all(
                        isinstance(value, str)
                        for value in (snapshot.doc_id, snapshot.doc_type, snapshot.field)
                    ):
                        raise _InvalidRunResult(
                            "checker result contains an invalid evidence reference"
                        )
                    value_present = snapshot.raw not in (None, "")
                    frozen = frozen_fields.get((snapshot.doc_id, snapshot.field), {})
                    eligible = frozen.get("evidence_eligible") is True
                    evidence_links.append(
                        _RunEvidenceLink(
                            document_id=snapshot.doc_id,
                            document_role=snapshot.doc_type,
                            field=snapshot.field,
                            value_state="present" if value_present else "missing",
                            raw_masked="[REDACTED]" if value_present else None,
                            observation_id=frozen.get("observation_id"),
                            source_object_ref=frozen.get("source_object_ref"),
                            source_sha256=frozen.get("source_sha256"),
                            provenance_manifest_digest=frozen.get(
                                "provenance_manifest_digest"
                            ),
                            source_page=frozen.get("source_page"),
                            source_region=frozen.get("source_region"),
                            evidence_eligible=eligible,
                            eligibility_reason=str(
                                frozen.get("eligibility_reason")
                                if eligible
                                else "PROVENANCE_INELIGIBLE"
                            ),
                        )
                    )
                checks.append(
                    _RunCheckResult(
                        rule_id=check.rule_id,
                        verdict=check.verdict.value,
                        severity=check.severity.value,
                        reason_codes=tuple(check.reason_codes),
                        evidence_links=tuple(evidence_links),
                    )
                )

            actual_ids = tuple(check.rule_id for check in checks)
            expected_ids = tuple(run_spec["applicable_check_ids"])
            expected_count = run_spec["applicable_check_count"]
            if len(actual_ids) != expected_count:
                raise _InvalidRunResult("checker result cardinality does not match RunSpec")
            if len(set(actual_ids)) != len(actual_ids):
                raise _InvalidRunResult("checker result contains duplicate check IDs")
            if set(actual_ids) != set(expected_ids):
                raise _InvalidRunResult("checker result checks do not match RunSpec")
            return _RunResult(application_id=report.application_id, checks=tuple(checks))
        except _InvalidRunResult:
            raise
        except Exception as error:
            raise _InvalidRunResult("checker result conversion failed") from error

    def _publish_run_diagnostic(
        self,
        app: dict[str, Any],
        job: dict[str, Any],
        attempt: dict[str, Any],
        run_spec: dict[str, Any],
        stage: Callable[
            [dict[str, Any], dict[str, Any], dict[str, Any]], WorkerResult
        ],
    ) -> WorkerResult:
        attempt_template = copy.deepcopy(attempt)
        for _ in range(3):
            result = stage(app, job, attempt)
            try:
                self._store.persist()
            except StaleStoreRevision:
                self._reload_store()
                current_app = self._store.applications.get(app["application_id"])
                current_job = next(
                    (
                        item
                        for item in self._store.jobs
                        if item.get("job_id") == job.get("job_id")
                    ),
                    None,
                )
                current_attempt = next(
                    (
                        item
                        for item in self._store.attempts
                        if item.get("attempt_id") == attempt_template["attempt_id"]
                    ),
                    None,
                )
                if (
                    current_app is None
                    or current_job is None
                    or json.dumps(
                        current_attempt,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    != json.dumps(
                        attempt_template,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    or current_job.get("status") != "leased"
                    or current_job.get("worker_id") != attempt_template.get("worker_id")
                    or current_job.get("fence") != run_spec["fence"]
                ):
                    break
                app = current_app
                job = current_job
                attempt = current_attempt
                continue
            return result

        try:
            self.stop_new_cohort(
                reason_code=self._RUNTIME_STOP_REASON,
                failure_reason_code="S01_BACKGROUND_RUNTIME_EXCEPTION",
                principal=S01CommandPrincipal(
                    subject=attempt_template["worker_id"],
                    role="operator",
                    scope="C-DEMO",
                    source_id="s01-target-worker",
                ),
            )
        except StaleStoreRevision:
            pass
        return WorkerResult(
            status="stopped",
            application_id=app["application_id"],
            job_id=job["job_id"],
            attempt_id=attempt["attempt_id"],
            run_id=run_spec["run_id"],
            reason_code="S01_BACKGROUND_RUNTIME_EXCEPTION",
            lifecycle_revision=app["lifecycle_revision"],
            evidence_revision=app["evidence_revision"],
            lifecycle_phases=tuple(app["phase_history"]),
            release_id=run_spec["release_id"],
            release_digest=run_spec["release_digest"],
            checker_build=run_spec["checker_build"],
            fence=run_spec["fence"],
            evidence_snapshot_id=run_spec["evidence_snapshot_id"],
            evidence_snapshot_digest=run_spec["evidence_snapshot_digest"],
        )

    def _record_checker_failure(
        self,
        app: dict[str, Any],
        job: dict[str, Any],
        attempt: dict[str, Any],
        run_spec: dict[str, Any],
        *,
        now: int,
        reason_code: str = "CHECKER_EXCEPTION",
    ) -> WorkerResult:
        run_id = run_spec["run_id"]
        diagnostic_status = (
            "checker_failed" if reason_code == "CHECKER_EXCEPTION" else "invalid_result"
        )
        recovery_reason = (
            "CHECKER_FAILURE_RETRY"
            if reason_code == "CHECKER_EXCEPTION"
            else "INVALID_RESULT_RETRY"
        )

        def stage(
            current_app: dict[str, Any],
            current_job: dict[str, Any],
            current_attempt: dict[str, Any],
        ) -> WorkerResult:
            status, public_reason, retry_after = self._schedule_failure_retry(
                current_job,
                reason_code=reason_code,
                recovery_reason=recovery_reason,
                now=now,
            )
            self._store.runs.append(
                {
                    "run_record_id": self._stable_id(
                        "run_record",
                        f"{current_attempt['attempt_id']}:{diagnostic_status}",
                    ),
                    "run_id": run_id,
                    "attempt_id": current_attempt["attempt_id"],
                    "application_id": current_app["application_id"],
                    "spec": copy.deepcopy(run_spec),
                    "status": diagnostic_status,
                    "reason_code": reason_code,
                    "finding_ids": [],
                }
            )
            return WorkerResult(
                status=status,
                application_id=current_app["application_id"],
                job_id=current_job["job_id"],
                attempt_id=current_attempt["attempt_id"],
                run_id=run_id,
                reason_code=public_reason,
                lifecycle_revision=current_app["lifecycle_revision"],
                evidence_revision=current_app["evidence_revision"],
                lifecycle_phases=tuple(current_app["phase_history"]),
                release_id=run_spec["release_id"],
                release_digest=run_spec["release_digest"],
                checker_build=run_spec["checker_build"],
                fence=run_spec["fence"],
                evidence_snapshot_id=run_spec["evidence_snapshot_id"],
                evidence_snapshot_digest=run_spec["evidence_snapshot_digest"],
                retry_after_seconds=retry_after,
            )

        return self._publish_run_diagnostic(app, job, attempt, run_spec, stage)

    def _schedule_failure_retry(
        self,
        job: dict[str, Any],
        *,
        reason_code: str,
        recovery_reason: str,
        now: int,
        terminal: bool = False,
    ) -> tuple[str, str, int]:
        attempt_no = int(job.get("attempt_no", 0))
        if terminal or attempt_no >= self._MAX_FAILURE_ATTEMPTS:
            terminal_reason = f"{reason_code}_RETRY_EXHAUSTED"
            job["status"] = "diagnostic"
            job["terminal_reason_code"] = terminal_reason
            job.pop("retry_not_before", None)
            job.pop("recovery_reason", None)
            requested_stop = {
                "track": "C-DEMO",
                "admission": "stopped",
                "reason_code": self._RUNTIME_STOP_REASON,
                "failure_reason_code": terminal_reason,
            }
            if not self.audit_available:
                self._local_cohort_stop = copy.deepcopy(requested_stop)
                return "stopped", terminal_reason, 0
            current_stop = self._store.cohort_stop
            next_stop = self._runtime_stop_with_resume(requested_stop, current_stop)
            self._store.cohort_stop = next_stop
            if next_stop != current_stop:
                self._append_cohort_stop_audit(
                    self._store,
                    principal=S01CommandPrincipal(
                        subject=str(job.get("worker_id") or self._worker_identity),
                        role="worker",
                        scope=self._application_visibility_scope(
                            str(job["application_id"])
                        ),
                        source_id="s01-target-worker",
                    ),
                    reason_code=self._RUNTIME_STOP_REASON,
                    failure_reason_code=terminal_reason,
                    cohort_stop=next_stop,
                )
            return "stopped", terminal_reason, 0
        retry_after = 2 ** (attempt_no - 1)
        job["status"] = "queued"
        job["recovery_reason"] = recovery_reason
        job["retry_not_before"] = now + retry_after
        return "failed", reason_code, retry_after

    def _commit_complete_result(
        self,
        app: dict[str, Any],
        job: dict[str, Any],
        attempt: dict[str, Any],
        run_spec: dict[str, Any],
        run_result: _RunResult,
        completion_context: dict[str, Any],
        semantic_differential: dict[str, Any],
        *,
        now: int,
    ) -> WorkerResult:
        current_app = app
        current_job = job
        current_attempt = attempt
        for _ in range(self._MAX_COMPLETE_RESULT_ATTEMPTS):
            try:
                return self._commit_complete_result_once(
                    current_app,
                    current_job,
                    current_attempt,
                    run_spec,
                    run_result,
                    completion_context,
                    semantic_differential,
                    now=now,
                )
            except StaleStoreRevision:
                self._reload_store()
                current_app = self._store.applications[app["application_id"]]
                current_job = next(
                    item for item in self._store.jobs if item["job_id"] == job["job_id"]
                )
                current_attempt = next(
                    item
                    for item in self._store.attempts
                    if item["attempt_id"] == attempt["attempt_id"]
                )
        return self._record_result_publication_failure(
            current_app,
            current_job,
            current_attempt,
            run_spec,
            now=now,
            terminal=True,
        )

    def _commit_complete_result_once(
        self,
        app: dict[str, Any],
        job: dict[str, Any],
        attempt: dict[str, Any],
        run_spec: dict[str, Any],
        run_result: _RunResult,
        completion_context: dict[str, Any],
        semantic_differential: dict[str, Any],
        *,
        now: int,
    ) -> WorkerResult:
        run_id = run_spec["run_id"]
        if not self.audit_available:
            return self._record_result_publication_failure(
                app, job, attempt, run_spec, now=now
            )
        result_visibility_scope = self._application_visibility_scope(
            app["application_id"]
        )
        expected_context = {
            "cycle": app["cycle"],
            "lifecycle_revision": app["lifecycle_revision"],
            "evidence_revision": app["evidence_revision"],
            "release_id": self._release["release_id"],
            "release_digest": self._release["digest"],
            "checker_build": self._release["checker_build"],
            "fence": job["fence"],
        }
        mismatches = tuple(
            field
            for field in self._CAS_CONTEXT_FIELDS
            if completion_context.get(field) != expected_context[field]
        )
        if mismatches:
            return self._record_stale_complete_result(
                app,
                job,
                attempt,
                run_spec,
                completion_context,
                mismatches,
            )

        findings = []
        for check in run_result.checks:
            mandatory = check.severity in {"critical", "major"}
            finding_id = self._stable_id("finding", f"{run_id}:{check.rule_id}")
            reason = (check.reason_codes or [self._reason_for_rule(check.rule_id)])[0]
            findings.append(
                {
                    "finding_id": finding_id,
                    "application_id": app["application_id"],
                    "run_id": run_id,
                    "rule_id": check.rule_id,
                    "verdict": check.verdict,
                    "severity": check.severity,
                    "reason_code": reason,
                    "mandatory": mandatory,
                    "evidence_links": [
                        {
                            "document_id": link.document_id,
                            "document_role": link.document_role,
                            "field": link.field,
                            "observation_id": link.observation_id,
                            "source_object_ref": link.source_object_ref,
                            "source_sha256": link.source_sha256,
                            "provenance_manifest_digest": (
                                link.provenance_manifest_digest
                            ),
                            "source_page": link.source_page,
                            "source_region": link.source_region,
                            "evidence_eligible": link.evidence_eligible,
                            "eligibility_reason": link.eligibility_reason,
                            "value_state": link.value_state,
                            "raw_masked": link.raw_masked,
                        }
                        for link in check.evidence_links
                    ],
                }
            )
        has_mandatory_blocker = any(
            check.severity in {"critical", "major"}
            and check.verdict != "consistent"
            for check in run_result.checks
        )
        review_assignee = (
            self._application_review_assignee(app["application_id"])
            if has_mandatory_blocker
            else None
        )
        claim_started_at = int(self._clock())
        staged = copy.deepcopy(self._store)
        staged_app = staged.applications[app["application_id"]]
        staged_job = next(item for item in staged.jobs if item["job_id"] == job["job_id"])
        staged_attempt = next(
            item for item in staged.attempts if item["attempt_id"] == attempt["attempt_id"]
        )
        try:
            self._before_write("result.findings")
            staged.findings.extend(findings)
            self._before_write("result.run")
            staged.runs.append(
                {
                    "run_record_id": self._stable_id(
                        "run_record", f"{staged_attempt['attempt_id']}:complete"
                    ),
                    "run_id": run_id,
                    "attempt_id": staged_attempt["attempt_id"],
                    "application_id": staged_app["application_id"],
                    "spec": copy.deepcopy(run_spec),
                    "completion_context": copy.deepcopy(completion_context),
                    "status": "complete",
                    "finding_ids": [f["finding_id"] for f in findings],
                    "normalization_outcomes": [
                        asdict(outcome) for outcome in run_result.normalization_outcomes
                    ],
                    "selection_outcomes": [
                        asdict(outcome) for outcome in run_result.selection_outcomes
                    ],
                    "semantic_differential": copy.deepcopy(semantic_differential),
                }
            )
            self._before_write("result.attempt")
            self._before_write("result.job")
            staged_job["status"] = "complete"
            self._before_write("result.current_run")
            staged_app["current_run_id"] = run_id
            staged_app["current_evidence_snapshot_id"] = run_spec[
                "evidence_snapshot_id"
            ]
            staged_app["current_evidence_snapshot_digest"] = run_spec[
                "evidence_snapshot_digest"
            ]
            self._before_write("result.lifecycle.routing")
            self._transition_lifecycle(
                staged_app,
                "Routing Determination",
                "RUN_RESULT_ACCEPTED",
                store=staged,
            )
            self._before_write("result.route")
            staged_app["route"] = "manual_review" if has_mandatory_blocker else "auto_complete"
            self._before_write("result.lifecycle.final")
            self._transition_lifecycle(
                staged_app,
                "Manual Review" if has_mandatory_blocker else "Verification Completed",
                "MANDATORY_BLOCKER_FOUND"
                if has_mandatory_blocker
                else "ALL_MANDATORY_CHECKS_PASSED",
                store=staged,
            )
            self._before_write("result.lifecycle.run_link")
            staged.lifecycle_events[-1]["run_id"] = run_id
            work_item_id = None
            if has_mandatory_blocker:
                work_item_id = self._stable_id(
                    "work", f"{staged_app['application_id']}:{staged_app['cycle']}:{run_id}"
                )
                self._before_write("result.review_work")
                staged.work_items.append(
                    {
                        "work_item_id": work_item_id,
                        "owner": "Lifecycle",
                        "kind": "manual_review",
                        "status": "active",
                        "application_id": staged_app["application_id"],
                        "cycle": staged_app["cycle"],
                        "run_id": run_id,
                        "lifecycle_revision": staged_app["lifecycle_revision"],
                        "evidence_revision": staged_app["evidence_revision"],
                        "evidence_snapshot_id": run_spec["evidence_snapshot_id"],
                        "release_id": run_spec["release_id"],
                        "finding_ids": [
                            finding["finding_id"]
                            for finding in findings
                            if finding["mandatory"]
                            and finding["verdict"] != "consistent"
                        ],
                        "visibility_scope": result_visibility_scope,
                        "assigned_subject": review_assignee,
                        "claim_subject": review_assignee,
                        "claim_fence": 1,
                        "claim_started_at": claim_started_at,
                        "claim_expires_at": claim_started_at
                        + self._REVIEW_CLAIM_TTL_SECONDS,
                    }
                )
            self._before_write("result.audit_event")
            staged.audit_events.append(
                {
                    "event_id": self._stable_id(
                        "audit", f"controlled_run_result:{run_id}"
                    ),
                    "action": "controlled_run_result",
                    "subject": staged_job["worker_id"],
                    "role": "worker",
                    "scope": result_visibility_scope,
                    "source_id": "s01-target-worker",
                    "application_id": staged_app["application_id"],
                    "job_id": staged_job["job_id"],
                    "attempt_id": staged_attempt["attempt_id"],
                    "run_id": run_id,
                    "result": "published",
                    "route": staged_app["route"],
                    "lifecycle_revision": staged_app["lifecycle_revision"],
                    "evidence_revision": staged_app["evidence_revision"],
                    "evidence_snapshot_id": run_spec["evidence_snapshot_id"],
                    "evidence_snapshot_digest": run_spec[
                        "evidence_snapshot_digest"
                    ],
                    "release_id": run_spec["release_id"],
                    "release_digest": run_spec["release_digest"],
                    "checker_build": run_spec["checker_build"],
                    "fence": run_spec["fence"],
                    "finding_count": len(findings),
                    "mandatory_blocker_count": sum(
                        finding["mandatory"] and finding["verdict"] != "consistent"
                        for finding in findings
                    ),
                    "work_item_id": work_item_id,
                    "assigned_subject": review_assignee,
                    **self._audit_time_fields(staged),
                }
            )
            self._before_write("result.projection")
            staged_app["projection_pending"] = True
            staged_app["projection_visible"] = False
            self._before_write("result.projection_outbox")
            staged.outbox.append(
                {
                    "event_id": self._stable_id("outbox", f"projection:{run_id}"),
                    "kind": "review_projection_requested",
                    "application_id": staged_app["application_id"],
                    "run_id": run_id,
                    "visibility_scope": result_visibility_scope,
                    "projection_watermark": 1
                    + sum(
                        event.get("kind") == "review_projection_requested"
                        and event.get("visibility_scope", "C-DEMO")
                        == result_visibility_scope
                        for event in staged.outbox
                    ),
                    "status": "pending",
                }
            )
            self._before_write("result.publish")
        except _StoreWriteFailure:
            return self._record_result_publication_failure(
                app, job, attempt, run_spec, now=now
            )
        try:
            staged.persist()
        except StaleStoreRevision:
            raise
        except Exception:
            return self._record_result_publication_failure(
                app, job, attempt, run_spec, now=now
            )
        self._store = staged
        return WorkerResult(
            status="complete",
            application_id=staged_app["application_id"],
            job_id=staged_job["job_id"],
            attempt_id=staged_attempt["attempt_id"],
            run_id=run_id,
            lifecycle_revision=staged_app["lifecycle_revision"],
            evidence_revision=staged_app["evidence_revision"],
            projection_pending=True,
            lifecycle_phases=tuple(staged_app["phase_history"]),
            release_id=run_spec["release_id"],
            release_digest=run_spec["release_digest"],
            checker_build=run_spec["checker_build"],
            fence=run_spec["fence"],
            evidence_snapshot_id=run_spec["evidence_snapshot_id"],
            evidence_snapshot_digest=run_spec["evidence_snapshot_digest"],
            semantic_differential=semantic_differential,
        )

    def _record_result_publication_failure(
        self,
        app: dict[str, Any],
        job: dict[str, Any],
        attempt: dict[str, Any],
        run_spec: dict[str, Any],
        *,
        now: int,
        terminal: bool = False,
    ) -> WorkerResult:
        run_id = run_spec["run_id"]

        def stage(
            current_app: dict[str, Any],
            current_job: dict[str, Any],
            current_attempt: dict[str, Any],
        ) -> WorkerResult:
            status, public_reason, retry_after = self._schedule_failure_retry(
                current_job,
                reason_code="RESULT_PUBLICATION_FAILED",
                recovery_reason="RESULT_PUBLICATION_RETRY",
                now=now,
                terminal=terminal,
            )
            self._store.runs.append(
                {
                    "run_record_id": self._stable_id(
                        "run_record",
                        f"{current_attempt['attempt_id']}:publication_failed",
                    ),
                    "run_id": run_id,
                    "attempt_id": current_attempt["attempt_id"],
                    "application_id": current_app["application_id"],
                    "spec": copy.deepcopy(run_spec),
                    "status": "publication_failed",
                    "reason_code": "RESULT_PUBLICATION_FAILED",
                    "finding_ids": [],
                }
            )
            return WorkerResult(
                status=status,
                application_id=current_app["application_id"],
                job_id=current_job["job_id"],
                attempt_id=current_attempt["attempt_id"],
                run_id=run_id,
                reason_code=public_reason,
                lifecycle_revision=current_app["lifecycle_revision"],
                evidence_revision=current_app["evidence_revision"],
                lifecycle_phases=tuple(current_app["phase_history"]),
                release_id=run_spec["release_id"],
                release_digest=run_spec["release_digest"],
                checker_build=run_spec["checker_build"],
                fence=run_spec["fence"],
                evidence_snapshot_id=run_spec["evidence_snapshot_id"],
                evidence_snapshot_digest=run_spec["evidence_snapshot_digest"],
                retry_after_seconds=retry_after,
            )

        return self._publish_run_diagnostic(app, job, attempt, run_spec, stage)

    @staticmethod
    def _check_signature(
        checks: list[Any],
    ) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
        return tuple(
            (
                check.rule_id,
                check.verdict.value
                if isinstance(check.verdict, Verdict)
                else str(check.verdict),
                tuple(check.reason_codes),
            )
            for check in checks
        )

    def _semantic_differential(
        self, app: dict[str, Any], run_result: _RunResult
    ) -> dict[str, Any]:
        oracle = tuple(
            (str(item[0]), str(item[1]), tuple(item[2]))
            for item in (app.get("legacy_oracle_outcomes") or ())
        )
        target = tuple(
            (check.rule_id, check.verdict, check.reason_codes)
            for check in run_result.checks
        )
        oracle_by_id = {item[0]: item for item in oracle}
        target_by_id = {item[0]: item for item in target}
        rule_ids = list(dict.fromkeys([item[0] for item in oracle] + [item[0] for item in target]))
        mismatches = []
        for rule_id in rule_ids:
            if oracle_by_id.get(rule_id) == target_by_id.get(rule_id):
                continue
            mismatches.append(
                {
                    "rule_id": rule_id,
                    "oracle": oracle_by_id.get(rule_id),
                    "target": target_by_id.get(rule_id),
                }
            )
        return {
            "oracle": "legacy-rule-engine",
            "scope": "one-c-demo-fixture",
            "checks_compared": len(rule_ids),
            "mismatches": mismatches,
            "status": "match" if not mismatches else "mismatch",
        }

    @classmethod
    def _completion_context(cls, run_spec: dict[str, Any]) -> dict[str, Any]:
        return {field: run_spec[field] for field in cls._CAS_CONTEXT_FIELDS}

    @staticmethod
    def _with_cas_fault(context: dict[str, Any], field: str) -> dict[str, Any]:
        changed = copy.deepcopy(context)
        value = changed[field]
        changed[field] = value + 1 if isinstance(value, int) else f"{value}:stale"
        return changed

    def _record_stale_complete_result(
        self,
        app: dict[str, Any],
        job: dict[str, Any],
        attempt: dict[str, Any],
        run_spec: dict[str, Any],
        completion_context: dict[str, Any],
        mismatches: tuple[str, ...],
    ) -> WorkerResult:
        run_id = run_spec["run_id"]

        def stage(
            current_app: dict[str, Any],
            current_job: dict[str, Any],
            current_attempt: dict[str, Any],
        ) -> WorkerResult:
            self._store.runs.append(
                {
                    "run_record_id": self._stable_id(
                        "run_record", f"{current_attempt['attempt_id']}:stale"
                    ),
                    "run_id": run_id,
                    "attempt_id": current_attempt["attempt_id"],
                    "application_id": current_app["application_id"],
                    "spec": copy.deepcopy(run_spec),
                    "completion_context": copy.deepcopy(completion_context),
                    "status": "stale",
                    "cas_mismatches": mismatches,
                    "finding_ids": [],
                }
            )
            superseded = (
                int(current_job.get("fence", 0)) > int(run_spec["fence"])
                or current_job.get("status") in {"complete", "diagnostic"}
            )
            if not superseded:
                current_job["status"] = "queued"
                self._prepare_retry(current_app, "STALE_RUN_RECORDED")
            return WorkerResult(
                status="stale",
                application_id=current_app["application_id"],
                job_id=current_job["job_id"],
                attempt_id=current_attempt["attempt_id"],
                run_id=run_id,
                reason_code="STALE_COMPARE_AND_SET",
                lifecycle_revision=current_app["lifecycle_revision"],
                evidence_revision=current_app["evidence_revision"],
                lifecycle_phases=tuple(current_app["phase_history"]),
                cas_mismatches=mismatches,
                release_id=run_spec["release_id"],
                release_digest=run_spec["release_digest"],
                checker_build=run_spec["checker_build"],
                fence=run_spec["fence"],
                evidence_snapshot_id=run_spec["evidence_snapshot_id"],
                evidence_snapshot_digest=run_spec["evidence_snapshot_digest"],
            )

        return self._publish_run_diagnostic(app, job, attempt, run_spec, stage)

    def _prepare_retry(self, app: dict[str, Any], reason_code: str) -> None:
        app["evidence_ready"] = False
        app["route"] = "pending_check"
        app["projection_visible"] = False
        app["projection_pending"] = False
        self._transition_lifecycle(app, "Assembly", reason_code)

    def _transition_lifecycle(
        self,
        app: dict[str, Any],
        phase: str,
        reason_code: str,
        *,
        store: _TargetStore | None = None,
    ) -> None:
        current = app["phase"]
        if phase not in self._ALLOWED_PHASE_SUCCESSORS.get(current, frozenset()):
            raise ValueError(f"illegal S01 lifecycle transition: {current} -> {phase}")
        app["phase"] = phase
        app["phase_history"].append(phase)
        app["lifecycle_revision"] += 1
        owner = store or self._store
        owner.lifecycle_events.append(
            {
                "event_id": self._stable_id(
                    "lifecycle",
                    f"{app['application_id']}:{app['cycle']}:{app['lifecycle_revision']}",
                ),
                "application_id": app["application_id"],
                "revision": app["lifecycle_revision"],
                "phase": phase,
                "cycle": app["cycle"],
                "reason_code": reason_code,
            }
        )

    def _record_duplicate_result(self, worker_id: str) -> WorkerResult:
        """Keep an at-least-once duplicate result as a non-current diagnostic."""
        job = next((item for item in self._store.jobs if item["status"] == "complete"), None)
        if job is None:
            return WorkerResult(status="idle", reason_code="NO_COMPLETED_JOB")
        app = self._store.applications[job["application_id"]]
        sequence = 1 + sum(
            1
            for attempt in self._store.attempts
            if attempt["job_id"] == job["job_id"] and attempt["status"] == "duplicate"
        )
        attempt_id = self._stable_id(
            "attempt", f"{job['job_id']}:duplicate:{sequence}:{worker_id}"
        )
        self._store.attempts.append(
            {
                "attempt_id": attempt_id,
                "job_id": job["job_id"],
                "application_id": job["application_id"],
                "worker_id": worker_id,
                "fence": job.get("fence", 0),
                "status": "duplicate",
                "result_run_id": app["current_run_id"],
            }
        )
        self._store.persist()
        return WorkerResult(
            status="duplicate",
            application_id=job["application_id"],
            job_id=job["job_id"],
            attempt_id=attempt_id,
            run_id=app["current_run_id"],
            reason_code="DUPLICATE_RESULT",
            lifecycle_revision=app["lifecycle_revision"],
            evidence_revision=app["evidence_revision"],
            projection_pending=bool(app.get("projection_pending")),
        )

    @staticmethod
    def _reason_for_rule(rule_id: str) -> str:
        return {
            "R_VIN_CROSS": "VIN_MISMATCH",
            "R_ENGINE_CROSS": "ENGINE_MISMATCH",
            "R_ID_EXACT": "ID_MISMATCH",
            "R_NAME_FUZZY": "NAME_NEAR_UNCERTAIN",
        }.get(rule_id, "MANDATORY_CHECK_FINDING")

    @staticmethod
    def _finding_projection(finding: dict[str, Any]) -> dict[str, Any]:
        return {
            "finding_id": finding["finding_id"],
            "run_id": finding["run_id"],
            "rule_id": finding["rule_id"],
            "verdict": finding["verdict"],
            "severity": finding["severity"],
            "reason_code": finding["reason_code"],
            "mandatory": finding["mandatory"],
            "evidence_links": copy.deepcopy(finding["evidence_links"]),
        }

    @staticmethod
    def _admission_from_payload(payload: Any) -> AdmissionResult:
        if isinstance(payload, AdmissionResult):
            return payload
        values = dict(payload)
        values.pop("submission_scope", None)
        values.pop("batch_facts", None)
        values.pop("envelope", None)
        values["disposition"] = AdmissionDisposition(values["disposition"])
        return AdmissionResult(**values)

    def _hydrate_admission_results(self) -> None:
        self._store.receipts = {
            key: self._admission_from_payload(result)
            for key, result in self._store.receipts.items()
        }
        self._store.idempotency = {
            key: (fingerprint, self._admission_from_payload(result))
            for key, (fingerprint, result) in self._store.idempotency.items()
        }

    def _reconcile_admission_jobs(self) -> None:
        """Restore or stop for an admitted request whose mutable job is absent."""
        for _ in range(3):
            repair_jobs: list[dict[str, Any]] = []
            recovery_failure = False
            request_events = [
                event
                for event in self._store.outbox
                if event.get("kind") == "controlled_check_requested"
            ]
            events_by_job: dict[str, dict[str, Any]] = {}
            for event in request_events:
                job_id = event.get("job_id")
                if not isinstance(job_id, str) or not job_id:
                    recovery_failure = True
                    break
                previous = events_by_job.get(job_id)
                if previous is not None and previous != event:
                    recovery_failure = True
                    break
                events_by_job[job_id] = event

            accepted_receipts = [
                receipt
                for receipt in self._store.receipts.values()
                if isinstance(receipt, AdmissionResult)
                and receipt.disposition is AdmissionDisposition.ACCEPTED
            ]
            accepted_applications = {
                str(receipt.application_id): receipt
                for receipt in accepted_receipts
                if isinstance(receipt.application_id, str)
            }
            if len(accepted_applications) != len(accepted_receipts):
                recovery_failure = True

            if not recovery_failure:
                for application_id, receipt in accepted_applications.items():
                    job_id = receipt.job_id
                    event = (
                        events_by_job.get(job_id)
                        if isinstance(job_id, str)
                        else None
                    )
                    app = self._store.applications.get(application_id)
                    if event is None or not self._admission_job_authority_matches(
                        app, receipt, event
                    ):
                        recovery_failure = True
                        break
                    jobs = [
                        job
                        for job in self._store.jobs
                        if job.get("job_id") == job_id
                    ]
                    if len(jobs) > 1:
                        recovery_failure = True
                        break
                    unprocessed = self._admission_is_unprocessed(app, application_id)
                    if jobs:
                        job = jobs[0]
                        if not self._job_matches_admission(job, app, receipt):
                            recovery_failure = True
                            break
                        canonical_job = self._admission_job_record(
                            job_id,
                            application_id,
                            receipt.envelope_fingerprint,
                        )
                        if (
                            unprocessed
                            and job != canonical_job
                            and not self._pristine_leased_job(job)
                        ):
                            repair_jobs.append(
                                canonical_job
                            )
                        continue
                    if not unprocessed:
                        recovery_failure = True
                        break
                    repair_jobs.append(
                        self._admission_job_record(
                            job_id,
                            application_id,
                            receipt.envelope_fingerprint,
                        )
                    )

            if not recovery_failure:
                known_application_ids = set(accepted_applications)
                for event in request_events:
                    application_id = event.get("application_id")
                    if (
                        not isinstance(application_id, str)
                        or application_id not in known_application_ids
                    ):
                        recovery_failure = True
                        break

            if not recovery_failure and not repair_jobs:
                return

            staged = copy.deepcopy(self._store)
            if recovery_failure:
                requested_stop = {
                    "track": "C-DEMO",
                    "admission": "stopped",
                    "reason_code": self._RUNTIME_STOP_REASON,
                    "failure_reason_code": self._ADMISSION_JOB_RECOVERY_FAILURE,
                }
                if not self.audit_available:
                    self._local_cohort_stop = copy.deepcopy(requested_stop)
                    return
                current_stop = staged.cohort_stop
                if current_stop is None:
                    staged.cohort_stop = requested_stop
                elif current_stop.get("reason_code") != self._RUNTIME_STOP_REASON:
                    staged.cohort_stop = self._runtime_stop_with_resume(
                        requested_stop, current_stop
                    )
                elif (
                    current_stop.get("failure_reason_code")
                    != self._ADMISSION_JOB_RECOVERY_FAILURE
                ):
                    staged.cohort_stop = requested_stop
                if staged.cohort_stop != current_stop:
                    self._append_cohort_stop_audit(
                        staged,
                        principal=S01CommandPrincipal(
                            subject="s01-admission-recovery",
                            role="system",
                            scope="C-DEMO",
                            source_id="s01-target-startup",
                        ),
                        reason_code=self._RUNTIME_STOP_REASON,
                        failure_reason_code=self._ADMISSION_JOB_RECOVERY_FAILURE,
                        cohort_stop=staged.cohort_stop,
                    )
            else:
                repaired_ids = {job["job_id"] for job in repair_jobs}
                staged.jobs = [
                    job for job in staged.jobs if job.get("job_id") not in repaired_ids
                ]
                staged.jobs.extend(repair_jobs)
            if staged.cohort_stop == self._store.cohort_stop and not repair_jobs:
                return
            try:
                staged.persist()
            except StaleStoreRevision:
                self._store.reload()
                self._hydrate_admission_results()
                self._restore_cohort_stop_authority()
                continue
            self._store = staged
            return
        raise StaleStoreRevision("could not reconcile S01 admission jobs")

    @classmethod
    def _admission_job_authority_matches(
        cls,
        app: dict[str, Any] | None,
        receipt: AdmissionResult,
        event: dict[str, Any],
    ) -> bool:
        if not isinstance(app, dict):
            return False
        if event.get("status") != "pending":
            return False
        if event.get("application_id") != receipt.application_id:
            return False
        if event.get("job_id") != receipt.job_id:
            return False
        if receipt.application_id != app.get("application_id"):
            return False
        envelope = app.get("envelope")
        fingerprint = (
            envelope.get("fingerprint")
            if isinstance(envelope, dict)
            else None
        )
        if not isinstance(fingerprint, str) or not fingerprint:
            return False
        if receipt.envelope_fingerprint != fingerprint:
            return False
        event_fingerprint = event.get("fingerprint")
        return event_fingerprint in (None, fingerprint)

    @staticmethod
    def _job_matches_admission(
        job: dict[str, Any], app: dict[str, Any] | None, receipt: AdmissionResult
    ) -> bool:
        return (
            isinstance(app, dict)
            and job.get("application_id") == app.get("application_id")
            and job.get("kind") == "controlled_check"
            and job.get("fingerprint") == receipt.envelope_fingerprint
        )

    @staticmethod
    def _admission_job_record(
        job_id: str, application_id: str, fingerprint: str
    ) -> dict[str, Any]:
        return {
            "job_id": job_id,
            "application_id": application_id,
            "kind": "controlled_check",
            "status": "queued",
            "fingerprint": fingerprint,
        }

    @staticmethod
    def _pristine_leased_job(job: dict[str, Any]) -> bool:
        attempt_no = job.get("attempt_no", 0)
        fence = job.get("fence", 0)
        return (
            job.get("status") == "leased"
            and attempt_no == 1
            and fence == 1
            and isinstance(job.get("worker_id"), str)
            and bool(job.get("worker_id"))
            and isinstance(job.get("lease_until"), int)
            and not isinstance(job.get("lease_until"), bool)
        )

    def _admission_is_unprocessed(
        self, app: dict[str, Any] | None, application_id: str
    ) -> bool:
        if not isinstance(app, dict):
            return False
        if (
            app.get("application_id") != application_id
            or app.get("phase") != "Intake"
            or app.get("cycle") != 1
            or app.get("lifecycle_revision") != 1
            or app.get("evidence_revision") != 1
            or app.get("phase_history") != ["Intake"]
            or app.get("evidence_ready") is not False
            or app.get("route") != "pending_check"
            or app.get("current_run_id") is not None
            or app.get("projection_pending") is not False
            or app.get("projection_visible") is not False
        ):
            return False
        lifecycle = [
            event
            for event in self._store.lifecycle_events
            if event.get("application_id") == application_id
        ]
        if len(lifecycle) != 1 or lifecycle[0].get("revision") != 1:
            return False
        admitted = [
            event
            for event in self._store.evidence_events
            if event.get("application_id") == application_id
            and event.get("kind") == "admitted_snapshot"
        ]
        if len(admitted) != 1 or admitted[0].get("revision") != 1:
            return False
        return not any(
            record.get("application_id") == application_id
            for record in (*self._store.attempts, *self._store.runs, *self._store.findings)
        )

    def _reload_store(self) -> None:
        self._store.reload()
        self._hydrate_admission_results()
        self._restore_cohort_stop_authority()
        self._reconcile_admission_jobs()

    def _restore_cohort_stop_authority(self) -> None:
        cohort_stop: dict[str, Any] | None = None
        for event in self._store.audit_events:
            action = event.get("action")
            if action not in {"controlled_cohort_stop", "runtime_recovery"}:
                continue
            authority = event.get("cohort_stop_authority")
            if authority is not None and not isinstance(authority, dict):
                raise RuntimeError("cohort stop authority is invalid")
            public_state = (
                self._public_cohort_stop(authority)
                if authority is not None
                else {"track": "C-DEMO", "admission": "open"}
            )
            expected = event.get(
                "admission_after_stop"
                if action == "controlled_cohort_stop"
                else "admission_after_recovery"
            )
            if expected != public_state:
                raise RuntimeError("cohort stop audit projection is invalid")
            cohort_stop = copy.deepcopy(authority)
        self._store.cohort_stop = cohort_stop


class ControlledScenarioTestDriver:
    """Test-only deterministic driver for worker failure and lease scenarios."""

    __test__ = False

    def __init__(self, service: ControlledScenarioService) -> None:
        self._service = service

    def process_next_job(
        self,
        *,
        worker_id: str = "s01-test-worker",
        now: int = 0,
        crash: bool = False,
        partial: bool = False,
        stale: bool = False,
        cas_fault: str | None = None,
        duplicate: bool = False,
    ) -> WorkerResult:
        return self._service._process_next_job(
            worker_id=worker_id,
            now=now,
            crash=crash,
            partial=partial,
            stale=stale,
            cas_fault=cas_fault,
            duplicate=duplicate,
        )
