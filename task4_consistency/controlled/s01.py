"""S01 controlled-scenario admission and processing-cycle authority.

This is deliberately a small in-process target seam.  It owns target facts for
the walking skeleton; legacy JSON remains a read-only adapter input.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field, replace
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
from .s02 import (
    ControlledObject,
    RegisteredSource,
    RegisteredSourceBoundary,
    S02CanonicalEnvelope,
    S02IntakeError,
    is_registered_scope,
    tenant_from_scope,
)
from .s08 import S08_SCOPE


class AdmissionDisposition(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    QUARANTINED = "quarantined"
    AWAITING_PREDECESSOR = "awaiting_predecessor"


@dataclass(frozen=True)
class AdmissionResult:
    disposition: AdmissionDisposition
    application_id: str | None = None
    receipt_id: str | None = None
    job_id: str | None = None
    reason_code: str | None = None
    replayed: bool = False
    lifecycle_revision: int | None = 0
    evidence_revision: int | None = 0
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
    retryable: bool = False
    responsible_party: str | None = None
    recovery_action: str | None = None
    related_reasons: tuple[str, ...] = ()
    fact_counts: dict[str, int] = field(default_factory=dict)
    gate_results: tuple[str, ...] = ()
    real_cross_document_opportunities: int | None = None
    performance_status: str | None = None
    source_registration_digest: str | None = None
    source_revision: int | None = None
    request_id: str | None = None
    request_status: str | None = None
    batch_closed: bool | None = None
    request_progress_revision: int | None = None
    attachment_id: str | None = None
    attachment_version: int | None = None
    supersedes_attachment_id: str | None = None
    fulfilled: bool | None = None
    phase: str | None = None
    route: str | None = None
    recovery_target: dict[str, Any] | None = None

    @property
    def claim_label(self) -> str | None:
        if self.performance_status == "not_estimable":
            return "R-OBSERVED"
        return None


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
    recovery_work_id: str | None = None
    reconciliation: dict[str, Any] | None = None


class QueryNotFound(LookupError):
    """Existence-hiding query result for unauthorized/cross-scope reads."""


class _StoreWriteFailure(RuntimeError):
    """One staged in-memory store write failed before owner publication."""


class _InvalidRunResult(ValueError):
    """Checker output could not become one complete typed target result."""


class _PinnedReleaseUnavailable(RuntimeError):
    """The release fixed at admission cannot be resolved by this worker."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    expires_at: float | None = None


@dataclass(frozen=True)
class S01ArtifactManifest:
    scenario_id: str
    source_sha256: str
    source_provenance_manifest_version: str
    source_provenance_manifest_digest: str
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
    _ALLOWED_SCENARIOS = frozenset(
        {
            "app_r53_bad_engine.json",
            "app_s04_bad_vin.json",
            "app_bad_brand.json",
            "app_bad_model.json",
            "app_uncertain_ocr_noise.json",
            "app_inconsistent_vin.json",
            "app_missing_vin_docs.json",
            "app_s10_ambiguous_membership.json",
            "app_s10_membership_field.json",
        }
    )
    _ALLOWED_PHASE_SUCCESSORS = {
        "Intake": frozenset({"Assembly"}),
        "Assembly": frozenset({"Awaiting Evidence", "Evidence Ready", "Unprocessable"}),
        "Evidence Ready": frozenset({"Checking"}),
        "Checking": frozenset(
            {"Routing Determination", "Assembly", "Unprocessable"}
        ),
        "Routing Determination": frozenset(
            {"Manual Review", "Verification Completed", "Unprocessable"}
        ),
        "Manual Review": frozenset(
            {
                "Manual Review",
                "Verification Completed",
                "Assembly",
                "Pending Exception Approval",
                "Supplement",
            }
        ),
        "Supplement": frozenset({"Assembly", "Unprocessable"}),
        "Awaiting Evidence": frozenset({"Assembly", "Unprocessable"}),
        "Unprocessable": frozenset(
            {"Assembly", "Evidence Ready", "Routing Determination"}
        ),
        "Pending Exception Approval": frozenset(
            {"Routing Determination", "Manual Review", "Assembly"}
        ),
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
    _S07_RETRY_POLICY_ID = "s07-c-demo-retry/1"
    _S07_FAILURES = {
        "checker_transient": {
            "classification": "transient",
            "primary_reason_code": "check.failed",
            "related_reason_codes": (),
            "operation": "execute_check_run",
            "dependency": "c-demo-target-checker",
            "responsible_party": "runtime_operations_owner",
            "recovery_action": "repair_dependency_and_verify_fixed_execution_probe",
            "recovery_target": "Evidence Ready",
            "criterion_id": "s07-fixed-execution-probe/1",
            "evidence_kind": "fixed_execution_probe",
        },
        "checker_incompatible": {
            "classification": "terminal",
            "primary_reason_code": "configuration.checker_unavailable",
            "related_reason_codes": (),
            "operation": "execute_check_run",
            "dependency": "c-demo-target-checker",
            "responsible_party": "policy_owner",
            "recovery_action": (
                "restore_exact_release_or_activate_compatible_successor"
            ),
            "recovery_target": "Evidence Ready",
            "criterion_id": "s07-checker-compatibility/1",
            "evidence_kind": "checker_compatibility_probe",
        },
        "checker_dead_lettered": {
            "classification": "terminal",
            "primary_reason_code": "check.failed",
            "related_reason_codes": ("operation.dead_lettered",),
            "operation": "execute_check_run",
            "dependency": "c-demo-target-checker",
            "responsible_party": "runtime_operations_owner",
            "recovery_action": "repair_dependency_and_verify_fixed_execution_probe",
            "recovery_target": "Evidence Ready",
            "criterion_id": "s07-fixed-execution-probe/1",
            "evidence_kind": "fixed_execution_probe",
            "job_status": "dead_lettered",
        },
        "compensation_failed": {
            "classification": "terminal",
            "primary_reason_code": "operation.compensation_failed",
            "related_reason_codes": ("check.outcome_unknown",),
            "operation": "compensate_check_run",
            "dependency": "c-demo-target-checker",
            "responsible_party": "integration_owner",
            "recovery_action": "complete_and_verify_compensation",
            "recovery_target": "Evidence Ready",
            "criterion_id": "s07-compensation-receipt/1",
            "evidence_kind": "compensation_receipt",
            "outcome_known": False,
            "job_status": "compensation_failed",
            "conditions": (
                {
                    "condition_id": "s07-exact-operation-reconciled/1",
                    "reason_code": "check.outcome_unknown",
                },
                {
                    "condition_id": "s07-compensation-receipt/1",
                    "reason_code": "operation.compensation_failed",
                },
            ),
        },
        "checker_outcome_unknown": {
            "classification": "terminal",
            "primary_reason_code": "check.outcome_unknown",
            "related_reason_codes": ("operation.status_unavailable",),
            "operation": "execute_check_run",
            "dependency": "c-demo-target-checker",
            "responsible_party": "integration_owner",
            "recovery_action": "reconcile_exact_logical_operation",
            "recovery_target": "Evidence Ready",
            "criterion_id": "s07-operation-status-reconciliation/1",
            "evidence_kind": "operation_status_receipt",
            "outcome_known": False,
            "job_status": "outcome_unknown",
        },
        "result_publication_audit": {
            "classification": "terminal",
            "primary_reason_code": "control.audit_unavailable",
            "related_reason_codes": (),
            "operation": "publish_check_result",
            "dependency": "security-audit-ledger",
            "responsible_party": "security_audit_owner",
            "recovery_action": "restore_and_reconcile_audit_ledger",
            "recovery_target": "Evidence Ready",
            "criterion_id": "s07-audit-ledger-reconciliation/1",
            "evidence_kind": "audit_ledger_reconciliation",
        },
        "object_storage_unavailable": {
            "classification": "terminal",
            "primary_reason_code": "control.storage_unavailable",
            "related_reason_codes": (),
            "operation": "read_evidence_object",
            "dependency": "c-demo-object-store",
            "responsible_party": "platform_storage_owner",
            "recovery_action": "restore_and_verify_storage_binding",
            "recovery_target": "Assembly",
            "criterion_id": "s07-object-storage-binding/1",
            "evidence_kind": "object_storage_binding_probe",
        },
    }
    _S07_ROUTING_FAILURE = {
        "primary_reason_code": "routing.dependency_unavailable",
        "related_reason_codes": (),
        "operation": "determine_business_exception_route",
        "dependency": "business-exception-routing-dependency",
        "responsible_party": "routing_operations_owner",
        "recovery_action": "restore_and_verify_routing_dependency",
        "recovery_target": "Routing Determination",
        "criterion_id": "s07-routing-dependency-probe/1",
        "evidence_kind": "routing_dependency_probe",
    }
    _RUNTIME_STOP_REASON = "S01_RUNTIME_UNHEALTHY"
    _S07_FAILURE_PUBLICATION_EXHAUSTED = "control.failure_publication_exhausted"
    _PINNED_RELEASE_FAILURE = "PINNED_RELEASE_UNAVAILABLE"
    _POLICY_UNAVAILABLE_FAILURE = "configuration.policy_unavailable"
    _S09_HOLD_FAILURE = "S09_POLICY_SAFETY_HOLD"
    _APPLICATION_STATE_FAILURE = "APPLICATION_STATE_AUTHORITY_UNAVAILABLE"
    _ADMISSION_JOB_RECOVERY_FAILURE = "ADMISSION_JOB_RECOVERY_UNAVAILABLE"
    _REVIEW_SOURCE_FAILURE = "SOURCE_EVIDENCE_UNAVAILABLE"
    _RESUME_STOP_KEY = "_resume_stop"
    _SESSION_SCOPE_PREFIX = "C-DEMO/session/"
    _REVIEW_CLAIM_TTL_SECONDS = 900
    _EXCEPTION_CLAIM_TTL_SECONDS = 300
    _REVIEW_REASON_CODES = frozenset(
        {"HUMAN_REVIEW_COMPLETED", "HUMAN_REVIEW_RECONSIDERED"}
    )
    _CORRECTION_REASON_CODES = frozenset(
        {"SOURCE_VALUE_MISREAD", "SOURCE_VALUE_MISSING"}
    )
    # S10: the closed Reviewer membership-decision reason vocabulary and the
    # closed decision kinds.  An accepted decision selects an explicit document
    # instance and role; an explicit unassign withdraws or supersedes an
    # accepted membership.  Eligibility for the checker projection comes only
    # from these explicit accepted facts -- never from candidate confidence,
    # order, count, majority or last write.
    _MEMBERSHIP_REASON_CODES = frozenset(
        {
            "MEMBERSHIP_SOURCE_VERIFIED",
            "MEMBERSHIP_SOURCE_MISASSIGNED",
            "MEMBERSHIP_INSTANCE_WRONG",
            "MEMBERSHIP_PAGE_UNASSIGNED",
        }
    )
    _MEMBERSHIP_DECISIONS = frozenset({"accept", "unassign"})
    _MEMBERSHIP_RULE_IDS = frozenset(
        {"MEMBERSHIP_UNRESOLVED", "MEMBERSHIP_AMBIGUOUS"}
    )
    _MEMBERSHIP_REFUSED = "MEMBERSHIP_REFUSED"
    _EXCEPTION_REQUEST_REASON = "DOCUMENTED_BRAND_VARIANCE"
    _EXCEPTION_APPROVAL_REASONS = frozenset(
        {"DOCUMENTED_VARIANCE_ACCEPTED", "DOCUMENTED_VARIANCE_REJECTED"}
    )
    _EXCEPTION_INVALIDATION_REASONS = frozenset(
        {"POLICY_REVOKED", "BUSINESS_EXCEPTION_OPERATIONS_CLOSED"}
    )
    _EXCEPTION_OPERATIONS_CLOSED = "BUSINESS_EXCEPTION_OPERATIONS_CLOSED"
    _EXCEPTION_OPERATIONS_RESUMED = "BUSINESS_EXCEPTION_OPERATIONS_RESUMED"
    _EXCEPTION_POLICY_ID = "c-demo-brand-exception/1"
    _EXCEPTION_SCOPE = "one_application_cycle_run_finding"
    _EXCEPTION_TTL_SECONDS = 900
    _SUPPLEMENT_REQUEST_REASON = "MISSING_REQUIRED_MATERIAL"
    _SUPPLEMENT_TTL_SECONDS = 3600
    _SUPPLEMENT_REQUIREMENT_ID = "c-demo-financing-lease-vin/1"
    _SUPPLEMENT_SATISFACTION_POLICY_ID = "c-demo-supplement/1"
    _SUPPLEMENT_TARGET_DOCUMENT_ROLE = "\u878d\u8d44\u79df\u8d41\u5408\u540c"
    _PROTECTED_EXCEPTION_CHECKS = frozenset(
        {"R_VIN_CROSS", "R_ENGINE_CROSS", "R_ID_EXACT"}
    )
    _REVIEW_NOTE_MAX_CHARACTERS = 2000
    _REVIEW_NOTE_MAX_BYTES = 4096
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
    _C_DEMO_MISSING_VIN_PROVENANCE_ENTRIES = (
        ("reg_cert", "address", 1, "/documents/0/fields/address"),
        ("reg_cert", "engine_no", 1, "/documents/0/fields/engine_no"),
        ("reg_cert", "owner_name", 1, "/documents/0/fields/owner_name"),
        ("reg_cert", "plate_no", 1, "/documents/0/fields/plate_no"),
        ("reg_cert", "reg_cert_no", 1, "/documents/0/fields/reg_cert_no"),
        ("reg_cert", "reg_date", 1, "/documents/0/fields/reg_date"),
        ("reg_cert", "vin", 1, "/documents/0/fields/vin"),
        ("policy", "engine_no", 2, "/documents/1/fields/engine_no"),
        ("policy", "insured_name", 2, "/documents/1/fields/insured_name"),
        ("policy", "plate_list", 2, "/documents/1/fields/plate_list"),
        ("policy", "plate_no", 2, "/documents/1/fields/plate_no"),
        ("policy", "vin", 2, "/documents/1/fields/vin"),
        ("lease", "contract_date", 3, "/documents/2/fields/contract_date"),
        ("lease", "financed_amount", 3, "/documents/2/fields/financed_amount"),
        ("lease", "id_number", 3, "/documents/2/fields/id_number"),
        ("lease", "lessee_name", 3, "/documents/2/fields/lessee_name"),
        ("lease", "reg_cert_no", 3, "/documents/2/fields/reg_cert_no"),
        ("lease", "reg_date", 3, "/documents/2/fields/reg_date"),
        ("invoice", "engine_no", 4, "/documents/3/fields/engine_no"),
        ("invoice", "invoice_amount", 4, "/documents/3/fields/invoice_amount"),
        ("invoice", "vin", 4, "/documents/3/fields/vin"),
        ("id_card", "address", 5, "/documents/4/fields/address"),
        ("id_card", "id_number", 5, "/documents/4/fields/id_number"),
        ("id_card", "owner_name", 5, "/documents/4/fields/owner_name"),
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
        checker_status_query: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        recovery_verifier: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        legacy_oracle_runner: Callable[[Application], Any] | None = None,
        application_id_allocator: Callable[[], str] | None = None,
        state_path: str | Path | None = None,
        worker_identity: str = "s01-worker",
        clock: Callable[[], int] | None = None,
        registered_sources: tuple[RegisteredSource, ...] = (),
        controlled_objects: tuple[ControlledObject, ...] = (),
        scenario_id: str = "app_r53_bad_engine.json",
        checker_build: str | None = None,
        exception_approver_subject: str = "c-demo-exception-approver",
        policy_governance: Any | None = None,
    ) -> None:
        if not worker_identity or worker_identity.strip() != worker_identity:
            raise ValueError("worker identity must be a non-empty canonical value")
        if scenario_id not in self._ALLOWED_SCENARIOS:
            raise ValueError("controlled scenario is not allowlisted")
        if (
            not isinstance(exception_approver_subject, str)
            or not exception_approver_subject
            or exception_approver_subject.strip() != exception_approver_subject
            or len(exception_approver_subject) > 200
        ):
            raise ValueError("exception approver subject must be canonical")
        self.fixture_root = Path(fixture_root).resolve()
        self.rules_path = Path(rules_path).resolve()
        self._scenario_id = scenario_id
        self._exception_approver_subject = exception_approver_subject
        self.audit_available = audit_available
        self.storage_available = storage_available
        self._audit_writer = audit_writer
        self._fault_injector = fault_injector
        self._checker_runner = checker_runner
        self._checker_status_query = checker_status_query
        self._recovery_verifier = recovery_verifier
        self._legacy_oracle_runner = legacy_oracle_runner
        self._application_id_allocator = (
            application_id_allocator or self._default_application_id
        )
        self._worker_identity = worker_identity
        self._clock = clock or (lambda: int(time.time()))
        self._registered_source_boundary = RegisteredSourceBoundary(
            registered_sources, controlled_objects
        )
        self._registered_runtime_configured = bool(
            registered_sources or controlled_objects
        )
        if state_path is None:
            raise ValueError("state_path is required for the S01 target authority")
        self._store = _TargetStore(state_path)
        self._hydrate_admission_results()
        self._restore_cohort_stop_authority()
        self._lock = threading.RLock()
        self._local_cohort_stop: dict[str, Any] | None = None
        self._reconcile_admission_jobs()
        self._purge_expired_sessions(now=float(self._clock()))
        self._policy_governance = policy_governance
        self._checker_build_override = checker_build
        if self._policy_governance is None:
            # Pre-cutover legacy seam: the singleton baseline is bound at
            # startup.  Governed runtimes never construct it eagerly; the
            # resolver/load-pinned path reads the Registry instead.
            self._release = self._load_baseline_release()
            self._run_release = (
                self._release
                if checker_build is None
                or checker_build == self._release["checker_build"]
                else self._select_checker_release(self._release, checker_build)
            )
            self._target_checker = TargetChecker(
                self._run_release["target_release"]
            )
        else:
            self._release = None
            self._run_release = None
            self._target_checker = None
        self._source_provenance_manifest = self._load_source_provenance_manifest()
        self._manifest = self._build_artifact_manifest()

    def _legacy_release(self) -> dict[str, Any]:
        """Lazily load the pre-cutover singleton baseline (legacy seam only;
        governed runtimes resolve the Registry instead and never read the
        YAML/KB files for target runs)."""
        if self._release is None:
            self._release = self._load_baseline_release()
        return self._release

    def _legacy_run_release(self) -> dict[str, Any]:
        if self._run_release is None:
            release = self._legacy_release()
            checker_build = self._checker_build_override
            self._run_release = (
                release
                if checker_build is None
                or checker_build == release["checker_build"]
                else self._select_checker_release(release, checker_build)
            )
        return self._run_release

    def _legacy_target_checker(self) -> TargetChecker:
        if self._target_checker is None:
            self._target_checker = TargetChecker(
                self._legacy_run_release()["target_release"]
            )
        return self._target_checker

    def process_next_policy_job(self) -> dict[str, Any] | None:
        """Delegate S08 policy worker work to the governance service."""
        if self._policy_governance is None:
            return None
        return self._policy_governance.process_next_policy_job()

    # ------------------------------------------------------------ S09 impact

    _S09_IMPACT_OUTBOX_KINDS = frozenset(
        {"s09_impact_activated", "s09_hold_imposed", "s09_hold_released"}
    )
    _S09_TERMINAL_PARTITIONS = frozenset(
        {"verification_completed", "terminated", "compliance_deleted"}
    )

    @staticmethod
    def _current_run_generation(
        owner: SQLiteTargetStore, app: dict[str, Any]
    ) -> int | None:
        """The active generation proved by the application's current
        complete run, or None when no current run exists or its generation
        cannot be derived."""
        current_run_id = app.get("current_run_id")
        if not current_run_id:
            return None
        for run in owner.runs:
            if (
                run.get("run_id") != current_run_id
                or run.get("status") != "complete"
            ):
                continue
            spec = run.get("spec")
            generation = (
                spec.get("active_generation") if isinstance(spec, dict) else None
            )
            if isinstance(generation, int) and not isinstance(generation, bool):
                return generation
            return None
        return None

    @staticmethod
    def _holds_cover_application(
        hold_union: list[dict[str, Any]], application_id: str
    ) -> bool:
        """True when any Policy Safety Hold in the union is scoped to cover
        this application.  Holds are scope-bound: ``open_cycle`` or the
        served scope cover every open-cycle application; a concrete
        application id covers only that application."""
        for hold in hold_union or []:
            hold_scope = str(hold.get("hold_scope") or "")
            if hold_scope in {"open_cycle", "C-DEMO/demo"}:
                return True
            if hold_scope == application_id:
                return True
        return False

    @staticmethod
    def verification_route_for_checks(
        checks: tuple[_RunCheckResult, ...],
        findings: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    ) -> str:
        """Return the Lifecycle-owned route for one complete run result."""
        if any(
            check.verdict != "consistent"
            and check.severity in {"critical", "major"}
            for check in checks
        ) or any(
            finding.get("mandatory") is True
            and finding.get("verdict") != "consistent"
            for finding in findings
        ):
            return "manual_review"
        return "auto_complete"

    def _s09_currentness_block_reasons(
        self, owner: SQLiteTargetStore, app: Mapping[str, Any]
    ) -> tuple[str, ...]:
        """The shared Lifecycle currentness guard used by every current
        route/workspace/history consumer.

        Currentness stays Lifecycle-owned, but it can never be reported for
        a run the authoritative Governance facts invalidate: the guard
        resolves the exact same authority seam as the completion fence
        (``resolve_run_pin`` over the same physical store snapshot) and
        consults the scoped hold union and the final-impact
        membership/disposition receipts.  Immediately after activation --
        with the impact outbox still pending -- an affected old run returns
        a stable non-current reason instead of ``CURRENT_CONTEXT_MATCH``;
        projection lag never relaxes currentness.  Any authority/integrity
        resolution failure fails closed exactly like the fence.  When no
        governed activation exists (pre-cutover) or the application has no
        current run, Lifecycle alone owns the frame."""
        if self._policy_governance is None or not app.get("current_run_id"):
            return ()
        try:
            pin = self._policy_governance.resolve_run_pin(
                S08_SCOPE, int(self._clock()), store=owner
            )
        except Exception:
            # Authority cannot be resolved: fail closed rather than report
            # a currentness the Governance facts can no longer prove.
            return ("BLOCKED_AUTHORITY_UNAVAILABLE",)
        if pin is None:
            return ()
        reasons: list[str] = []
        current_generation = self._current_run_generation(owner, app)
        if (
            current_generation is None
            or current_generation != int(pin["active_generation"])
        ):
            reasons.append("STALE_GENERATION")
        if self._holds_cover_application(
            pin.get("hold_union") or [],
            str(app.get("application_id") or ""),
        ):
            reasons.append("BLOCKED_POLICY_HOLD")
        final_digest = pin.get("final_impact_digest")
        if final_digest and self._policy_governance is not None:
            try:
                manifest = self._policy_governance.load_final_impact(
                    final_digest, store=owner
                )
            except Exception:
                manifest = None
            if manifest is None:
                # Integrity fail-closed: the pinned final impact cannot be
                # verified, so currentness cannot be reported at all.
                reasons.append("BLOCKED_AUTHORITY_UNAVAILABLE")
            else:
                key = (
                    str(app.get("application_id") or ""),
                    int(app.get("cycle") or 0),
                )
                if any(
                    str(member.get("application_id") or "") == key[0]
                    and int(member.get("cycle") or 0) == key[1]
                    for member in manifest.get("members", [])
                ):
                    receipt = self._impact_receipts(owner, final_digest).get(key)
                    if receipt is None or receipt.get("disposition") == "outstanding":
                        reasons.append("BLOCKED_IMPACT_DISPOSITION")
        return tuple(reasons)

    def _impact_receipts(
        self,
        owner: SQLiteTargetStore,
        final_impact_digest: str,
    ) -> dict[tuple[str, int], dict[str, Any]]:
        """Lifecycle-owned consumption receipts for one final impact digest."""
        receipts: dict[tuple[str, int], dict[str, Any]] = {}
        for message in owner.inbox:
            if (
                message.get("kind") != "s09_impact_disposition"
                or message.get("final_impact_digest") != final_impact_digest
            ):
                continue
            key = (
                str(message.get("application_id") or ""),
                int(message.get("cycle") or 0),
            )
            if not key[0] or key in receipts:
                continue
            receipts[key] = message
        return receipts

    def build_policy_impact_snapshot(
        self,
        owner: SQLiteTargetStore,
        final_impact_digest: str | None = None,
    ) -> dict[str, Any]:
        """The read-only, side-effect-free Lifecycle-owned impact snapshot.

        Governance passes its own reloaded store snapshot here so both
        owners see one consistent physical view; the builder never mutates
        anything.  The snapshot carries the deterministic application
        universe, per-member partition/current-run/expected-revision facts,
        and (when requested) the consumption receipts for one final impact
        digest."""
        deleted_ids = {
            str(receipt.get("application_id") or "")
            for receipt in owner.deletion_receipts
            if isinstance(receipt, dict) and receipt.get("application_id")
        }
        applications: list[dict[str, Any]] = []
        for application_id, app in owner.applications.items():
            phase = str(app.get("phase") or "")
            if application_id in deleted_ids:
                partition = "compliance_deleted"
            elif phase == "Verification Completed":
                partition = "verification_completed"
            elif phase == "Terminated":
                partition = "terminated"
            else:
                partition = "open_cycle"
            current_generation = self._current_run_generation(owner, app)
            applications.append(
                {
                    "application_id": application_id,
                    "cycle": int(app.get("cycle") or 1),
                    "partition": partition,
                    "phase": phase,
                    "route": str(app.get("route") or ""),
                    "current_run_id": (
                        str(app.get("current_run_id"))
                        if app.get("current_run_id")
                        else None
                    ),
                    "current_generation": current_generation,
                    "lifecycle_revision": int(app.get("lifecycle_revision") or 0),
                    "evidence_revision": int(app.get("evidence_revision") or 0),
                    "active_hold_ids": sorted(
                        str(hold_id)
                        for hold_id in app.get("active_hold_ids") or []
                        if str(hold_id)
                    ),
                    "old_references_operable": bool(
                        app.get("current_run_id")
                        or phase != "Unprocessable"
                    ),
                }
            )
        identities = sorted(
            (str(item["application_id"]), int(item["cycle"]))
            for item in applications
        )
        identity_bytes = json.dumps(
            identities,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        universe_digest = hashlib.sha256(identity_bytes).hexdigest()
        partition_tallies = sorted(
            (
                partition,
                sum(item["partition"] == partition for item in applications),
            )
            for partition in (
                "open_cycle",
                "verification_completed",
                "terminated",
                "compliance_deleted",
            )
        )
        dependency_index_digest = hashlib.sha256(
            json.dumps(
                partition_tallies,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        snapshot: dict[str, Any] = {
            "scope": "C-DEMO/demo",
            "complete": True,
            "lifecycle_watermark": len(owner.lifecycle_events),
            "dependency_index_digest": dependency_index_digest,
            "dependency_index_version": "s09-lifecycle-dependency-index/1",
            "applications": applications,
            "universe": {
                "complete": True,
                "count": len(identities),
                "digest": universe_digest,
            },
        }
        if final_impact_digest:
            receipts = self._impact_receipts(owner, final_impact_digest)
            unconsumed = 0
            outstanding = 0
            manifest = (
                self._policy_governance.load_final_impact(
                    final_impact_digest, store=owner
                )
                if self._policy_governance is not None
                else None
            )
            if manifest is not None:
                for member in manifest.get("members", []):
                    key = (
                        str(member.get("application_id") or ""),
                        int(member.get("cycle") or 0),
                    )
                    if key not in receipts:
                        unconsumed += 1
                    elif receipts[key].get("disposition") == "outstanding":
                        outstanding += 1
            snapshot["unconsumed_count"] = unconsumed
            snapshot["outstanding_count"] = outstanding
            snapshot["receipts"] = {
                f"{key[0]}:{key[1]}": dict(message)
                for key, message in receipts.items()
            }
            coverage: dict[str, dict[str, Any]] = {}
            if manifest is not None:
                for member in manifest.get("members", []):
                    application_id = str(member.get("application_id") or "")
                    cycle = int(member.get("cycle") or 0)
                    key = f"{application_id}:{cycle}"
                    message = receipts.get((application_id, cycle))
                    job_count = sum(
                        job.get("kind") == "operational_reevaluation"
                        and job.get("application_id") == application_id
                        and job.get("final_impact_digest") == final_impact_digest
                        for job in owner.jobs
                    )
                    coverage[key] = {
                        "disposition": (
                            str(message.get("disposition") or "")
                            if message is not None
                            else "outstanding"
                        ),
                        "reevaluation_job_count": int(job_count),
                        "blocked_by_hold": bool(
                            message is not None
                            and message.get("blocked_by_hold")
                        ),
                    }
            snapshot["member_coverage"] = coverage
        return snapshot

    def build_policy_diagnostic_snapshot(
        self,
        owner: SQLiteTargetStore,
        application_id: str,
    ) -> dict[str, Any]:
        """Return one exact Lifecycle-owned fixed run snapshot for S09.

        Selection is by the application's authoritative current run identity;
        a list-order or latest-run fallback is never permitted."""
        app = owner.applications.get(application_id)
        if not isinstance(app, dict):
            return {
                "schema_version": "s09-diagnostic-snapshot/1",
                "complete": False,
                "application_id": application_id,
            }
        run_id = app.get("current_run_id")
        if not isinstance(run_id, str) or not run_id:
            return {
                "schema_version": "s09-diagnostic-snapshot/1",
                "complete": False,
                "application_id": application_id,
            }
        matches = [
            run
            for run in owner.runs
            if run.get("run_id") == run_id
            and run.get("application_id") == application_id
            and run.get("status") == "complete"
            and isinstance(run.get("spec"), dict)
            and isinstance(run["spec"].get("evidence_snapshot"), dict)
        ]
        if len(matches) != 1:
            return {
                "schema_version": "s09-diagnostic-snapshot/1",
                "complete": False,
                "application_id": application_id,
            }
        run_spec = copy.deepcopy(matches[0]["spec"])
        if (
            run_spec.get("application_id") != application_id
            or run_spec.get("cycle") != app.get("cycle")
            or run_spec.get("evidence_snapshot_id")
            != app.get("current_evidence_snapshot_id")
            or run_spec.get("evidence_snapshot_digest")
            != app.get("current_evidence_snapshot_digest")
        ):
            return {
                "schema_version": "s09-diagnostic-snapshot/1",
                "complete": False,
                "application_id": application_id,
            }
        return {
            "schema_version": "s09-diagnostic-snapshot/1",
            "complete": True,
            "application_id": application_id,
            "cycle": int(app.get("cycle") or 0),
            "run_id": run_id,
            "run_spec": run_spec,
            "evidence_snapshot_id": run_spec.get("evidence_snapshot_id"),
            "evidence_snapshot_digest": run_spec.get("evidence_snapshot_digest"),
            "lifecycle_revision": int(app.get("lifecycle_revision") or 0),
        }

    def _create_operational_reevaluation_job(
        self,
        staged: SQLiteTargetStore,
        *,
        app: dict[str, Any],
        final_impact_digest: str,
        target_generation: int,
        now: int,
    ) -> str | None:
        """One durable Operational Re-evaluation job per member; the stable
        job identity makes duplicate delivery idempotent."""
        job_id = self._stable_id(
            "job",
            f"operational_reevaluation:{app['application_id']}:{app['cycle']}:{final_impact_digest}",
        )
        for job in staged.jobs:
            if job.get("job_id") == job_id:
                return job_id
        staged.jobs.append(
            {
                "job_id": job_id,
                "application_id": app["application_id"],
                "kind": "operational_reevaluation",
                "status": "queued",
                "fence": 0,
                "attempt_no": 0,
                "fingerprint": final_impact_digest,
                "target_generation": target_generation,
                "final_impact_digest": final_impact_digest,
                "created_at": now,
            }
        )
        return job_id

    def _record_impact_disposition(
        self,
        staged: SQLiteTargetStore,
        *,
        app: dict[str, Any] | None,
        member: dict[str, Any],
        final_impact_digest: str,
        disposition: str,
        job_id: str | None,
        now: int,
        blocked_by_hold: bool = False,
    ) -> None:
        """The idempotent Lifecycle disposition receipt: one inbox message
        per (digest, application_id, cycle) plus the append-only lifecycle
        fact for open-cycle/tracked members."""
        application_id = str(member["application_id"])
        cycle = int(member["cycle"])
        message_id = self._stable_id(
            "impact",
            f"{final_impact_digest}:{application_id}:{cycle}",
        )
        for message in staged.inbox:
            if message.get("message_id") == message_id:
                return
        staged.inbox.append(
            {
                "message_id": message_id,
                "kind": "s09_impact_disposition",
                "final_impact_digest": final_impact_digest,
                "application_id": application_id,
                "cycle": cycle,
                "partition": str(member.get("partition") or ""),
                "disposition": disposition,
                "target_generation": int(member.get("target_generation") or 0),
                "required_disposition": str(
                    member.get("required_disposition") or ""
                ),
                "job_id": job_id,
                "blocked_by_hold": blocked_by_hold,
                "received_at": now,
            }
        )
        if app is None:
            return
        staged.lifecycle_events.append(
            {
                "event_id": self._stable_id(
                    "lifecycle",
                    f"{application_id}:{cycle}:impact:{final_impact_digest[:16]}",
                ),
                "application_id": application_id,
                "revision": int(app.get("lifecycle_revision") or 0),
                "phase": str(app.get("phase") or ""),
                "cycle": cycle,
                "auxiliary": True,
                "reason_code": f"S09_DISPOSITION_{disposition.upper()}",
                "final_impact_digest": final_impact_digest,
                "disposition": disposition,
                "target_generation": int(member.get("target_generation") or 0),
                "partition": str(member.get("partition") or ""),
                "job_id": job_id,
            }
        )

    def _consume_impact_activated(
        self,
        staged: SQLiteTargetStore,
        event: dict[str, Any],
        now: int,
    ) -> None:
        final_impact_digest = str(event.get("final_impact_digest") or "")
        if not final_impact_digest or self._policy_governance is None:
            raise RuntimeError("final impact fact identity is unavailable")
        manifest = self._policy_governance.load_final_impact(
            final_impact_digest, store=staged
        )
        if manifest is None:
            raise RuntimeError("final impact manifest is unavailable")
        for member in manifest.get("members", []):
            application_id = str(member.get("application_id") or "")
            cycle = int(member.get("cycle") or 0)
            partition = str(member.get("partition") or "")
            target_generation = int(member.get("target_generation") or 0)
            app = staged.applications.get(application_id)
            if app is None or int(app.get("cycle") or 0) != cycle:
                # The member cannot be reconciled to a live application:
                # the tuple stays outstanding and the generation fence keeps
                # blocking until a successor proves it.
                self._record_impact_disposition(
                    staged,
                    app=None,
                    member=member,
                    final_impact_digest=final_impact_digest,
                    disposition="outstanding",
                    job_id=None,
                    now=now,
                )
                continue
            if partition == "open_cycle":
                current_run_generation = self._current_run_generation(
                    staged, app
                )
                if (
                    current_run_generation is not None
                    and current_run_generation >= target_generation
                ):
                    # A current successor already proved the exact new
                    # generation and required context: no stale, no
                    # reevaluation job -- only the reconcilable receipt.
                    # Defensive branch: the completion fence forces
                    # consumption before any target-generation run becomes
                    # current, so in normal flow this is reached only by
                    # duplicate delivery (no-op) or level-2 expansion.
                    self._record_impact_disposition(
                        staged,
                        app=app,
                        member=member,
                        final_impact_digest=final_impact_digest,
                        disposition="already_revalidated",
                        job_id=None,
                        now=now,
                    )
                    continue
                self._apply_open_cycle_disposition(
                    staged,
                    app=app,
                    member=member,
                    final_impact_digest=final_impact_digest,
                    target_generation=target_generation,
                    now=now,
                )
            else:
                # Terminal partitions: record historical exposure only; no
                # historical rewrite and no reevaluation job.
                self._record_impact_disposition(
                    staged,
                    app=app,
                    member=member,
                    final_impact_digest=final_impact_digest,
                    disposition="historical_terminated_exposure",
                    job_id=None,
                    now=now,
                )

    def _apply_open_cycle_disposition(
        self,
        staged: SQLiteTargetStore,
        *,
        app: dict[str, Any],
        member: dict[str, Any],
        final_impact_digest: str,
        target_generation: int,
        now: int,
    ) -> None:
        app_id = app["application_id"]
        cycle = int(app.get("cycle") or 0)
        message_id = self._stable_id(
            "impact",
            f"{final_impact_digest}:{app_id}:{cycle}",
        )
        for message in staged.inbox:
            if (
                message.get("message_id") == message_id
                and message.get("disposition") != "outstanding"
            ):
                return
        phase = str(app.get("phase") or "")
        old_run_id = app.get("current_run_id")
        # Stale the current run and route: the old generation can never be
        # current again after the boundary changed.
        app["current_run_id"] = None
        app["current_evidence_snapshot_id"] = None
        app["current_evidence_snapshot_digest"] = None
        app["route"] = "pending_check"
        app["projection_pending"] = False
        app["projection_visible"] = False
        if old_run_id:
            staged.lifecycle_events.append(
                {
                    "event_id": self._stable_id(
                        "lifecycle",
                        f"{app_id}:{cycle}:invalidate:{old_run_id}",
                    ),
                    "application_id": app_id,
                    "revision": int(app.get("lifecycle_revision") or 0),
                    "phase": str(app.get("phase") or ""),
                    "cycle": cycle,
                    "auxiliary": True,
                    "reason_code": "S09_GENERATION_INVALIDATED",
                    "invalidated_run_id": str(old_run_id),
                    "final_impact_digest": final_impact_digest,
                }
            )
        # Record the authoritative generation the member must re-prove.
        app["policy_generation"] = target_generation
        app["current_final_impact_digest"] = final_impact_digest
        if phase == "Unprocessable":
            # An active hold already blocks this member: the stale/recheck
            # part of the disposition is committed, while the reevaluation
            # itself waits for the explicit hold release -- never a timer.
            # Recording ``applied`` keeps the tuple reconcilable so the
            # separate recovery command can prove delivery and release the
            # hold; the hold union itself keeps the generation fence closed.
            self._record_impact_disposition(
                staged,
                app=app,
                member=member,
                final_impact_digest=final_impact_digest,
                disposition="applied",
                job_id=None,
                now=now,
                blocked_by_hold=True,
            )
            # The stale/route marking above is the pre-hold frame; the
            # authority-consistent Unprocessable frame (route
            # ``unprocessable``) is restored so the read paths never see the
            # (Unprocessable, pending_check) pair as an authority failure.
            app["route"] = "unprocessable"
            return
        hit_reasons = {
            str(reason) for reason in member.get("hit_reasons") or ()
        }
        if "evidence_dependency" in hit_reasons:
            # P-4: a dependency-context change (readiness/normalization/
            # comparison/semantic/entity knowledge) leaves the application in
            # Assembly with a durable assembly obligation: the disposition is
            # recorded, the app stays in Assembly without evidence_ready and
            # without an Operational Re-evaluation job, and the assembly
            # obligation (phase + policy_generation + final-impact binding)
            # is durable store state.  Only an unchanged dependency context
            # enters Evidence Ready with exactly one reevaluation job.
            if phase != "Assembly":
                self._transition_lifecycle(
                    app, "Assembly", "S09_IMPACT_REASSEMBLE", store=staged
                )
            app["route"] = "pending_check"
            app["evidence_ready"] = False
            app["projection_pending"] = False
            app["projection_visible"] = False
            self._record_impact_disposition(
                staged,
                app=app,
                member=member,
                final_impact_digest=final_impact_digest,
                disposition="applied",
                job_id=None,
                now=now,
            )
            return
        if phase != "Evidence Ready":
            # Every remaining open-cycle phase (Intake, Assembly, Awaiting
            # Evidence, Checking, Routing Determination, Manual Review,
            # Supplement, Pending Exception Approval) re-enters through
            # Assembly; a terminal phase is never an open-cycle member.
            if phase != "Assembly":
                self._transition_lifecycle(
                    app, "Assembly", "S09_IMPACT_REASSEMBLE", store=staged
                )
            self._transition_lifecycle(
                app, "Evidence Ready", "S09_OPERATIONAL_RE_EVALUATION", store=staged
            )
        app["evidence_ready"] = True
        job_id = self._create_operational_reevaluation_job(
            staged,
            app=app,
            final_impact_digest=final_impact_digest,
            target_generation=target_generation,
            now=now,
        )
        self._record_impact_disposition(
            staged,
            app=app,
            member=member,
            final_impact_digest=final_impact_digest,
            disposition="applied",
            job_id=job_id,
            now=now,
        )

    def _consume_hold_imposed(
        self,
        staged: SQLiteTargetStore,
        event: dict[str, Any],
        now: int,
    ) -> None:
        hold_id = str(event.get("hold_id") or "")
        hold_scope = str(event.get("hold_scope") or "")
        if not hold_id:
            raise RuntimeError("hold fact identity is unavailable")
        for app in staged.applications.values():
            phase = str(app.get("phase") or "")
            if phase in {
                "Verification Completed",
                "Terminated",
            }:
                continue
            if not (
                hold_scope in {"open_cycle", "C-DEMO/demo"}
                or hold_scope == app["application_id"]
            ):
                continue
            active_hold_ids = sorted(
                set(app.get("active_hold_ids") or []) | {hold_id}
            )
            app["active_hold_ids"] = active_hold_ids
            if phase != "Unprocessable":
                if (
                    phase != "Assembly"
                    and "Unprocessable"
                    not in self._ALLOWED_PHASE_SUCCESSORS.get(
                        phase, frozenset()
                    )
                ):
                    # Phases that cannot reach Unprocessable directly
                    # re-enter through a legal predecessor: Evidence Ready
                    # through Checking, the rest through Assembly.
                    if phase == "Evidence Ready":
                        self._transition_lifecycle(
                            app, "Checking", "S09_HOLD_REENTER", store=staged
                        )
                    else:
                        self._transition_lifecycle(
                            app, "Assembly", "S09_IMPACT_REASSEMBLE", store=staged
                        )
                self._transition_lifecycle(
                    app, "Unprocessable", "S09_POLICY_SAFETY_HOLD", store=staged
                )
                # The old run/route/work references are invalidated in the
                # same transition: the phase becomes Unprocessable with no
                # current run, so current-route/history queries return the
                # stable non-current frame instead of an unreconstructible
                # authority failure.
                old_run_id = app.get("current_run_id")
                if old_run_id:
                    staged.lifecycle_events.append(
                        {
                            "event_id": self._stable_id(
                                "lifecycle",
                                f"{app['application_id']}:{app['cycle']}:"
                                f"hold-invalidate:{old_run_id}",
                            ),
                            "application_id": app["application_id"],
                            "revision": int(app.get("lifecycle_revision") or 0),
                            "phase": "Unprocessable",
                            "cycle": int(app.get("cycle") or 0),
                            "auxiliary": True,
                            "reason_code": "S09_HOLD_INVALIDATED",
                            "invalidated_run_id": str(old_run_id),
                            "hold_id": hold_id,
                        }
                    )
            # The authority-consistent Unprocessable frame is enforced for
            # every covered application, whether it just transitioned or was
            # already held: an impact disposition can re-enter a held member
            # through Assembly (route ``pending_check``) between the
            # transition and this consumption, and the read paths treat any
            # other (phase, route) pair as an authority failure.  The phase/
            # route invariant is restored here unconditionally.
            app["current_run_id"] = None
            app["current_evidence_snapshot_id"] = None
            app["current_evidence_snapshot_digest"] = None
            app["route"] = "unprocessable"
            app["evidence_ready"] = False
            app["projection_pending"] = False
            app["projection_visible"] = False

    def _consume_hold_released(
        self,
        staged: SQLiteTargetStore,
        event: dict[str, Any],
        now: int,
    ) -> None:
        hold_id = str(event.get("hold_id") or "")
        recovery_generation = int(event.get("recovery_generation") or 0)
        if not hold_id:
            raise RuntimeError("hold release fact identity is unavailable")
        for app in staged.applications.values():
            active_hold_ids = sorted(
                set(app.get("active_hold_ids") or []) - {hold_id}
            )
            app["active_hold_ids"] = active_hold_ids
            if active_hold_ids:
                continue
            if str(app.get("phase") or "") != "Unprocessable":
                continue
            unprocessable_reasons = [
                item.get("reason_code")
                for item in staged.lifecycle_events
                if item.get("application_id") == app["application_id"]
                and item.get("reason_code") == "S09_POLICY_SAFETY_HOLD"
                and item.get("revision") == int(app.get("lifecycle_revision") or 0)
            ]
            if not unprocessable_reasons:
                continue
            target_generation = recovery_generation
            app["policy_generation"] = target_generation
            # P-4: a dependency-context obligation stays durable across the
            # hold.  Releasing an evidence-dependent member returns it to
            # Assembly (no Evidence Ready, no reevaluation job): the member
            # must re-assemble its evidence before any run can become
            # current at the recovery generation.
            final_impact_digest = str(app.get("current_final_impact_digest") or "")
            evidence_dependent = False
            if final_impact_digest and self._policy_governance is not None:
                manifest = self._policy_governance.load_final_impact(
                    final_impact_digest, store=staged
                )
                if manifest is not None:
                    evidence_dependent = any(
                        str(m.get("application_id") or "")
                        == str(app.get("application_id") or "")
                        and int(m.get("cycle") or 0) == int(app.get("cycle") or 0)
                        and "evidence_dependency"
                        in {str(reason) for reason in m.get("hit_reasons") or ()}
                        for m in manifest.get("members", [])
                    )
            if evidence_dependent:
                self._transition_lifecycle(
                    app, "Assembly", "S09_HOLD_RELEASE_ASSEMBLY", store=staged
                )
                app["route"] = "pending_check"
                app["evidence_ready"] = False
                app["projection_pending"] = False
                app["projection_visible"] = False
                staged.lifecycle_events.append(
                    {
                        "event_id": self._stable_id(
                            "lifecycle",
                            f"{app['application_id']}:{app['cycle']}:hold-release:"
                            f"{hold_id}",
                        ),
                        "application_id": app["application_id"],
                        "revision": int(app.get("lifecycle_revision") or 0),
                        "phase": "Assembly",
                        "cycle": int(app.get("cycle") or 0),
                        "auxiliary": True,
                        "reason_code": "S09_HOLD_RELEASE_CONSUMED",
                        "hold_id": hold_id,
                        "recovery_generation": recovery_generation,
                    }
                )
                continue
            self._transition_lifecycle(
                app, "Evidence Ready", "S09_HOLD_RELEASE_CONSUMED", store=staged
            )
            app["route"] = "pending_check"
            app["evidence_ready"] = True
            job_id = self._create_operational_reevaluation_job(
                staged,
                app=app,
                final_impact_digest=(
                    final_impact_digest
                    if final_impact_digest
                    else f"recovery:{recovery_generation}"
                ),
                target_generation=target_generation,
                now=now,
            )
            staged.lifecycle_events.append(
                {
                    "event_id": self._stable_id(
                        "lifecycle",
                        f"{app['application_id']}:{app['cycle']}:hold-release:"
                        f"{hold_id}",
                    ),
                    "application_id": app["application_id"],
                    "revision": int(app.get("lifecycle_revision") or 0),
                    "phase": "Evidence Ready",
                    "cycle": int(app.get("cycle") or 0),
                    "auxiliary": True,
                    "reason_code": "S09_HOLD_RELEASE_CONSUMED",
                    "hold_id": hold_id,
                    "recovery_generation": recovery_generation,
                    "job_id": job_id,
                }
            )

    def process_next_policy_impact(self) -> int:
        """Consume pending immutable governance impact/hold facts into
        Lifecycle dispositions.  At-least-once: the outbox record becomes
        ``published`` only in the same transaction as the dispositions, and
        every per-member receipt is idempotent under duplicate delivery."""
        if self._policy_governance is None:
            return 0
        with self._lock:
            for _ in range(3):
                self._reload_store()
                pending = [
                    event
                    for event in self._store.outbox
                    if event.get("kind") in self._S09_IMPACT_OUTBOX_KINDS
                    and event.get("status") == "pending"
                ]
                if not pending:
                    return 0
                staged = copy.deepcopy(self._store)
                now = int(self._clock())
                for event in staged.outbox:
                    if (
                        event.get("kind") not in self._S09_IMPACT_OUTBOX_KINDS
                        or event.get("status") != "pending"
                    ):
                        continue
                    kind = event.get("kind")
                    if kind == "s09_impact_activated":
                        self._consume_impact_activated(staged, event, now)
                    elif kind == "s09_hold_imposed":
                        self._consume_hold_imposed(staged, event, now)
                    elif kind == "s09_hold_released":
                        self._consume_hold_released(staged, event, now)
                    event["status"] = "published"
                try:
                    staged.persist()
                except StaleStoreRevision:
                    continue
                self._store = staged
                return len(pending)
            raise RuntimeError("policy impact consumption retry exhausted")

    def impact_dispositions_view(
        self,
        *,
        principal: S01CommandPrincipal,
        final_impact_digest: str,
    ) -> dict[str, Any]:
        """Minimized Lifecycle-owned consumption view for one final impact
        manifest.  An ordinary Reviewer sees only the aggregate digest,
        counts and projection watermark; per-member application/job receipts
        are exposed only to an authorized audit/reconciliation identity with
        a matching resource scope.  Only digest/count/identity fields are
        exposed; raw values and free text never leave the service."""
        if principal.role == "reviewer":
            detail = False
        elif principal.role in {"auditor", "reconciliation"}:
            detail = True
        else:
            raise QueryNotFound("final impact manifest is unavailable")
        if detail and not (
            principal.scope == "C-DEMO"
            or self.is_c_demo_scope(principal.scope)
        ):
            raise QueryNotFound("final impact manifest is unavailable")
        if self._policy_governance is None:
            raise QueryNotFound("final impact manifest is unavailable")
        with self._lock:
            self._reload_store()
            manifest = self._policy_governance.load_final_impact(
                final_impact_digest, store=self._store
            )
            if manifest is None:
                raise QueryNotFound("final impact manifest is unavailable")
            receipts = self._impact_receipts(self._store, final_impact_digest)
            members = []
            for member in manifest.get("members", []):
                application_id = str(member["application_id"])
                cycle = int(member["cycle"])
                key = (application_id, cycle)
                receipt = receipts.get(key)
                disposition = (
                    str(receipt.get("disposition") or "")
                    if receipt is not None
                    else "outstanding"
                )
                job_ids = [
                    job.get("job_id")
                    for job in self._store.jobs
                    if job.get("kind") == "operational_reevaluation"
                    and job.get("application_id") == application_id
                    and job.get("final_impact_digest") == final_impact_digest
                ]
                members.append(
                    {
                        "application_id": application_id,
                        "cycle": cycle,
                        "partition": str(member.get("partition") or ""),
                        "required_disposition": str(
                            member.get("required_disposition") or ""
                        ),
                        "disposition": disposition,
                        "target_generation": int(
                            member.get("target_generation") or 0
                        ),
                        "reevaluation_job_id": job_ids[0] if job_ids else None,
                        "reevaluation_job_count": len(job_ids),
                    }
                )
            unconsumed = sum(
                1
                for member in members
                if member["disposition"] == "outstanding"
            )
            result: dict[str, Any] = {
                "final_impact_digest": final_impact_digest,
                "member_count": len(members),
                "unconsumed_count": unconsumed,
                "projection_watermark": self._store.projection_watermark,
            }
            if detail:
                result["members"] = members
            return result

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
        if scenario_id != self._scenario_id:
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

    def submit_registered(
        self,
        *,
        submission: dict[str, Any],
        idempotency_key: str,
        principal: S01CommandPrincipal | None = None,
    ) -> AdmissionResult:
        """Verify and atomically admit one registered R-OBSERVED envelope."""
        if principal is None or not self._valid_registered_principal(principal):
            return self._registered_rejected("intake.forbidden")
        if not self._valid_idempotency_key(idempotency_key):
            return self._registered_rejected("intake.idempotency_key_invalid")
        try:
            command_fingerprint = self._registered_source_boundary.command_fingerprint(
                submission
            )
        except S02IntakeError as error:
            return self._registered_failure_result(error)
        workload_id = (
            str(submission.get("workload_identity_id") or "")
            if isinstance(submission, dict)
            else ""
        )
        binding_key = self._registered_idempotency_binding_key(
            principal, idempotency_key, workload_id
        )

        with self._lock:
            self._reload_store()
            previous = self._store.idempotency.get(binding_key)
            if previous is not None:
                previous_fingerprint, previous_result = previous
                if previous_fingerprint == command_fingerprint:
                    return self._registered_replay(previous_result)
                return self._registered_idempotency_conflict(previous_result)

        try:
            envelope = self._registered_source_boundary.canonicalize(
                submission,
                scope=principal.scope,
                source_system_id=principal.source_id,
            )
        except S02IntakeError as error:
            with self._lock:
                self._reload_store()
                return self._record_registered_disposition(
                    error=error,
                    command_fingerprint=command_fingerprint,
                    binding_key=binding_key,
                    principal=principal,
                    submission=submission if isinstance(submission, dict) else {},
                )

        with self._lock:
            self._reload_store()
            previous = self._store.idempotency.get(binding_key)
            if previous is not None:
                previous_fingerprint, previous_result = previous
                if previous_fingerprint == command_fingerprint:
                    return self._registered_replay(previous_result)
                return self._registered_idempotency_conflict(previous_result)

            accepted = self._accepted_source_stream_receipts(envelope)
            same_revision = [
                receipt
                for receipt in accepted
                if receipt.source_revision == envelope.source_revision
            ]
            if same_revision:
                existing = same_revision[0]
                if existing.envelope_fingerprint == envelope.fingerprint:
                    return self._registered_replay(existing)
                conflict = S02IntakeError(
                    "quarantined",
                    "intake.source_revision_conflict",
                    responsible_party="source_owner",
                    recovery_action="reconcile_the_source_revision_history",
                    gate_results=(
                        "identity:verified",
                        "contract:verified",
                        "object:verified",
                        "causality:failed",
                    ),
                    adapter_id=envelope.adapter_id,
                    adapter_version=envelope.adapter_version,
                    registration_digest=envelope.registration_digest,
                )
                return self._record_registered_disposition(
                    error=conflict,
                    command_fingerprint=command_fingerprint,
                    binding_key=binding_key,
                    principal=principal,
                    submission=submission,
                    envelope=envelope,
                )
            if envelope.source_revision != 1:
                predecessor_exists = any(
                    receipt.source_revision == envelope.predecessor_revision
                    for receipt in accepted
                )
                error = S02IntakeError(
                    "rejected" if predecessor_exists else "awaiting_predecessor",
                    (
                        "evidence.late_input_requires_reopen"
                        if predecessor_exists
                        else "intake.sequence_gap"
                    ),
                    responsible_party=(
                        "lifecycle_owner" if predecessor_exists else "source_owner"
                    ),
                    recovery_action=(
                        "use_the_later_reopen_contract"
                        if predecessor_exists
                        else "submit_and_reconcile_the_declared_predecessor"
                    ),
                    gate_results=(
                        "identity:verified",
                        "contract:verified",
                        "object:verified",
                        "causality:failed",
                    ),
                    adapter_id=envelope.adapter_id,
                    adapter_version=envelope.adapter_version,
                    registration_digest=envelope.registration_digest,
                )
                return self._record_registered_disposition(
                    error=error,
                    command_fingerprint=command_fingerprint,
                    binding_key=binding_key,
                    principal=principal,
                    submission=submission,
                    envelope=envelope,
                )
            if not self.audit_available:
                return self._registered_rejected("intake.audit_unavailable")
            if not self.storage_available:
                return self._registered_rejected("intake.storage_unavailable")
            return self._admit_registered(
                envelope,
                principal=principal,
                binding_key=binding_key,
                command_fingerprint=command_fingerprint,
            )

    def submit_attachment_version(
        self,
        *,
        submission: dict[str, Any],
        idempotency_key: str,
        principal: S01CommandPrincipal | None = None,
        now: float | None = None,
    ) -> AdmissionResult:
        """Verify and append one attachment version for an open supplement request."""
        receipt_time = int(self._clock() if now is None else now)
        if principal is None or not self._valid_registered_principal(principal):
            return self._registered_rejected("intake.forbidden")
        if not self._valid_idempotency_key(idempotency_key):
            return self._registered_rejected("intake.idempotency_key_invalid")
        try:
            command_fingerprint = self._registered_source_boundary.command_fingerprint(
                submission
            )
        except S02IntakeError as error:
            return self._registered_failure_result(error)
        workload_id = str(submission.get("workload_identity_id") or "")
        binding_key = self._registered_idempotency_binding_key(
            principal,
            idempotency_key,
            workload_id,
            action="submit_attachment_version",
        )
        with self._lock:
            self._reload_store()
            previous = self._store.idempotency.get(binding_key)
            if previous is not None:
                if previous[0] == command_fingerprint:
                    return self._registered_replay(previous[1])
                return self._registered_idempotency_conflict(previous[1])
            if self._supplement_operations_state()["intake"] == "closed":
                return self._registered_rejected("supplement.intake_stopped")
        try:
            envelope = self._registered_source_boundary.canonicalize_attachment_version(
                submission,
                scope=principal.scope,
                source_system_id=principal.source_id,
            )
        except S02IntakeError as error:
            with self._lock:
                self._reload_store()
                return self._record_registered_disposition(
                    error=error,
                    command_fingerprint=command_fingerprint,
                    binding_key=binding_key,
                    principal=principal,
                    submission=submission,
                    command_type="submit_attachment_version",
                )

        request_binding = envelope.payload["request_binding"]
        request_id = request_binding["supplement_request_id"]
        with self._lock:
            for attempt in range(2):
                self._reload_store()
                previous = self._store.idempotency.get(binding_key)
                if previous is not None:
                    if previous[0] == command_fingerprint:
                        return self._registered_replay(previous[1])
                    return self._registered_idempotency_conflict(previous[1])
                if self._supplement_operations_state()["intake"] == "closed":
                    return self._registered_rejected("supplement.intake_stopped")
                requests = [
                    record
                    for record in self._store.review_records
                    if record.get("record_type") == "supplement_request"
                    and record.get("request_id") == request_id
                ]
                if len(requests) != 1:
                    error = S02IntakeError(
                        "rejected",
                        "intake.request_not_found",
                        responsible_party="integrator",
                        recovery_action="use_a_visible_open_supplement_request",
                        gate_results=(
                            "identity:verified",
                            "contract:verified",
                            "request_binding:failed",
                        ),
                        adapter_id=envelope.adapter_id,
                        adapter_version=envelope.adapter_version,
                        registration_digest=envelope.registration_digest,
                    )
                    return self._record_registered_disposition(
                        error=error,
                        command_fingerprint=command_fingerprint,
                        binding_key=binding_key,
                        principal=principal,
                        submission=submission,
                        envelope=envelope,
                        command_type="submit_attachment_version",
                    )
                request = requests[0]
                application_id = request["application_id"]
                app = self._store.applications.get(application_id)
                self._require_application_state_authority(app)
                assert app is not None

                def reject(
                    reason_code: str,
                    *,
                    disposition: str = "rejected",
                    responsible_party: str = "integrator",
                    recovery_action: str = "repair_and_resubmit",
                    retryable: bool = False,
                ) -> AdmissionResult:
                    error = S02IntakeError(
                        disposition,
                        reason_code,
                        responsible_party=responsible_party,
                        recovery_action=recovery_action,
                        retryable=retryable,
                        gate_results=(
                            "identity:verified",
                            "contract:verified",
                            "object:verified",
                            "request_binding:failed",
                        ),
                        adapter_id=envelope.adapter_id,
                        adapter_version=envelope.adapter_version,
                        registration_digest=envelope.registration_digest,
                    )
                    return self._record_registered_disposition(
                        error=error,
                        command_fingerprint=command_fingerprint,
                        binding_key=binding_key,
                        principal=principal,
                        submission=submission,
                        envelope=envelope,
                        command_type="submit_attachment_version",
                        application_id=application_id,
                        request_id=request_id,
                    )

                terminal = [
                    record
                    for record in self._store.review_records
                    if record.get("request_id") == request_id
                    and record.get("record_type")
                    in {
                        "supplement_request_fulfilled",
                        "supplement_request_expired",
                        "supplement_request_invalidated",
                    }
                ]
                matching_successors = [
                    record
                    for record in self._store.review_records
                    if record.get("record_type") == "supplement_recovery_successor"
                    and record.get("request_id") == request_id
                    and record.get("stream_id") == envelope.stream_id
                    and record.get("source_revision") == envelope.source_revision
                    and record.get("envelope_fingerprint") == envelope.fingerprint
                ]
                if len(matching_successors) > 1:
                    raise RuntimeError("supplement recovery successor is not unique")
                if matching_successors:
                    existing = self._store.receipts.get(
                        str(matching_successors[0].get("receipt_id"))
                    )
                    if isinstance(existing, AdmissionResult):
                        return self._registered_replay(existing)
                if terminal:
                    matching_terminal = next(
                        (
                            record
                            for record in terminal
                            if record.get("stream_id") == envelope.stream_id
                            and record.get("source_revision")
                            == envelope.source_revision
                            and record.get("envelope_fingerprint")
                            == envelope.fingerprint
                        ),
                        None,
                    )
                    if matching_terminal is not None:
                        existing = self._store.receipts.get(
                            str(matching_terminal.get("receipt_id"))
                        )
                        if isinstance(existing, AdmissionResult):
                            return self._registered_replay(existing)
                recovery_successor: dict[str, Any] | None = None
                if len(terminal) == 1 and terminal[0].get("record_type") == (
                    "supplement_request_fulfilled"
                ):
                    terminal_record = terminal[0]
                    terminal_receipt = self._store.receipts.get(
                        str(terminal_record.get("receipt_id"))
                    )
                    open_recoveries = [
                        event
                        for event in self._store.recovery_events
                        if event.get("kind") == "opened"
                        and event.get("application_id") == application_id
                        and event.get("cycle") == request["cycle"]
                        and event.get("lifecycle_revision")
                        == app.get("lifecycle_revision")
                        and event.get("evidence_revision")
                        == app.get("evidence_revision")
                        and not any(
                            successor.get("recovery_work_id")
                            == event.get("recovery_work_id")
                            and successor.get("kind")
                            in {"resolved", "superseded", "terminated"}
                            for successor in self._store.recovery_events
                        )
                    ]
                    blocked_jobs = [
                        job
                        for job in self._store.jobs
                        if len(open_recoveries) == 1
                        and job.get("job_id") == open_recoveries[0].get("job_id")
                        and job.get("application_id") == application_id
                        and job.get("kind") == "supplement_check"
                        and job.get("request_id") == request_id
                        and job.get("status")
                        in {
                            "blocked",
                            "exhausted",
                            "dead_lettered",
                            "outcome_unknown",
                            "compensation_failed",
                        }
                    ]
                    if (
                        app.get("phase") == "Unprocessable"
                        and app.get("cycle") == request["cycle"]
                        and app.get("current_run_id") is None
                        and len(open_recoveries) == len(blocked_jobs) == 1
                        and terminal_record.get("application_id") == application_id
                        and terminal_record.get("cycle") == request["cycle"]
                        and terminal_record.get("evidence_revision")
                        == app.get("evidence_revision")
                        and isinstance(
                            terminal_record.get("lifecycle_revision"), int
                        )
                        and isinstance(
                            open_recoveries[0].get("pre_block_lifecycle_revision"),
                            int,
                        )
                        and terminal_record["lifecycle_revision"]
                        < open_recoveries[0]["pre_block_lifecycle_revision"]
                        and terminal_record.get("evidence_revision")
                        == open_recoveries[0].get("evidence_revision")
                        and isinstance(terminal_receipt, AdmissionResult)
                        and terminal_receipt.disposition
                        is AdmissionDisposition.ACCEPTED
                        and terminal_receipt.request_id == request_id
                        and terminal_receipt.request_status == "fulfilled"
                        and terminal_receipt.job_id == open_recoveries[0].get("job_id")
                        and terminal_receipt.stream_id == envelope.stream_id
                        and terminal_receipt.source_registration_digest
                        == envelope.registration_digest
                        and terminal_receipt.adapter_id == envelope.adapter_id
                        and terminal_receipt.adapter_version == envelope.adapter_version
                    ):
                        recovery_successor = {
                            "work": open_recoveries[0],
                            "job": blocked_jobs[0],
                            "terminal": terminal_record,
                            "receipt": terminal_receipt,
                        }
                if terminal and recovery_successor is None:
                    return reject(
                        "evidence.late_input_requires_reopen",
                        responsible_party="lifecycle_owner",
                        recovery_action="use_the_later_reopen_contract",
                    )
                authenticated = envelope.payload["envelope"]["authenticated_context"]
                allowed = request["allowed_source_policy"]
                document = envelope.payload["application"]["evidence"][0]
                lineage = envelope.payload["attachment_lineage"]
                batch = envelope.payload["batch"]
                expected_attachment_id = (
                    recovery_successor["terminal"]["attachment_id"]
                    if recovery_successor is not None
                    else request["expected_predecessor_attachment_id"]
                )
                expected_attachment_version = (
                    recovery_successor["terminal"]["attachment_version"]
                    if recovery_successor is not None
                    else request["expected_predecessor_attachment_version"]
                )
                if (
                    authenticated.get("tenant_id") != allowed["tenant_id"]
                    or authenticated.get("source_id")
                    not in allowed["source_system_ids"]
                    or authenticated.get("workload_identity_id")
                    not in allowed["workload_identity_ids"]
                    or envelope.upstream_application_reference
                    != app["upstream_application_reference"]
                    or request_binding.get("request_context_digest")
                    != request["context_digest"]
                    or request_binding.get("material_requirement_id")
                    != request["material_requirement_id"]
                    or batch.get("item_count") != request["batch_item_count"]
                    or document.get("document_role") != request["document_role"]
                    or document.get("document_type") != request["material_kind"]
                    or lineage.get("operation") != request["operation"]
                    or lineage.get("predecessor_attachment_id")
                    != expected_attachment_id
                    or lineage.get("predecessor_attachment_version")
                    != expected_attachment_version
                    or lineage.get("attachment_version")
                    != expected_attachment_version + 1
                ):
                    return reject("intake.request_context_mismatch")
                progress = sorted(
                    (
                        record
                        for record in self._store.review_records
                        if record.get("record_type")
                        == "supplement_request_progress"
                        and record.get("request_id") == request_id
                    ),
                    key=lambda record: int(record["request_progress_revision"]),
                )
                if recovery_successor is None:
                    expected_progress_revision = len(progress) + 1
                    expected_source_revision = len(progress) + 1
                    expected_predecessor = (
                        progress[-1]["source_revision"] if progress else None
                    )
                    expected_batch_sequence = expected_progress_revision
                else:
                    predecessor = recovery_successor["terminal"]
                    expected_progress_revision = (
                        int(predecessor["request_progress_revision"]) + 1
                    )
                    expected_source_revision = int(predecessor["source_revision"]) + 1
                    expected_predecessor = predecessor["source_revision"]
                    expected_batch_sequence = int(request["batch_item_count"])
                same_source_revision = [
                    receipt
                    for receipt in self._accepted_source_stream_receipts(envelope)
                    if receipt.source_revision == envelope.source_revision
                ]
                if any(
                    receipt.envelope_fingerprint == envelope.fingerprint
                    for receipt in same_source_revision
                ):
                    existing = next(
                        receipt
                        for receipt in same_source_revision
                        if receipt.envelope_fingerprint == envelope.fingerprint
                    )
                    return self._registered_replay(existing)
                if same_source_revision:
                    return reject(
                        "intake.source_revision_conflict",
                        disposition="quarantined",
                        responsible_party="source_owner",
                        recovery_action="reconcile_the_source_revision_history",
                    )
                if (
                    request_binding.get("request_progress_revision")
                    != expected_progress_revision
                    or envelope.source_revision != expected_source_revision
                    or envelope.predecessor_revision != expected_predecessor
                    or batch["item_sequence"] != expected_batch_sequence
                    or progress
                    and (
                        progress[0]["batch_id"] != batch["batch_id"]
                        or progress[0]["batch_manifest_digest"]
                        != batch["manifest_digest"]
                    )
                    or recovery_successor is not None
                    and (
                        recovery_successor["terminal"].get("stream_id")
                        != envelope.stream_id
                        or recovery_successor["terminal"].get("batch_id")
                        != batch["batch_id"]
                        or recovery_successor["terminal"].get(
                            "batch_manifest_digest"
                        )
                        != batch["manifest_digest"]
                    )
                ):
                    return reject(
                        "intake.sequence_gap",
                        disposition="awaiting_predecessor",
                        responsible_party="source_owner",
                        recovery_action="submit_and_reconcile_the_declared_predecessor",
                        retryable=True,
                    )
                if (
                    recovery_successor is None
                    and receipt_time >= int(request["due_at"])
                ):
                    if not self.audit_available:
                        return self._registered_rejected("intake.audit_unavailable")
                    if not self.storage_available:
                        return self._registered_rejected("intake.storage_unavailable")
                    receipt_id = self._stable_id(
                        "receipt",
                        f"supplement:expired:{binding_key}:{envelope.fingerprint}",
                    )
                    staged = copy.deepcopy(self._store)
                    staged_app = staged.applications[application_id]
                    try:
                        recovery_target = self._stage_supplement_expiry(
                            staged,
                            request=request,
                            event_time=receipt_time,
                        )
                        result = AdmissionResult(
                            disposition=AdmissionDisposition.REJECTED,
                            application_id=application_id,
                            receipt_id=receipt_id,
                            reason_code="supplement.deadline_reached",
                            lifecycle_revision=staged_app[
                                "lifecycle_revision"
                            ],
                            evidence_revision=staged_app["evidence_revision"],
                            audit_recorded=True,
                            envelope_version=envelope.envelope_version,
                            schema_version=envelope.schema_version,
                            semantic_version=envelope.semantic_version,
                            envelope_id=envelope.envelope_id,
                            stream_id=envelope.stream_id,
                            source_revision_id=self._stable_id(
                                "source_revision",
                                f"{envelope.stream_id}:{envelope.source_revision}:"
                                f"{envelope.fingerprint}",
                            ),
                            batch_id=batch["batch_id"],
                            envelope_fingerprint=envelope.fingerprint,
                            idempotency_identity=binding_key,
                            idempotency_key_digest=hashlib.sha256(
                                idempotency_key.encode("utf-8")
                            ).hexdigest(),
                            adapter_id=envelope.adapter_id,
                            adapter_version=envelope.adapter_version,
                            artifact_manifest_digest=self._manifest.digest,
                            responsible_party=request["responsible_party"],
                            recovery_action=(
                                "create_a_new_current_supplement_request"
                            ),
                            gate_results=(
                                "identity:verified",
                                "contract:verified",
                                "object:verified",
                                "request_binding:verified",
                                "deadline:expired",
                                "idempotency:bound",
                            ),
                            fact_counts={
                                "applications": 0,
                                "receipts": 1,
                                "idempotency_bindings": 1,
                                "lifecycle_events": 1,
                                "evidence_events": 0,
                                "audit_events": 1,
                                "jobs": 0,
                                "outbox_events": 0,
                                "attachments": 0,
                                "pages": 0,
                                "producer_results": 0,
                                "observations": 0,
                            },
                            real_cross_document_opportunities=0,
                            performance_status="not_estimable",
                            source_registration_digest=(
                                envelope.registration_digest
                            ),
                            source_revision=envelope.source_revision,
                            request_id=request_id,
                            request_status="expired",
                            batch_closed=batch["closed"],
                            request_progress_revision=(
                                expected_progress_revision
                            ),
                            attachment_id=(
                                progress[-1]["attachment_id"]
                                if progress
                                else None
                            ),
                            attachment_version=(
                                progress[-1]["attachment_version"]
                                if progress
                                else None
                            ),
                            supersedes_attachment_id=(
                                progress[-1]["supersedes_attachment_id"]
                                if progress
                                else None
                            ),
                            fulfilled=False,
                            phase="Unprocessable",
                            route="unprocessable",
                            recovery_target=recovery_target,
                        )
                        self._before_write("supplement_expiry.receipt")
                        staged.receipts[receipt_id] = result
                        self._append_supplement_expiry_audit(
                            staged,
                            request=request,
                            principal=principal,
                            event_time=receipt_time,
                            recovery_target=recovery_target,
                            receipt_id=receipt_id,
                        )
                        self._before_write("supplement_expiry.idempotency")
                        staged.idempotency[binding_key] = (
                            command_fingerprint,
                            result,
                        )
                        self._before_write("supplement_expiry.publish")
                        staged.persist()
                    except StaleStoreRevision:
                        if attempt == 0:
                            continue
                        return self._registered_rejected(
                            "intake.storage_unavailable"
                        )
                    except _StoreWriteFailure as error:
                        return self._registered_rejected(
                            "intake.audit_unavailable"
                            if str(error) == "supplement_expiry.audit"
                            else "intake.storage_unavailable"
                        )
                    self._store = staged
                    return result
                if recovery_successor is None and (
                    app.get("cycle") != request["cycle"]
                    or app.get("phase") not in {"Supplement", "Awaiting Evidence"}
                    or app.get("evidence_revision")
                    != (
                        request["expected_evidence_revision"]
                        if not progress
                        else progress[-1]["evidence_revision"]
                    )
                    or app.get("current_run_id")
                    not in {request["run_id"], None}
                    or progress
                    and app.get("current_run_id") is not None
                ):
                    return reject(
                        "intake.request_context_stale",
                        responsible_party="lifecycle_owner",
                        recovery_action="request_a_new_current_material_assessment",
                    )
                observations = document.get("observations")
                eligible_vin = [
                    observation
                    for observation in observations
                    if observation.get("field") == "vin"
                    and observation.get("evidence_eligible") is True
                    and observation.get("raw") not in {None, ""}
                ] if isinstance(observations, list) else []
                if (
                    not envelope.provenance_eligible
                    or envelope.attachment_count < 1
                    or len(eligible_vin) != 1
                ):
                    return reject(
                        "evidence.satisfaction_policy_failed",
                        disposition="quarantined",
                        responsible_party="source_owner",
                        recovery_action="submit_verified_required_material",
                    )
                gate = self._review_write_gate(app=app)
                if gate is not None:
                    _, failure = gate
                    if (
                        failure == self._REVIEW_SOURCE_FAILURE
                        and recovery_successor is None
                    ):
                        try:
                            return self._invalidate_supplement_source_dependency(
                                request=request,
                                progress=progress,
                                envelope=envelope,
                                batch=batch,
                                binding_key=binding_key,
                                command_fingerprint=command_fingerprint,
                                idempotency_key=idempotency_key,
                                principal=principal,
                                event_time=receipt_time,
                            )
                        except StaleStoreRevision:
                            if attempt == 0:
                                continue
                            return self._registered_rejected(
                                "intake.storage_unavailable"
                            )
                        except _StoreWriteFailure as error:
                            return self._registered_rejected(
                                "intake.audit_unavailable"
                                if str(error) == "supplement_invalidation.audit"
                                else "intake.storage_unavailable"
                            )
                    return reject(
                        failure,
                        responsible_party="platform_owner",
                        recovery_action="restore_the_protected_write_dependency",
                        retryable=True,
                    )

                evidence = self._admitted_evidence(app)
                predecessor_documents = [
                    candidate
                    for candidate in evidence
                    if isinstance(candidate.get("attachment"), dict)
                    and candidate["attachment"].get("attachment_id")
                    == expected_attachment_id
                    and candidate["attachment"].get("version")
                    == expected_attachment_version
                ]
                if len(predecessor_documents) != 1:
                    return reject(
                        "evidence.predecessor_unavailable",
                        disposition="quarantined",
                        responsible_party="lifecycle_owner",
                        recovery_action="reconcile_the_attachment_lineage",
                    )
                if batch["closed"] and recovery_successor is None:
                    if not progress:
                        return reject(
                            "intake.sequence_gap",
                            disposition="awaiting_predecessor",
                            responsible_party="source_owner",
                            recovery_action="submit_the_declared_request_progress",
                            retryable=True,
                        )
                    current_documents = [
                        candidate
                        for candidate in self._assemble_evidence(evidence)
                        if isinstance(candidate.get("attachment"), dict)
                        and candidate["attachment"].get("attachment_id")
                        == progress[-1]["attachment_id"]
                    ]
                    incoming_pages = envelope.payload["application"]["graph"][
                        "attachments"
                    ]
                    current_vin = [
                        observation
                        for observation in current_documents[0].get(
                            "observations", []
                        )
                        if observation.get("field") == "vin"
                        and observation.get("evidence_eligible") is True
                        and observation.get("raw") not in {None, ""}
                    ] if len(current_documents) == 1 else []
                    current_attachment = (
                        current_documents[0]["attachment"]
                        if len(current_documents) == 1
                        else None
                    )
                    if (
                        not isinstance(current_attachment, dict)
                        or current_attachment.get("version")
                        != lineage["attachment_version"]
                        or not current_attachment.get("page_ids")
                        or not current_attachment.get("producer_result_id")
                        or len(incoming_pages) != 1
                        or current_attachment.get("source_sha256")
                        != incoming_pages[0].get("source_sha256")
                        or len(current_vin) != 1
                        or current_vin[0].get("raw") != eligible_vin[0].get("raw")
                    ):
                        return reject(
                            "evidence.satisfaction_policy_failed",
                            disposition="quarantined",
                            responsible_party="source_owner",
                            recovery_action="submit_verified_required_material",
                        )
                    job_id = self._stable_id(
                        "job", f"{application_id}:supplement:{request_id}"
                    )
                    receipt_id = self._stable_id(
                        "receipt",
                        f"supplement:{binding_key}:{envelope.fingerprint}",
                    )
                    source_revision_id = self._stable_id(
                        "source_revision",
                        f"{envelope.stream_id}:{envelope.source_revision}:"
                        f"{envelope.fingerprint}",
                    )
                    staged = copy.deepcopy(self._store)
                    staged_app = staged.applications[application_id]
                    try:
                        self._before_write("supplement_fulfillment.lifecycle")
                        staged_app["route"] = "pending_check"
                        staged_app["evidence_ready"] = False
                        staged_app["projection_visible"] = False
                        staged_app["projection_pending"] = False
                        self._transition_lifecycle(
                            staged_app,
                            "Assembly",
                            "SUPPLEMENT_REQUEST_FULFILLED",
                            store=staged,
                        )
                        staged.lifecycle_events[-1].update(
                            {
                                "request_id": request_id,
                                "receipt_id": receipt_id,
                                "attachment_id": progress[-1]["attachment_id"],
                                "work_item_id": request["work_item_id"],
                                "job_id": job_id,
                            }
                        )
                        self._before_write("supplement_fulfillment.request")
                        staged.review_records.append(
                            {
                                "record_id": self._stable_id(
                                    "supplement_fulfillment",
                                    f"{request_id}:{envelope.fingerprint}",
                                ),
                                "record_type": "supplement_request_fulfilled",
                                "schema_version": "supplement-request-fulfillment/1",
                                "request_id": request_id,
                                "work_item_id": request["work_item_id"],
                                "application_id": application_id,
                                "cycle": request["cycle"],
                                "status": "fulfilled",
                                "request_progress_revision": (
                                    expected_progress_revision
                                ),
                                "stream_id": envelope.stream_id,
                                "source_revision": envelope.source_revision,
                                "predecessor_revision": envelope.predecessor_revision,
                                "batch_id": batch["batch_id"],
                                "batch_manifest_digest": batch[
                                    "manifest_digest"
                                ],
                                "batch_closed": True,
                                "attachment_id": progress[-1]["attachment_id"],
                                "attachment_version": progress[-1][
                                    "attachment_version"
                                ],
                                "receipt_id": receipt_id,
                                "envelope_id": envelope.envelope_id,
                                "envelope_fingerprint": envelope.fingerprint,
                                "evidence_revision": staged_app[
                                    "evidence_revision"
                                ],
                                "lifecycle_revision": staged_app[
                                    "lifecycle_revision"
                                ],
                                "fulfilled_at": receipt_time,
                            }
                        )
                        self._before_write("supplement_fulfillment.work_item")
                        staged.review_records.append(
                            {
                                "record_id": self._stable_id(
                                    "review_record",
                                    f"{request['work_item_id']}:fulfilled:1",
                                ),
                                "record_type": "supplement_work_item_fulfilled",
                                "sequence": 1,
                                "request_id": request_id,
                                "work_item_id": request["work_item_id"],
                                "application_id": application_id,
                                "attachment_id": progress[-1]["attachment_id"],
                                "fulfilled_at": receipt_time,
                                "recorded_at": receipt_time,
                            }
                        )
                        self._before_write("supplement_fulfillment.job")
                        supplement_job = self._admission_job_record(
                            job_id, application_id, envelope.fingerprint
                        )
                        supplement_job.update(
                            {
                                "kind": "supplement_check",
                                "request_id": request_id,
                            }
                        )
                        staged.jobs.append(supplement_job)
                        self._before_write("supplement_fulfillment.outbox")
                        staged.outbox.append(
                            {
                                "event_id": self._stable_id("outbox", job_id),
                                "kind": "controlled_check_requested",
                                "application_id": application_id,
                                "job_id": job_id,
                                "fingerprint": envelope.fingerprint,
                                "request_id": request_id,
                                "status": "pending",
                            }
                        )
                        result = AdmissionResult(
                            disposition=AdmissionDisposition.ACCEPTED,
                            application_id=application_id,
                            receipt_id=receipt_id,
                            job_id=job_id,
                            reason_code="request_fulfilled",
                            replayed=False,
                            lifecycle_revision=staged_app[
                                "lifecycle_revision"
                            ],
                            evidence_revision=staged_app["evidence_revision"],
                            audit_recorded=True,
                            envelope_version=envelope.envelope_version,
                            schema_version=envelope.schema_version,
                            semantic_version=envelope.semantic_version,
                            envelope_id=envelope.envelope_id,
                            stream_id=envelope.stream_id,
                            source_revision_id=source_revision_id,
                            batch_id=batch["batch_id"],
                            envelope_fingerprint=envelope.fingerprint,
                            idempotency_identity=binding_key,
                            idempotency_key_digest=hashlib.sha256(
                                idempotency_key.encode("utf-8")
                            ).hexdigest(),
                            adapter_id=envelope.adapter_id,
                            adapter_version=envelope.adapter_version,
                            artifact_manifest_digest=self._manifest.digest,
                            gate_results=(
                                "identity:verified",
                                "contract:verified",
                                "object:verified",
                                "provenance:verified",
                                "request_binding:verified",
                                "batch:closed",
                                "idempotency:bound",
                            ),
                            fact_counts={
                                "applications": 0,
                                "receipts": 1,
                                "idempotency_bindings": 1,
                                "lifecycle_events": 1,
                                "evidence_events": 0,
                                "audit_events": 1,
                                "jobs": 1,
                                "outbox_events": 1,
                                "attachments": 0,
                                "pages": 0,
                                "producer_results": 0,
                                "observations": 0,
                            },
                            real_cross_document_opportunities=0,
                            performance_status="not_estimable",
                            source_registration_digest=(
                                envelope.registration_digest
                            ),
                            source_revision=envelope.source_revision,
                            request_id=request_id,
                            request_status="fulfilled",
                            batch_closed=True,
                            request_progress_revision=(
                                expected_progress_revision
                            ),
                            attachment_id=progress[-1]["attachment_id"],
                            attachment_version=progress[-1][
                                "attachment_version"
                            ],
                            supersedes_attachment_id=progress[-1][
                                "supersedes_attachment_id"
                            ],
                            fulfilled=True,
                            phase="Assembly",
                            route="pending_check",
                        )
                        self._before_write("supplement_fulfillment.receipt")
                        staged.receipts[receipt_id] = result
                        self._before_write("supplement_fulfillment.audit")
                        staged.audit_events.append(
                            {
                                "event_id": self._stable_id(
                                    "audit", f"supplement_fulfilled:{receipt_id}"
                                ),
                                "action": "supplement_request_fulfilled",
                                "subject": principal.subject,
                                "role": principal.role,
                                "scope": principal.scope,
                                "source_id": principal.source_id,
                                "application_id": application_id,
                                "request_id": request_id,
                                "receipt_id": receipt_id,
                                "attachment_id": progress[-1][
                                    "attachment_id"
                                ],
                                "batch_id": batch["batch_id"],
                                "batch_closed": True,
                                "job_id": job_id,
                                "lifecycle_revision": staged_app[
                                    "lifecycle_revision"
                                ],
                                "evidence_revision": staged_app[
                                    "evidence_revision"
                                ],
                                "result": "accepted",
                                **self._audit_time_fields(
                                    staged, now=receipt_time
                                ),
                            }
                        )
                        self._before_write("supplement_fulfillment.idempotency")
                        staged.idempotency[binding_key] = (
                            command_fingerprint,
                            result,
                        )
                        self._before_write("supplement_fulfillment.publish")
                        staged.persist()
                    except StaleStoreRevision:
                        if attempt == 0:
                            continue
                        return self._registered_rejected(
                            "intake.storage_unavailable"
                        )
                    except _StoreWriteFailure as error:
                        return self._registered_rejected(
                            "intake.audit_unavailable"
                            if str(error) == "supplement_fulfillment.audit"
                            else "intake.storage_unavailable"
                        )
                    except Exception:
                        return self._registered_rejected(
                            "intake.storage_unavailable"
                        )
                    self._store = staged
                    return result
                attachment_id = self._stable_id(
                    "attachment", f"{request_id}:{envelope.fingerprint}"
                )
                producer_result = copy.deepcopy(
                    envelope.payload["application"]["graph"]["producer_result"]
                )
                producer_result_id = self._stable_id(
                    "producer_result", f"{request_id}:{envelope.fingerprint}"
                )
                producer_result.update(
                    {
                        "producer_result_id": producer_result_id,
                        "version": 1,
                    }
                )
                pages = []
                for page in envelope.payload["application"]["graph"]["attachments"]:
                    pages.append(
                        {
                            **copy.deepcopy(page),
                            "page_id": self._stable_id(
                                "page",
                                f"{attachment_id}:{page['page_ref']}:{page['source_sha256']}",
                            ),
                            "version": 1,
                        }
                    )
                document_id = self._stable_id(
                    "document", f"{request_id}:{attachment_id}"
                )
                successor_observations = []
                successor_fields: dict[str, dict[str, Any]] = {}
                for observation in observations:
                    successor = {
                        **copy.deepcopy(observation),
                        "document_id": document_id,
                        "document_role": request["target_document_role"],
                        "version": 1,
                        "producer_result_id": producer_result_id,
                    }
                    field_name = successor.get("field")
                    if (
                        not isinstance(field_name, str)
                        or not field_name
                        or field_name in successor_fields
                    ):
                        return reject(
                            "evidence.satisfaction_policy_failed",
                            disposition="quarantined",
                            responsible_party="source_owner",
                            recovery_action="submit_unambiguous_field_observations",
                        )
                    successor_observations.append(successor)
                    successor_fields[field_name] = copy.deepcopy(successor)
                successor_document = {
                    "document_id": document_id,
                    "document_type": request["material_kind"],
                    "document_role": request["target_document_role"],
                    "fields": successor_fields,
                    "observations": successor_observations,
                    "attachment": {
                        "attachment_id": attachment_id,
                        "version": lineage["attachment_version"],
                        "supersedes_attachment_id": lineage[
                            "predecessor_attachment_id"
                        ],
                        "supersedes_attachment_version": lineage[
                            "predecessor_attachment_version"
                        ],
                        "source_attachment_ref": pages[0]["attachment_ref"],
                        "source_object_ref": pages[0]["source_object_ref"],
                        "source_sha256": pages[0]["source_sha256"],
                        "media_type": pages[0]["media_type"],
                        "producer_result_id": producer_result_id,
                        "page_ids": [page["page_id"] for page in pages],
                    },
                    "pages": pages,
                    "producer_result": producer_result,
                }
                evidence.append(successor_document)
                next_evidence_revision = int(app["evidence_revision"]) + 1
                evidence_payload = {
                    "schema_version": "s06-supplement-evidence/1",
                    "evidence": evidence,
                    "request_id": request_id,
                    "request_progress_revision": expected_progress_revision,
                    "attachment_id": attachment_id,
                    "supersedes_attachment_id": lineage[
                        "predecessor_attachment_id"
                    ],
                    "batch": copy.deepcopy(batch),
                }
                if recovery_successor is not None:
                    evidence_payload["supersedes_recovery_work_id"] = (
                        recovery_successor["work"]["recovery_work_id"]
                    )
                evidence_bytes = json.dumps(
                    evidence_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                receipt_id = self._stable_id(
                    "receipt", f"supplement:{binding_key}:{envelope.fingerprint}"
                )
                source_revision_id = self._stable_id(
                    "source_revision",
                    f"{envelope.stream_id}:{envelope.source_revision}:{envelope.fingerprint}",
                )
                successor_job_id = (
                    self._stable_id(
                        "job",
                        f"{application_id}:supplement-recovery:{receipt_id}",
                    )
                    if recovery_successor is not None
                    else None
                )
                staged = copy.deepcopy(self._store)
                staged_app = staged.applications[application_id]
                old_route = staged_app["route"]
                try:
                    self._before_write("supplement_progress.evidence")
                    staged.evidence_events.append(
                        {
                            "event_id": self._stable_id(
                                "evidence",
                                f"{application_id}:supplement:{request_id}:"
                                f"{expected_progress_revision}",
                            ),
                            "application_id": application_id,
                            "revision": next_evidence_revision,
                            "kind": "supplement_attachment_version",
                            "content_sha256": hashlib.sha256(
                                evidence_bytes
                            ).hexdigest(),
                            "content_bytes": len(evidence_bytes),
                            "payload": evidence_payload,
                        }
                    )
                    staged_app["evidence_revision"] = next_evidence_revision
                    staged_app["evidence_ready"] = False
                    staged_app["route"] = (
                        "pending_check"
                        if recovery_successor is not None
                        else "awaiting_evidence"
                    )
                    staged_app["current_run_id"] = None
                    staged_app["current_evidence_snapshot_id"] = None
                    staged_app["current_evidence_snapshot_digest"] = None
                    staged_app["projection_visible"] = False
                    staged_app["projection_pending"] = False
                    self._before_write("supplement_progress.lifecycle")
                    self._transition_lifecycle(
                        staged_app,
                        "Assembly",
                        (
                            "RECOVERY_CONTEXT_SUCCESSOR_ACCEPTED"
                            if recovery_successor is not None
                            else "SUPPLEMENT_PROGRESS_ACCEPTED"
                        ),
                        store=staged,
                    )
                    staged.lifecycle_events[-1].update(
                        {
                            "request_id": request_id,
                            "receipt_id": receipt_id,
                            "attachment_id": attachment_id,
                            "invalidated_run_id": request["run_id"],
                            "invalidated_route": old_route,
                            "invalidated_work_item_id": request[
                                "source_work_item_id"
                            ],
                            **(
                                {
                                    "recovery_work_id": recovery_successor[
                                        "work"
                                    ]["recovery_work_id"],
                                    "superseded_recovery_target": recovery_successor[
                                        "work"
                                    ]["recovery_target"],
                                    "job_id": successor_job_id,
                                }
                                if recovery_successor is not None
                                else {}
                            ),
                        }
                    )
                    if recovery_successor is None:
                        self._transition_lifecycle(
                            staged_app,
                            "Awaiting Evidence",
                            "SUPPLEMENT_BATCH_OPEN",
                            store=staged,
                        )
                        staged.lifecycle_events[-1].update(
                            {
                                "request_id": request_id,
                                "receipt_id": receipt_id,
                                "batch_id": batch["batch_id"],
                            }
                        )
                    else:
                        recovery_work_id = recovery_successor["work"][
                            "recovery_work_id"
                        ]
                        staged.recovery_events.append(
                            {
                                "event_id": self._stable_id(
                                    "recovery_event",
                                    f"{recovery_work_id}:superseded:{receipt_id}",
                                ),
                                "kind": "superseded",
                                "schema_version": "recovery-work/1",
                                "recovery_work_id": recovery_work_id,
                                "application_id": application_id,
                                "successor_kind": "supplement_attachment_version",
                                "successor_receipt_id": receipt_id,
                                "successor_evidence_revision": next_evidence_revision,
                                "superseded_at": receipt_time,
                                "successor_target": "Assembly",
                            }
                        )
                        assert successor_job_id is not None
                        self._before_write("supplement_progress.job")
                        staged.jobs.append(
                            self._supplement_job_record(
                                successor_job_id,
                                application_id,
                                request_id,
                                envelope.fingerprint,
                            )
                        )
                        self._before_write("supplement_progress.outbox")
                        staged.outbox.append(
                            {
                                "event_id": self._stable_id(
                                    "outbox", successor_job_id
                                ),
                                "kind": "controlled_check_requested",
                                "application_id": application_id,
                                "job_id": successor_job_id,
                                "fingerprint": envelope.fingerprint,
                                "request_id": request_id,
                                "status": "pending",
                            }
                        )
                    result = AdmissionResult(
                        disposition=AdmissionDisposition.ACCEPTED,
                        application_id=application_id,
                        receipt_id=receipt_id,
                        job_id=successor_job_id,
                        reason_code=(
                            "request_fulfilled"
                            if recovery_successor is not None
                            else "request_progress_accepted"
                        ),
                        replayed=False,
                        lifecycle_revision=staged_app["lifecycle_revision"],
                        evidence_revision=next_evidence_revision,
                        audit_recorded=True,
                        envelope_version=envelope.envelope_version,
                        schema_version=envelope.schema_version,
                        semantic_version=envelope.semantic_version,
                        envelope_id=envelope.envelope_id,
                        stream_id=envelope.stream_id,
                        source_revision_id=source_revision_id,
                        batch_id=batch["batch_id"],
                        envelope_fingerprint=envelope.fingerprint,
                        idempotency_identity=binding_key,
                        idempotency_key_digest=hashlib.sha256(
                            idempotency_key.encode("utf-8")
                        ).hexdigest(),
                        adapter_id=envelope.adapter_id,
                        adapter_version=envelope.adapter_version,
                        artifact_manifest_digest=self._manifest.digest,
                        gate_results=(
                            "identity:verified",
                            "contract:verified",
                            "object:verified",
                            "provenance:verified",
                            "request_binding:verified",
                            *(
                                ("recovery_context:verified",)
                                if recovery_successor is not None
                                else ()
                            ),
                            "idempotency:bound",
                        ),
                        fact_counts={
                            "applications": 0,
                            "receipts": 1,
                            "idempotency_bindings": 1,
                            "lifecycle_events": (
                                1 if recovery_successor is not None else 2
                            ),
                            "evidence_events": 1,
                            "audit_events": 1,
                            "jobs": 1 if recovery_successor is not None else 0,
                            "outbox_events": (
                                1 if recovery_successor is not None else 0
                            ),
                            "attachments": 1,
                            "pages": len(pages),
                            "producer_results": 1,
                            "observations": len(successor_observations),
                        },
                        real_cross_document_opportunities=0,
                        performance_status="not_estimable",
                        source_registration_digest=envelope.registration_digest,
                        source_revision=envelope.source_revision,
                        request_id=request_id,
                        request_status=(
                            "fulfilled"
                            if recovery_successor is not None
                            else "open"
                        ),
                        batch_closed=recovery_successor is not None,
                        request_progress_revision=expected_progress_revision,
                        attachment_id=attachment_id,
                        attachment_version=lineage["attachment_version"],
                        supersedes_attachment_id=lineage[
                            "predecessor_attachment_id"
                        ],
                        fulfilled=recovery_successor is not None,
                        phase=(
                            "Assembly"
                            if recovery_successor is not None
                            else "Awaiting Evidence"
                        ),
                        route=(
                            "pending_check"
                            if recovery_successor is not None
                            else "awaiting_evidence"
                        ),
                    )
                    self._before_write("supplement_progress.request")
                    staged.review_records.append(
                        {
                            "record_id": self._stable_id(
                                (
                                    "supplement_recovery_successor"
                                    if recovery_successor is not None
                                    else "supplement_progress"
                                ),
                                f"{request_id}:{expected_progress_revision}:"
                                f"{envelope.fingerprint}",
                            ),
                            "record_type": (
                                "supplement_recovery_successor"
                                if recovery_successor is not None
                                else "supplement_request_progress"
                            ),
                            "schema_version": (
                                "supplement-recovery-successor/1"
                                if recovery_successor is not None
                                else "supplement-request-progress/1"
                            ),
                            "request_id": request_id,
                            "work_item_id": request["work_item_id"],
                            "application_id": application_id,
                            "cycle": request["cycle"],
                            "request_progress_revision": expected_progress_revision,
                            "stream_id": envelope.stream_id,
                            "source_revision": envelope.source_revision,
                            "predecessor_revision": envelope.predecessor_revision,
                            "batch_id": batch["batch_id"],
                            "batch_manifest_digest": batch["manifest_digest"],
                            "batch_item_sequence": batch["item_sequence"],
                            "batch_closed": recovery_successor is not None,
                            "attachment_id": attachment_id,
                            "attachment_version": lineage["attachment_version"],
                            "supersedes_attachment_id": lineage[
                                "predecessor_attachment_id"
                            ],
                            "receipt_id": receipt_id,
                            "envelope_id": envelope.envelope_id,
                            "envelope_fingerprint": envelope.fingerprint,
                            "evidence_revision": next_evidence_revision,
                            "lifecycle_revision": staged_app[
                                "lifecycle_revision"
                            ],
                            "recorded_at": receipt_time,
                            **(
                                {
                                    "recovery_work_id": recovery_successor[
                                        "work"
                                    ]["recovery_work_id"],
                                    "predecessor_receipt_id": recovery_successor[
                                        "terminal"
                                    ]["receipt_id"],
                                    "job_id": successor_job_id,
                                }
                                if recovery_successor is not None
                                else {}
                            ),
                        }
                    )
                    self._before_write("supplement_progress.receipt")
                    staged.receipts[receipt_id] = result
                    self._before_write("supplement_progress.audit")
                    staged.audit_events.append(
                        {
                            "event_id": self._stable_id(
                                "audit",
                                (
                                    f"supplement_recovery_successor:{receipt_id}"
                                    if recovery_successor is not None
                                    else f"supplement_progress:{receipt_id}"
                                ),
                            ),
                            "action": (
                                "supplement_recovery_successor"
                                if recovery_successor is not None
                                else "supplement_request_progress"
                            ),
                            "subject": principal.subject,
                            "role": principal.role,
                            "scope": principal.scope,
                            "source_id": principal.source_id,
                            "application_id": application_id,
                            "request_id": request_id,
                            "receipt_id": receipt_id,
                            "attachment_id": attachment_id,
                            "supersedes_attachment_id": lineage[
                                "predecessor_attachment_id"
                            ],
                            "batch_id": batch["batch_id"],
                            "batch_closed": recovery_successor is not None,
                            "request_progress_revision": expected_progress_revision,
                            "lifecycle_revision": staged_app[
                                "lifecycle_revision"
                            ],
                            "evidence_revision": next_evidence_revision,
                            "result": "accepted",
                            **(
                                {
                                    "recovery_work_id": recovery_successor[
                                        "work"
                                    ]["recovery_work_id"],
                                    "superseded_recovery_target": recovery_successor[
                                        "work"
                                    ]["recovery_target"],
                                    "job_id": successor_job_id,
                                }
                                if recovery_successor is not None
                                else {}
                            ),
                            **self._audit_time_fields(staged, now=receipt_time),
                        }
                    )
                    self._before_write("supplement_progress.idempotency")
                    staged.idempotency[binding_key] = (
                        command_fingerprint,
                        result,
                    )
                    self._before_write("supplement_progress.publish")
                    staged.persist()
                except StaleStoreRevision:
                    if attempt == 0:
                        continue
                    return self._registered_rejected("intake.storage_unavailable")
                except _StoreWriteFailure as error:
                    return self._registered_rejected(
                        "intake.audit_unavailable"
                        if str(error) == "supplement_progress.audit"
                        else "intake.storage_unavailable"
                    )
                except Exception:
                    return self._registered_rejected("intake.storage_unavailable")
                self._store = staged
                return result
            return self._registered_rejected("intake.storage_unavailable")

    def _stage_supplement_expiry(
        self,
        staged: _TargetStore,
        *,
        request: dict[str, Any],
        event_time: int,
    ) -> dict[str, Any]:
        request_id = request["request_id"]
        application_id = request["application_id"]
        recovery_target = {
            "kind": "supplement_request",
            "request_id": request_id,
            "cycle": request["cycle"],
            "due_at": request["due_at"],
        }
        staged_app = staged.applications[application_id]
        self._before_write("supplement_expiry.lifecycle")
        staged_app["evidence_ready"] = False
        staged_app["route"] = "unprocessable"
        staged_app["current_run_id"] = None
        staged_app["current_evidence_snapshot_id"] = None
        staged_app["current_evidence_snapshot_digest"] = None
        staged_app["projection_visible"] = False
        staged_app["projection_pending"] = False
        self._transition_lifecycle(
            staged_app,
            "Unprocessable",
            "SUPPLEMENT_DEADLINE_REACHED",
            store=staged,
        )
        staged.lifecycle_events[-1].update(
            {
                "request_id": request_id,
                "work_item_id": request["work_item_id"],
                "reason_code": "supplement.deadline_reached",
                "responsible_party": request["responsible_party"],
                "recovery_action": "create_a_new_current_supplement_request",
                "recovery_target": copy.deepcopy(recovery_target),
                "invalidated_run_id": request["run_id"],
            }
        )
        self._before_write("supplement_expiry.request")
        staged.review_records.append(
            {
                "record_id": self._stable_id(
                    "supplement_expiry",
                    f"{request_id}:{request['due_at']}",
                ),
                "record_type": "supplement_request_expired",
                "schema_version": "supplement-request-expiry/1",
                "request_id": request_id,
                "work_item_id": request["work_item_id"],
                "application_id": application_id,
                "cycle": request["cycle"],
                "status": "expired",
                "reason_code": "supplement.deadline_reached",
                "responsible_party": request["responsible_party"],
                "recovery_action": "create_a_new_current_supplement_request",
                "recovery_target": copy.deepcopy(recovery_target),
                "due_at": request["due_at"],
                "expired_at": event_time,
                "lifecycle_revision": staged_app["lifecycle_revision"],
                "evidence_revision": staged_app["evidence_revision"],
            }
        )
        self._before_write("supplement_expiry.work_item")
        staged.review_records.append(
            {
                "record_id": self._stable_id(
                    "review_record",
                    f"{request['work_item_id']}:expired:1",
                ),
                "record_type": "supplement_work_item_expired",
                "sequence": 1,
                "request_id": request_id,
                "work_item_id": request["work_item_id"],
                "application_id": application_id,
                "expired_at": event_time,
                "recorded_at": event_time,
            }
        )
        return recovery_target

    def _append_supplement_expiry_audit(
        self,
        staged: _TargetStore,
        *,
        request: dict[str, Any],
        principal: S01CommandPrincipal,
        event_time: int,
        recovery_target: dict[str, Any],
        receipt_id: str | None,
    ) -> None:
        self._before_write("supplement_expiry.audit")
        event = {
            "event_id": self._stable_id(
                "audit", f"supplement_expired:{request['request_id']}"
            ),
            "action": "supplement_request_expired",
            "subject": principal.subject,
            "role": principal.role,
            "scope": principal.scope,
            "source_id": principal.source_id,
            "application_id": request["application_id"],
            "request_id": request["request_id"],
            "work_item_id": request["work_item_id"],
            "reason_code": "supplement.deadline_reached",
            "responsible_party": request["responsible_party"],
            "recovery_action": "create_a_new_current_supplement_request",
            "recovery_target": copy.deepcopy(recovery_target),
            "lifecycle_revision": staged.applications[request["application_id"]][
                "lifecycle_revision"
            ],
            "evidence_revision": staged.applications[request["application_id"]][
                "evidence_revision"
            ],
            "result": "expired",
            **self._audit_time_fields(staged, now=event_time),
        }
        if receipt_id is not None:
            event["receipt_id"] = receipt_id
        staged.audit_events.append(event)

    def expire_due_supplement_requests(
        self,
        *,
        principal: S01CommandPrincipal,
        now: float | None = None,
    ) -> dict[str, Any]:
        event_time = int(self._clock() if now is None else now)
        if not self._valid_exception_router_principal(principal, now=event_time):
            raise QueryNotFound("supplement_deadlines")
        with self._lock:
            for attempt in range(2):
                self._reload_store()
                terminal_request_ids = {
                    record["request_id"]
                    for record in self._store.review_records
                    if record.get("record_type")
                    in {
                        "supplement_request_fulfilled",
                        "supplement_request_expired",
                        "supplement_request_invalidated",
                    }
                }
                due = sorted(
                    (
                        record
                        for record in self._store.review_records
                        if record.get("record_type") == "supplement_request"
                        and record.get("request_id") not in terminal_request_ids
                        and int(record["due_at"]) <= event_time
                    ),
                    key=lambda record: (int(record["due_at"]), record["request_id"]),
                )
                if not due:
                    return {
                        "status": "accepted",
                        "expired_request_ids": [],
                        "expired_count": 0,
                    }
                if not self.audit_available:
                    return {
                        "status": "unavailable",
                        "expired_request_ids": [],
                        "expired_count": 0,
                        "reason_code": "AUDIT_UNAVAILABLE",
                    }
                if not self.storage_available:
                    return {
                        "status": "unavailable",
                        "expired_request_ids": [],
                        "expired_count": 0,
                        "reason_code": "STORAGE_UNAVAILABLE",
                    }
                staged = copy.deepcopy(self._store)
                try:
                    for request in due:
                        app = staged.applications.get(request["application_id"])
                        progress = [
                            record
                            for record in staged.review_records
                            if record.get("record_type")
                            == "supplement_request_progress"
                            and record.get("request_id") == request["request_id"]
                        ]
                        expected_evidence_revision = (
                            max(
                                progress,
                                key=lambda record: int(
                                    record["request_progress_revision"]
                                ),
                            )["evidence_revision"]
                            if progress
                            else request["expected_evidence_revision"]
                        )
                        if (
                            not isinstance(app, dict)
                            or app.get("cycle") != request["cycle"]
                            or app.get("phase")
                            not in {"Supplement", "Awaiting Evidence"}
                            or app.get("evidence_revision")
                            != expected_evidence_revision
                        ):
                            return {
                                "status": "unavailable",
                                "expired_request_ids": [],
                                "expired_count": 0,
                                "reason_code": "STALE_SUPPLEMENT_CONTEXT",
                            }
                        recovery_target = self._stage_supplement_expiry(
                            staged,
                            request=request,
                            event_time=event_time,
                        )
                        self._append_supplement_expiry_audit(
                            staged,
                            request=request,
                            principal=principal,
                            event_time=event_time,
                            recovery_target=recovery_target,
                            receipt_id=None,
                        )
                    self._before_write("supplement_expiry.publish")
                    staged.persist()
                except StaleStoreRevision:
                    if attempt == 0:
                        continue
                    return {
                        "status": "unavailable",
                        "expired_request_ids": [],
                        "expired_count": 0,
                        "reason_code": "STORAGE_UNAVAILABLE",
                    }
                except _StoreWriteFailure as error:
                    return {
                        "status": "unavailable",
                        "expired_request_ids": [],
                        "expired_count": 0,
                        "reason_code": (
                            "AUDIT_UNAVAILABLE"
                            if str(error) == "supplement_expiry.audit"
                            else "STORAGE_UNAVAILABLE"
                        ),
                    }
                self._store = staged
                return {
                    "status": "accepted",
                    "expired_request_ids": [
                        request["request_id"] for request in due
                    ],
                    "expired_count": len(due),
                }
        return {
            "status": "unavailable",
            "expired_request_ids": [],
            "expired_count": 0,
            "reason_code": "STORAGE_UNAVAILABLE",
        }

    def _invalidate_supplement_source_dependency(
        self,
        *,
        request: dict[str, Any],
        progress: list[dict[str, Any]],
        envelope: Any,
        batch: dict[str, Any],
        binding_key: str,
        command_fingerprint: str,
        idempotency_key: str,
        principal: S01CommandPrincipal,
        event_time: int,
    ) -> AdmissionResult:
        request_id = request["request_id"]
        application_id = request["application_id"]
        reason_code = "supplement.source_evidence_unavailable"
        recovery_action = "restore_source_evidence_and_create_a_new_request"
        recovery_target = {
            "kind": "source_evidence",
            "application_id": application_id,
            "request_id": request_id,
            "cycle": request["cycle"],
        }
        receipt_id = self._stable_id(
            "receipt",
            f"supplement:invalidated:{binding_key}:{envelope.fingerprint}",
        )
        staged = copy.deepcopy(self._store)
        staged_app = staged.applications[application_id]
        self._before_write("supplement_invalidation.lifecycle")
        staged_app["evidence_ready"] = False
        staged_app["route"] = "unprocessable"
        staged_app["current_run_id"] = None
        staged_app["current_evidence_snapshot_id"] = None
        staged_app["current_evidence_snapshot_digest"] = None
        staged_app["projection_visible"] = False
        staged_app["projection_pending"] = False
        self._transition_lifecycle(
            staged_app,
            "Unprocessable",
            "SUPPLEMENT_SOURCE_EVIDENCE_UNAVAILABLE",
            store=staged,
        )
        staged.lifecycle_events[-1].update(
            {
                "request_id": request_id,
                "work_item_id": request["work_item_id"],
                "reason_code": reason_code,
                "responsible_party": "platform_owner",
                "recovery_action": recovery_action,
                "recovery_target": copy.deepcopy(recovery_target),
                "invalidated_run_id": request["run_id"],
            }
        )
        self._before_write("supplement_invalidation.request")
        staged.review_records.append(
            {
                "record_id": self._stable_id(
                    "supplement_invalidation",
                    f"{request_id}:source_evidence:{event_time}",
                ),
                "record_type": "supplement_request_invalidated",
                "schema_version": "supplement-request-invalidation/1",
                "request_id": request_id,
                "work_item_id": request["work_item_id"],
                "application_id": application_id,
                "cycle": request["cycle"],
                "status": "invalidated",
                "reason_code": reason_code,
                "responsible_party": "platform_owner",
                "recovery_action": recovery_action,
                "recovery_target": copy.deepcopy(recovery_target),
                "invalidated_at": event_time,
                "lifecycle_revision": staged_app["lifecycle_revision"],
                "evidence_revision": staged_app["evidence_revision"],
            }
        )
        self._before_write("supplement_invalidation.work_item")
        staged.review_records.append(
            {
                "record_id": self._stable_id(
                    "review_record",
                    f"{request['work_item_id']}:invalidated:1",
                ),
                "record_type": "supplement_work_item_invalidated",
                "sequence": 1,
                "request_id": request_id,
                "work_item_id": request["work_item_id"],
                "application_id": application_id,
                "invalidated_at": event_time,
                "recorded_at": event_time,
            }
        )
        result = AdmissionResult(
            disposition=AdmissionDisposition.REJECTED,
            application_id=application_id,
            receipt_id=receipt_id,
            reason_code=reason_code,
            lifecycle_revision=staged_app["lifecycle_revision"],
            evidence_revision=staged_app["evidence_revision"],
            audit_recorded=True,
            envelope_version=envelope.envelope_version,
            schema_version=envelope.schema_version,
            semantic_version=envelope.semantic_version,
            envelope_id=envelope.envelope_id,
            stream_id=envelope.stream_id,
            source_revision_id=self._stable_id(
                "source_revision",
                f"{envelope.stream_id}:{envelope.source_revision}:"
                f"{envelope.fingerprint}",
            ),
            batch_id=batch["batch_id"],
            envelope_fingerprint=envelope.fingerprint,
            idempotency_identity=binding_key,
            idempotency_key_digest=hashlib.sha256(
                idempotency_key.encode("utf-8")
            ).hexdigest(),
            adapter_id=envelope.adapter_id,
            adapter_version=envelope.adapter_version,
            artifact_manifest_digest=self._manifest.digest,
            responsible_party="platform_owner",
            recovery_action=recovery_action,
            gate_results=(
                "identity:verified",
                "contract:verified",
                "object:verified",
                "request_binding:verified",
                "source_evidence:unavailable",
                "idempotency:bound",
            ),
            fact_counts={
                "applications": 0,
                "receipts": 1,
                "idempotency_bindings": 1,
                "lifecycle_events": 1,
                "evidence_events": 0,
                "audit_events": 1,
                "jobs": 0,
                "outbox_events": 0,
                "attachments": 0,
                "pages": 0,
                "producer_results": 0,
                "observations": 0,
            },
            real_cross_document_opportunities=0,
            performance_status="not_estimable",
            source_registration_digest=envelope.registration_digest,
            source_revision=envelope.source_revision,
            request_id=request_id,
            request_status="invalidated",
            batch_closed=batch["closed"],
            request_progress_revision=len(progress) + 1,
            attachment_id=progress[-1]["attachment_id"] if progress else None,
            attachment_version=(
                progress[-1]["attachment_version"] if progress else None
            ),
            supersedes_attachment_id=(
                progress[-1]["supersedes_attachment_id"] if progress else None
            ),
            fulfilled=False,
            phase="Unprocessable",
            route="unprocessable",
            recovery_target=recovery_target,
        )
        self._before_write("supplement_invalidation.receipt")
        staged.receipts[receipt_id] = result
        self._before_write("supplement_invalidation.audit")
        staged.audit_events.append(
            {
                "event_id": self._stable_id(
                    "audit",
                    f"supplement_invalidated:{request_id}:source_evidence",
                ),
                "action": "supplement_request_invalidated",
                "subject": principal.subject,
                "role": principal.role,
                "scope": principal.scope,
                "source_id": principal.source_id,
                "application_id": application_id,
                "request_id": request_id,
                "work_item_id": request["work_item_id"],
                "receipt_id": receipt_id,
                "reason_code": reason_code,
                "responsible_party": "platform_owner",
                "recovery_action": recovery_action,
                "recovery_target": copy.deepcopy(recovery_target),
                "lifecycle_revision": staged_app["lifecycle_revision"],
                "evidence_revision": staged_app["evidence_revision"],
                "result": "invalidated",
                **self._audit_time_fields(staged, now=event_time),
            }
        )
        self._before_write("supplement_invalidation.idempotency")
        staged.idempotency[binding_key] = (command_fingerprint, result)
        self._before_write("supplement_invalidation.publish")
        staged.persist()
        self._store = staged
        return result

    def _supplement_operations_state(
        self, store: _TargetStore | None = None
    ) -> dict[str, Any]:
        owner = self._store if store is None else store
        records = sorted(
            (
                record
                for record in owner.review_records
                if record.get("record_type") == "supplement_operations_changed"
            ),
            key=lambda record: int(record.get("sequence", 0)),
        )
        if [record.get("sequence") for record in records] != list(
            range(1, len(records) + 1)
        ):
            raise RuntimeError("supplement operations authority is not contiguous")
        state: dict[str, Any] = {
            "requests": "open",
            "intake": "open",
            "workers": "open",
            "revision": 0,
            "changed_at": None,
        }
        for record in records:
            if (
                record.get("requests") not in {"open", "closed"}
                or record.get("intake") not in {"open", "closed"}
                or record.get("workers") not in {"open", "fenced"}
                or record.get("revision") != record.get("sequence")
                or isinstance(record.get("changed_at"), bool)
                or not isinstance(record.get("changed_at"), (int, float))
            ):
                raise RuntimeError("supplement operations revision is invalid")
            state.update(
                {
                    "requests": record["requests"],
                    "intake": record["intake"],
                    "workers": record["workers"],
                    "revision": record["revision"],
                    "changed_at": record.get("changed_at"),
                }
            )
        return state

    def supplement_operations_status(
        self,
        *,
        principal: S01CommandPrincipal,
        now: float | None = None,
    ) -> dict[str, Any]:
        event_time = int(self._clock() if now is None else now)
        if not self._valid_exception_router_principal(principal, now=event_time):
            raise QueryNotFound("supplement_operations")
        with self._lock:
            self._reload_store()
            state = self._supplement_operations_state()
            open_requests = {
                record["request_id"]
                for record in self._store.review_records
                if record.get("record_type") == "supplement_request"
            }
            for record in self._store.review_records:
                if record.get("record_type") in {
                    "supplement_request_fulfilled",
                    "supplement_request_expired",
                    "supplement_request_invalidated",
                }:
                    open_requests.discard(record.get("request_id"))
            queued_jobs = sum(
                job.get("kind") == "supplement_check"
                and job.get("status") not in {"complete", "diagnostic"}
                for job in self._store.jobs
            )
            return {
                **state,
                "open_request_count": len(open_requests),
                "queued_job_count": queued_jobs,
            }

    def stop_new_supplement_requests(
        self,
        *,
        principal: S01CommandPrincipal,
        idempotency_key: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        return self._change_supplement_operations(
            principal=principal,
            idempotency_key=idempotency_key,
            requests="closed",
            now=now,
        )

    def stop_supplement_intake(
        self,
        *,
        principal: S01CommandPrincipal,
        idempotency_key: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        return self._change_supplement_operations(
            principal=principal,
            idempotency_key=idempotency_key,
            intake="closed",
            now=now,
        )

    def drain_supplement_operations(
        self,
        *,
        principal: S01CommandPrincipal,
        idempotency_key: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        result = self.stop_new_supplement_requests(
            principal=principal,
            idempotency_key=idempotency_key,
            now=now,
        )
        if result.get("status") != "accepted":
            return result
        status = self.supplement_operations_status(principal=principal, now=now)
        return {
            **result,
            "drain": (
                "complete"
                if status["open_request_count"] == 0
                and status["queued_job_count"] == 0
                else "draining"
            ),
            "open_request_count": status["open_request_count"],
            "queued_job_count": status["queued_job_count"],
        }

    def fence_supplement_workers(
        self,
        *,
        principal: S01CommandPrincipal,
        idempotency_key: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        return self._change_supplement_operations(
            principal=principal,
            idempotency_key=idempotency_key,
            workers="fenced",
            now=now,
        )

    def resume_supplement_operations(
        self,
        *,
        principal: S01CommandPrincipal,
        idempotency_key: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        return self._change_supplement_operations(
            principal=principal,
            idempotency_key=idempotency_key,
            requests="open",
            intake="open",
            workers="open",
            now=now,
        )

    def _change_supplement_operations(
        self,
        *,
        principal: S01CommandPrincipal,
        idempotency_key: str,
        requests: str | None = None,
        intake: str | None = None,
        workers: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        event_time = int(self._clock() if now is None else now)
        if not self._valid_exception_router_principal(principal, now=event_time):
            raise QueryNotFound("supplement_operations")
        if not self._valid_idempotency_key(idempotency_key):
            raise ValueError("supplement operations idempotency key is invalid")
        requested = {key: value for key, value in {
            "requests": requests,
            "intake": intake,
            "workers": workers,
        }.items() if value is not None}
        if not requested or any(
            value not in ({"open", "closed"} if key != "workers" else {"open", "fenced"})
            for key, value in requested.items()
        ):
            raise ValueError("supplement operations command is invalid")
        fingerprint = hashlib.sha256(
            json.dumps(requested, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        binding_key = self._exception_idempotency_binding_key(
            principal,
            "supplement_operations",
            idempotency_key,
            action="supplement_operations",
        )
        with self._lock:
            for attempt in range(2):
                self._reload_store()
                previous = self._store.idempotency.get(binding_key)
                if previous is not None:
                    if previous[0] == fingerprint:
                        return {**copy.deepcopy(previous[1]), "replayed": True}
                    return {
                        "status": "conflict",
                        "replayed": False,
                        "reason_code": "IDEMPOTENCY_KEY_CONFLICT",
                    }
                cohort_stop = self._store.cohort_stop
                if (
                    "open" in requested.values()
                    and cohort_stop is not None
                    and cohort_stop.get("reason_code") == self._RUNTIME_STOP_REASON
                ):
                    return {
                        "status": "stopped",
                        "replayed": False,
                        "reason_code": "S01_RUNTIME_REPAIR_NOT_VERIFIED",
                    }
                if not self.audit_available:
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "reason_code": "AUDIT_UNAVAILABLE",
                    }
                if not self.storage_available:
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "reason_code": "STORAGE_UNAVAILABLE",
                    }
                current = self._supplement_operations_state()
                next_state = {
                    key: requested.get(key, current[key])
                    for key in ("requests", "intake", "workers")
                }
                staged = copy.deepcopy(self._store)
                fenced_jobs = [
                    job
                    for job in staged.jobs
                    if workers == "fenced"
                    and job.get("kind") == "supplement_check"
                    and job.get("status") == "leased"
                ]
                fenced_job_ids = sorted(job["job_id"] for job in fenced_jobs)
                sequence = int(current["revision"]) + 1
                record = {
                    "record_id": self._stable_id(
                        "supplement_operations", f"{sequence}:{fingerprint}"
                    ),
                    "record_type": "supplement_operations_changed",
                    "schema_version": "supplement-operations/1",
                    "sequence": sequence,
                    "revision": sequence,
                    "requests": next_state["requests"],
                    "intake": next_state["intake"],
                    "workers": next_state["workers"],
                    "changed_at": event_time,
                    "requested": copy.deepcopy(requested),
                    "fenced_job_ids": fenced_job_ids,
                }
                result = {
                    "status": "accepted",
                    "replayed": False,
                    **next_state,
                    "revision": sequence,
                    "changed_at": event_time,
                    "reason_code": "supplement.operations_changed",
                    "fenced_job_ids": fenced_job_ids,
                }
                try:
                    if fenced_jobs:
                        self._before_write("supplement_operations.worker_fence")
                        for job in fenced_jobs:
                            job["fence"] = int(job.get("fence", 0)) + 1
                    self._before_write("supplement_operations.record")
                    staged.review_records.append(record)
                    self._before_write("supplement_operations.audit")
                    staged.audit_events.append(
                        {
                            "event_id": self._stable_id(
                                "audit", f"supplement_operations:{sequence}:{fingerprint}"
                            ),
                            "action": "supplement_operations_changed",
                            "subject": principal.subject,
                            "role": principal.role,
                            "scope": principal.scope,
                            "source_id": principal.source_id,
                            "operations_revision": sequence,
                            "requested": copy.deepcopy(requested),
                            "requests": next_state["requests"],
                            "intake": next_state["intake"],
                            "workers": next_state["workers"],
                            "fenced_job_ids": fenced_job_ids,
                            "result": "accepted",
                            **self._audit_time_fields(staged, now=event_time),
                        }
                    )
                    self._before_write("supplement_operations.idempotency")
                    staged.idempotency[binding_key] = (fingerprint, result)
                    self._before_write("supplement_operations.publish")
                    staged.persist()
                except StaleStoreRevision:
                    if attempt == 0:
                        continue
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "reason_code": "STORAGE_UNAVAILABLE",
                    }
                except _StoreWriteFailure as error:
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "reason_code": (
                            "AUDIT_UNAVAILABLE"
                            if str(error) == "supplement_operations.audit"
                            else "STORAGE_UNAVAILABLE"
                        ),
                    }
                self._store = staged
                return result
        return {"status": "unavailable", "replayed": False, "reason_code": "STORAGE_UNAVAILABLE"}

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
        policy_pin = None
        if self._policy_governance is not None:
            try:
                policy_pin = self._policy_governance.resolve_run_pin(
                    "C-DEMO/demo", int(self._clock())
                )
            except Exception as error:
                raise _PinnedReleaseUnavailable(self._PINNED_RELEASE_FAILURE) from error
        if policy_pin is not None:
            release = policy_pin["release"]
        elif self._policy_governance is not None:
            raise _PinnedReleaseUnavailable(self._POLICY_UNAVAILABLE_FAILURE)
        else:
            release = self._legacy_run_release()
        probe_spec: dict[str, Any] = {
            "run_id": self._stable_id(
                "probe", f"runtime-repair:{probe_identity}:{release['digest']}"
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
                    for key, value in release.items()
                    if key not in {"target_release", "legacy_oracle"}
                }
            ),
            "release_id": release["release_id"],
            "release_digest": release["digest"],
            "checker_build": release["checker_build"],
            "fence": fence,
            "limits": copy.deepcopy(release["limits"]),
            "applicable_check_ids": release["applicable_check_ids"],
            "applicable_check_count": release["applicable_check_count"],
        }
        if policy_pin is not None:
            probe_spec.update(
                {
                    "policy_scope": policy_pin["policy_scope"],
                    "activation_event_id": policy_pin["activation_event_id"],
                    "active_generation": policy_pin["active_generation"],
                    "candidate_id": policy_pin["candidate_id"],
                    "manifest_id": policy_pin["manifest_id"],
                    "manifest_digest": policy_pin["manifest_digest"],
                    "validation_bundle_id": policy_pin["validation_bundle_id"],
                    "validation_bundle_digest": policy_pin["validation_bundle_digest"],
                    "approval_binding_id": policy_pin["approval_binding_id"],
                    "approval_binding_digest": policy_pin["approval_binding_digest"],
                    "components": copy.deepcopy(policy_pin["components"]),
                }
            )
        return probe_spec

    def _verify_runtime_repair(self, failure_reason_code: str) -> dict[str, Any] | None:
        if failure_reason_code == self._REVIEW_SOURCE_FAILURE:
            review_apps = [
                app
                for app in self._store.applications.values()
                if app.get("phase") == "Manual Review"
            ]
            if not review_apps or any(
                not self._review_source_evidence_readable(app) for app in review_apps
            ):
                return None
            payload = {
                "kind": "review_source_readability_probe",
                "verified_applications": len(review_apps),
            }
            encoded = json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            return {
                **payload,
                "probe_digest": hashlib.sha256(encoded).hexdigest(),
            }

        diagnostic_jobs = [
            job
            for job in self._store.jobs
            if job.get("status") == "diagnostic"
            and job.get("terminal_reason_code") == failure_reason_code
        ]
        publication_jobs = [
            job
            for job in diagnostic_jobs
            if isinstance(job.get("failure_publication_pending"), str)
        ]
        if publication_jobs:
            if len(publication_jobs) != len(diagnostic_jobs):
                return None
            try:
                self._before_write("s07.failure.audit")
            except _StoreWriteFailure:
                return None
            payload = {
                "kind": "failure_publication_authority_probe",
                "logical_operation_ids": sorted(
                    str(job["job_id"]) for job in publication_jobs
                ),
                "store_revision": self._store._store_revision,
            }
            encoded = json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            return {**payload, "probe_digest": hashlib.sha256(encoded).hexdigest()}
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
                "release_digest": probe_run_spec["release_digest"],
                "checker_build": probe_run_spec["checker_build"],
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
                # Governed recovery always resolves the stopped RunSpec
                # through the Registry/Ledger: an activation-pinned RunSpec
                # loads its exact pinned release, a pre-cutover RunSpec is
                # exact-mapped to the Registry compat checker, and the
                # legacy singleton is never a governed target fallback.
                probe_release = self._pinned_release_for(stopped_run_spec)
                probe_run_spec.update(
                    {
                        "run_id": self._stable_id(
                            "probe",
                            f"{job['job_id']}:{stopped_fence}:{probe_release['digest']}",
                        ),
                        "baseline_release": copy.deepcopy(
                            {
                                key: value
                                for key, value in probe_release.items()
                                if key not in {"target_release", "legacy_oracle"}
                            }
                        ),
                        "release_id": probe_release["release_id"],
                        "release_digest": probe_release["digest"],
                        "checker_build": probe_release["checker_build"],
                        "fence": stopped_fence + 1,
                        "limits": copy.deepcopy(probe_release["limits"]),
                        "applicable_check_ids": probe_release[
                            "applicable_check_ids"
                        ],
                        "applicable_check_count": probe_release[
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
                        "release_digest": probe_release["digest"],
                        "checker_build": probe_release["checker_build"],
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
            "release_digest": probes[-1]["release_digest"],
            "checker_build": probes[-1]["checker_build"],
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

    def verify_recovery(
        self,
        *,
        principal: S01CommandPrincipal,
        recovery_work_id: str,
        expected_lifecycle_revision: int,
        expected_criterion_digest: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Verify one pinned condition and re-enter its fixed normal gate."""
        if (
            not isinstance(recovery_work_id, str)
            or not recovery_work_id
            or recovery_work_id.strip() != recovery_work_id
            or isinstance(expected_lifecycle_revision, bool)
            or not isinstance(expected_lifecycle_revision, int)
            or expected_lifecycle_revision < 1
            or not isinstance(expected_criterion_digest, str)
            or len(expected_criterion_digest) != 64
            or any(character not in "0123456789abcdef" for character in expected_criterion_digest)
            or not self._valid_idempotency_key(idempotency_key)
        ):
            raise ValueError("VerifyRecovery command is invalid")
        if (
            principal.role != "operator"
            or not isinstance(principal.subject, str)
            or not principal.subject
            or principal.subject.strip() != principal.subject
            or not self.is_controlled_scope(principal.scope)
            or not isinstance(principal.source_id, str)
            or not principal.source_id
        ):
            raise QueryNotFound(recovery_work_id)

        command = {
            "recovery_work_id": recovery_work_id,
            "expected_lifecycle_revision": expected_lifecycle_revision,
            "expected_criterion_digest": expected_criterion_digest,
            "idempotency_key": idempotency_key,
        }
        command_fingerprint = hashlib.sha256(
            json.dumps(command, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        binding_key = ":".join(
            (
                "s07",
                "verify_recovery",
                principal.scope,
                principal.subject,
                recovery_work_id,
                hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
            )
        )

        with self._lock:
            self._reload_store()
            events = [
                event
                for event in self._store.recovery_events
                if event.get("recovery_work_id") == recovery_work_id
            ]
            opened = [event for event in events if event.get("kind") == "opened"]
            if len(opened) != 1 or not self._recovery_scope_visible(
                principal, opened[0].get("visibility_scope")
            ):
                raise QueryNotFound(recovery_work_id)
            work = opened[0]
            previous = self._store.idempotency.get(binding_key)
            if previous is not None:
                previous_fingerprint, previous_result = previous
                if previous_fingerprint == command_fingerprint:
                    return {**copy.deepcopy(previous_result), "replayed": True}
                return {
                    "status": "conflict",
                    "reason_code": "recovery.idempotency_conflict",
                    "replayed": False,
                }
            resolved_events = [
                event for event in events if event.get("kind") == "resolved"
            ]
            if len(resolved_events) > 1 or any(
                event.get("kind") in {"superseded", "terminated"}
                for event in events
            ):
                return {
                    "status": "stale",
                    "reason_code": "recovery.work_not_open",
                    "replayed": False,
                }
            resolved_delivery = len(resolved_events) == 1
            application_id = str(work["application_id"])
            app = self._store.applications.get(application_id)
            self._require_application_state_authority(app)
            assert app is not None
            pinned_spec = next(
                (
                    attempt["run_spec"]
                    for attempt in reversed(self._store.attempts)
                    if attempt.get("application_id") == application_id
                    and isinstance(attempt.get("run_spec"), dict)
                ),
                None,
            )
            if (
                work["lifecycle_revision"] != expected_lifecycle_revision
                or work["criterion"]["digest"] != expected_criterion_digest
                or not resolved_delivery
                and (
                    app["phase"] != "Unprocessable"
                    or app["cycle"] != work["cycle"]
                    or app["lifecycle_revision"] != expected_lifecycle_revision
                    or app["evidence_revision"] != work["evidence_revision"]
                    or pinned_spec is None
                    or pinned_spec["release_id"] != work["release_id"]
                    or pinned_spec["release_digest"] != work["release_digest"]
                    or pinned_spec["checker_build"] != work["checker_build"]
                )
            ):
                return {
                    "status": "stale",
                    "reason_code": "recovery.context_changed",
                    "replayed": False,
                }
            if not self.audit_available or not self.storage_available:
                return {
                    "status": "unavailable",
                    "reason_code": "recovery.authority_unavailable",
                    "replayed": False,
                }
            if self._recovery_verifier is None:
                return {
                    "status": "unavailable",
                    "reason_code": "recovery.verifier_unavailable",
                    "replayed": False,
                }
            try:
                verification = self._recovery_verifier(copy.deepcopy(work))
            except Exception:
                return {
                    "status": "unavailable",
                    "reason_code": "recovery.verifier_unavailable",
                    "replayed": False,
                }
            if not isinstance(verification, dict):
                return {
                    "status": "rejected",
                    "reason_code": "recovery.criterion_not_satisfied",
                    "replayed": False,
                }
            verification_id = verification.get("verification_id")
            observed_at = verification.get("observed_at")
            evidence_kind = verification.get("evidence_kind")
            condition_results = verification.get("conditions")
            expected_condition_ids = {
                str(condition["condition_id"]) for condition in work["conditions"]
            }
            valid_conditions = (
                isinstance(condition_results, list)
                and len(condition_results) == len(expected_condition_ids)
                and {
                    str(condition.get("condition_id"))
                    for condition in condition_results
                    if isinstance(condition, dict)
                }
                == expected_condition_ids
                and all(
                    isinstance(condition, dict)
                    and condition.get("verified") is True
                    and isinstance(condition.get("evidence_digest"), str)
                    and len(condition["evidence_digest"]) == 64
                    and all(
                        character in "0123456789abcdef"
                        for character in condition["evidence_digest"]
                    )
                    for condition in condition_results
                )
            )
            if (
                not isinstance(verification_id, str)
                or not verification_id
                or verification_id.strip() != verification_id
                or len(verification_id) > 200
                or isinstance(observed_at, bool)
                or not isinstance(observed_at, int)
                or observed_at <= int(work["opened_at"])
                or evidence_kind != work["criterion"]["evidence_kind"]
                or verification.get("scope") != work["visibility_scope"]
                or verification.get("recovery_work_id") != recovery_work_id
                or verification.get("criterion_digest")
                != expected_criterion_digest
                or not valid_conditions
            ):
                return {
                    "status": "rejected",
                    "reason_code": "recovery.criterion_not_satisfied",
                    "replayed": False,
                }

            target = str(work["recovery_target"])
            recovery_fact_event_id = self._stable_id(
                "recovery_event", f"{recovery_work_id}:fact:{verification_id}"
            )
            recovery_fact = {
                "event_id": recovery_fact_event_id,
                "kind": "fact",
                "schema_version": "recovery-fact/1",
                "recovery_work_id": recovery_work_id,
                "recovery_fact_id": verification_id,
                "application_id": application_id,
                "visibility_scope": work["visibility_scope"],
                "criterion_digest": expected_criterion_digest,
                "evidence_kind": evidence_kind,
                "condition_results": copy.deepcopy(condition_results),
                "observed_at": observed_at,
                "verifier": work["criterion"]["trusted_verifier"],
            }
            semantic_digest = hashlib.sha256(
                json.dumps(
                    recovery_fact,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            duplicate_result = self._replay_s07_recovery_delivery(
                binding_key=binding_key,
                command_fingerprint=command_fingerprint,
                recovery_fact=recovery_fact,
                semantic_digest=semantic_digest,
            )
            if duplicate_result is not None:
                return duplicate_result
            if resolved_delivery:
                return {
                    "status": "stale",
                    "reason_code": "recovery.work_not_open",
                    "replayed": False,
                }
            staged = copy.deepcopy(self._store)
            staged_app = staged.applications[application_id]
            blocked_job = next(
                job for job in staged.jobs if job.get("job_id") == work["job_id"]
            )
            resolved = {
                "event_id": self._stable_id(
                    "recovery_event", f"{recovery_work_id}:resolved"
                ),
                "kind": "resolved",
                "schema_version": "recovery-work/1",
                "recovery_work_id": recovery_work_id,
                "application_id": application_id,
                "recovery_fact_id": verification_id,
                "resolved_at": observed_at,
                "recovery_target": target,
            }
            staged.recovery_events.extend((recovery_fact, resolved))
            staged.inbox.append(
                {
                    "message_id": verification_id,
                    "kind": "s07_recovery_fact",
                    "semantic_digest": semantic_digest,
                    "recovery_work_id": recovery_work_id,
                    "application_id": application_id,
                    "received_at": observed_at,
                }
            )
            staged_app["evidence_ready"] = target != "Assembly"
            staged_app["route"] = (
                "routing_determination"
                if target == "Routing Determination"
                else "pending_check"
            )
            staged_app["projection_visible"] = False
            staged_app["projection_pending"] = False
            self._transition_lifecycle(
                staged_app,
                target,
                "VERIFIED_RECOVERY_FACT_ACCEPTED",
                store=staged,
            )
            staged.lifecycle_events[-1].update(
                {
                    "recovery_work_id": recovery_work_id,
                    "recovery_fact_id": verification_id,
                }
            )
            routing_context = None
            routing_request = None
            routing_decision = None
            if target == "Routing Determination":
                routing_request_id = work.get("request_id")
                routing_decision_id = work.get("decision_id")
                routing_request = (
                    self._business_exception_request_authority(routing_request_id)
                    if isinstance(routing_request_id, str)
                    else None
                )
                routing_decisions = [
                    record
                    for record in staged.review_records
                    if record.get("record_type") == "business_exception_decision"
                    and record.get("request_id") == routing_request_id
                    and record.get("decision_id") == routing_decision_id
                    and record.get("decision") == "approved"
                ]
                if routing_request is None or len(routing_decisions) != 1:
                    return {
                        "status": "unavailable",
                        "reason_code": "recovery.authority_unavailable",
                        "replayed": False,
                    }
                routing_decision = routing_decisions[0]
                routing_context = self._business_exception_routing_context(
                    routing_request, routing_decision, staged_app
                )
                if work.get("routing_context") != blocked_job.get("routing_context"):
                    return {
                        "status": "unavailable",
                        "reason_code": "recovery.authority_unavailable",
                        "replayed": False,
                    }
                staged.lifecycle_events[-1].update(
                    {
                        "run_id": routing_request["run_id"],
                        "request_id": routing_request["request_id"],
                        "decision_id": routing_decision["decision_id"],
                    }
                )
            successor_job_id = self._stable_id(
                "job", f"s07_recovery:{recovery_work_id}:{verification_id}"
            )
            successor_fence = int(blocked_job.get("fence", 0)) + 1
            successor_job = {
                "job_id": successor_job_id,
                "application_id": application_id,
                "kind": "recovery_check"
                if target in {"Assembly", "Evidence Ready"}
                else "recovery_route",
                "status": "queued",
                "fingerprint": expected_criterion_digest,
                "logical_operation_id": successor_job_id,
                "recovery_work_id": recovery_work_id,
                "fence": successor_fence,
                "attempt_no": 0,
            }
            if target == "Routing Determination":
                successor_job.update(
                    {
                        "request_id": routing_request["request_id"],
                        "decision_id": routing_decision["decision_id"],
                        "routing_context": copy.deepcopy(routing_context),
                    }
                )
            staged.jobs.append(successor_job)
            try:
                self._before_write("s07.recovery.audit")
            except _StoreWriteFailure:
                return {
                    "status": "unavailable",
                    "reason_code": "recovery.authority_unavailable",
                    "replayed": False,
                }
            staged.audit_events.append(
                {
                    "event_id": self._stable_id(
                        "audit", f"s07_recovery:{recovery_work_id}:{verification_id}"
                    ),
                    "action": "verified_recovery_accepted",
                    "subject": principal.subject,
                    "role": principal.role,
                    "scope": principal.scope,
                    "source_id": principal.source_id,
                    "application_id": application_id,
                    "recovery_work_id": recovery_work_id,
                    "recovery_fact_id": verification_id,
                    "result": "accepted",
                    "lifecycle_revision": staged_app["lifecycle_revision"],
                    "recovery_target": target,
                    "successor_job_id": successor_job_id,
                    "successor_fence": successor_fence,
                    **self._audit_time_fields(staged),
                }
            )
            staged.outbox.append(
                {
                    "event_id": self._stable_id(
                        "outbox", f"s07_recovery_gate:{successor_job_id}"
                    ),
                    "kind": "s07_recovery_gate_requested",
                    "application_id": application_id,
                    "recovery_work_id": recovery_work_id,
                    "recovery_fact_id": verification_id,
                    "job": copy.deepcopy(successor_job),
                    "lifecycle_revision": staged_app["lifecycle_revision"],
                    "visibility_scope": work["visibility_scope"],
                    "status": "pending",
                }
            )
            result = {
                "status": "accepted",
                "replayed": False,
                "recovery_work_id": recovery_work_id,
                "recovery_fact_id": verification_id,
                "application_id": application_id,
                "phase": target,
                "lifecycle_revision": staged_app["lifecycle_revision"],
                "evidence_revision": staged_app["evidence_revision"],
                "successor_job_id": successor_job_id,
                "successor_fence": successor_fence,
            }
            if routing_context is not None:
                result["routing_context"] = copy.deepcopy(routing_context)
            staged.idempotency[binding_key] = (command_fingerprint, result)
            try:
                self._before_write("s07.recovery.publish")
                staged.persist()
            except _StoreWriteFailure:
                return {
                    "status": "unavailable",
                    "reason_code": "recovery.authority_unavailable",
                    "replayed": False,
                }
            except StaleStoreRevision:
                self._reload_store()
                previous = self._store.idempotency.get(binding_key)
                if previous is not None:
                    if previous[0] == command_fingerprint:
                        return {**copy.deepcopy(previous[1]), "replayed": True}
                    return {
                        "status": "conflict",
                        "reason_code": "recovery.idempotency_conflict",
                        "replayed": False,
                    }
                duplicate_result = self._replay_s07_recovery_delivery(
                    binding_key=binding_key,
                    command_fingerprint=command_fingerprint,
                    recovery_fact=recovery_fact,
                    semantic_digest=semantic_digest,
                )
                if duplicate_result is not None:
                    return duplicate_result
                current_events = [
                    event
                    for event in self._store.recovery_events
                    if event.get("recovery_work_id") == recovery_work_id
                ]
                if any(
                    event.get("kind") in {"resolved", "superseded", "terminated"}
                    for event in current_events
                ):
                    return {
                        "status": "stale",
                        "reason_code": "recovery.work_not_open",
                        "replayed": False,
                    }
                return {
                    "status": "unavailable",
                    "reason_code": "recovery.authority_unavailable",
                    "replayed": False,
                }
            self._store = staged
            return copy.deepcopy(result)

    def _replay_s07_recovery_delivery(
        self,
        *,
        binding_key: str,
        command_fingerprint: str,
        recovery_fact: dict[str, Any],
        semantic_digest: str,
    ) -> dict[str, Any] | None:
        verification_id = recovery_fact["recovery_fact_id"]
        recovery_work_id = recovery_fact["recovery_work_id"]
        application_id = recovery_fact["application_id"]
        delivered = [
            message
            for message in self._store.inbox
            if message.get("message_id") == verification_id
        ]
        facts = [
            event
            for event in self._store.recovery_events
            if event.get("kind") == "fact"
            and event.get("recovery_fact_id") == verification_id
        ]
        if not delivered and not facts:
            return None
        if (
            len(delivered) != 1
            or delivered[0].get("semantic_digest") != semantic_digest
            or delivered[0].get("recovery_work_id") != recovery_work_id
            or delivered[0].get("application_id") != application_id
            or len(facts) != 1
            or facts[0] != recovery_fact
        ):
            return {
                "status": "conflict",
                "reason_code": "recovery.fact_identity_conflict",
                "replayed": False,
            }
        accepted_results = [
            stored_result
            for _, stored_result in self._store.idempotency.values()
            if isinstance(stored_result, dict)
            and stored_result.get("status") == "accepted"
            and stored_result.get("recovery_work_id") == recovery_work_id
            and stored_result.get("recovery_fact_id") == verification_id
        ]
        canonical_results = {
            json.dumps(
                stored_result,
                sort_keys=True,
                separators=(",", ":"),
            ): stored_result
            for stored_result in accepted_results
        }
        if len(canonical_results) != 1:
            return {
                "status": "unavailable",
                "reason_code": "recovery.authority_unavailable",
                "replayed": False,
            }
        canonical = copy.deepcopy(next(iter(canonical_results.values())))
        previous = self._store.idempotency.get(binding_key)
        if previous is not None:
            if previous[0] == command_fingerprint:
                return {**copy.deepcopy(previous[1]), "replayed": True}
            return {
                "status": "conflict",
                "reason_code": "recovery.idempotency_conflict",
                "replayed": False,
            }
        rebound = copy.deepcopy(self._store)
        rebound.idempotency[binding_key] = (command_fingerprint, canonical)
        try:
            rebound.persist()
        except StaleStoreRevision:
            self._reload_store()
            rebound_previous = self._store.idempotency.get(binding_key)
            if (
                rebound_previous is None
                or rebound_previous[0] != command_fingerprint
            ):
                return {
                    "status": "unavailable",
                    "reason_code": "recovery.authority_unavailable",
                    "replayed": False,
                }
            canonical = copy.deepcopy(rebound_previous[1])
        else:
            self._store = rebound
        return {**canonical, "replayed": True}

    def recovery_work_view(
        self,
        *,
        principal: S01CommandPrincipal,
        recovery_work_id: str,
    ) -> dict[str, Any]:
        """Return one minimized Lifecycle-owned recovery work view."""
        if (
            not isinstance(recovery_work_id, str)
            or not recovery_work_id
            or recovery_work_id.strip() != recovery_work_id
            or principal.role not in {"reviewer", "operator"}
            or not self.is_controlled_scope(principal.scope)
        ):
            raise QueryNotFound(str(recovery_work_id))
        with self._lock:
            self._reload_store()
            events = [
                event
                for event in self._store.recovery_events
                if event.get("recovery_work_id") == recovery_work_id
            ]
            opened = [event for event in events if event.get("kind") == "opened"]
            if len(opened) != 1:
                raise QueryNotFound(recovery_work_id)
            work = opened[0]
            if not self._recovery_scope_visible(
                principal, work.get("visibility_scope")
            ):
                raise QueryNotFound(recovery_work_id)
            application_id = str(work["application_id"])
            if (
                principal.role == "reviewer"
                and self._application_review_assignee(application_id)
                != principal.subject
            ):
                raise QueryNotFound(recovery_work_id)
            app = self._store.applications.get(application_id)
            self._require_application_state_authority(app)
            assert app is not None
            status = "open"
            if any(event.get("kind") == "resolved" for event in events):
                status = "resolved"
            elif any(event.get("kind") == "superseded" for event in events):
                status = "superseded"
            elif any(event.get("kind") == "terminated" for event in events):
                status = "terminated"
            attempt_ids = set(work.get("attempt_ids") or ())
            attempts = sorted(
                (
                    {
                        "attempt": int(record["attempt_no"]),
                        "classification": str(record["failure_classification"]),
                        "status": {
                            "failure_publication_pending": "reconcile_wait",
                            "terminal_failure": "blocked",
                            "transient_failure": "retry_wait",
                            "exhausted": "exhausted",
                        }[str(record["status"])],
                        "started_at": int(record["started_at"]),
                        "retry_not_before": record.get("retry_not_before"),
                    }
                    for record in self._store.runs
                    if record.get("attempt_id") in attempt_ids
                    and record.get("status")
                    in {
                        "failure_publication_pending",
                        "terminal_failure",
                        "transient_failure",
                        "exhausted",
                    }
                ),
                key=lambda item: item["attempt"],
            )
            if not attempts:
                attempts = sorted(
                    (
                        {
                            "attempt": int(record["attempt_no"]),
                            "classification": str(
                                record["failure_classification"]
                            ),
                            "status": "blocked",
                            "started_at": int(record["started_at"]),
                            "retry_not_before": record.get("retry_not_before"),
                        }
                        for record in self._store.attempts
                        if record.get("attempt_id") in attempt_ids
                        and record.get("status") == "terminal_failure"
                        and record.get("failure_classification") == "terminal"
                    ),
                    key=lambda item: item["attempt"],
                )
            protected_business_revision = sum(
                record.get("application_id") == application_id
                and record.get("status") == "complete"
                for record in self._store.runs
            )
            return {
                "schema_version": "recovery-work-view/1",
                "recovery_work_id": recovery_work_id,
                "status": status,
                "application_id": application_id,
                "cycle": app["cycle"],
                "phase": app["phase"],
                "route": app["route"],
                "lifecycle_revision": app["lifecycle_revision"],
                "evidence_revision": app["evidence_revision"],
                "primary_reason_code": work["primary_reason_code"],
                "related_reason_codes": list(work["related_reason_codes"]),
                "operation": work["operation"],
                "dependency": work["dependency"],
                "logical_operation_id": work["logical_operation_id"],
                "attempts": attempts,
                "responsible_party": work["responsible_party"],
                "recovery_action": work["recovery_action"],
                "recovery_target": work["recovery_target"],
                "criterion": copy.deepcopy(work["criterion"]),
                "retry_policy": copy.deepcopy(work["retry_policy"]),
                "outcome_known": bool(work["outcome_known"]),
                "retryable": False,
                "recovery_fact_count": sum(
                    event.get("kind") == "fact" for event in events
                ),
                "resolution_count": sum(
                    event.get("kind") == "resolved" for event in events
                ),
                "job_status": next(
                    str(job["status"])
                    for job in self._store.jobs
                    if job.get("job_id") == work["job_id"]
                ),
                "delivery_semantics": "at_least_once",
                "protected_business_revision": protected_business_revision,
                "current_run_id": app.get("current_run_id"),
                "projection_watermark": self._store.projection_watermark,
                "can_verify": principal.role == "operator",
            }

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
        scope: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if ttl_seconds <= 0:
            raise ValueError("session TTL must be positive")
        if not subject or subject.strip() != subject:
            raise ValueError("session subject must be a non-empty canonical value")
        session_roles = tuple(dict.fromkeys(roles))
        if not session_roles or set(session_roles) - {"integrator", "reviewer"}:
            raise ValueError("session roles must be registered demo roles")
        if scope is not None and not self.is_registered_scope(scope):
            raise ValueError("registered session scope is invalid")
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
                    "scope": scope or f"{self._SESSION_SCOPE_PREFIX}{session_id}",
                    "issued_at": float(now),
                    "expires_at": float(now) + ttl_seconds,
                    "cleanup_due_at": float(now) + self._DEMO_RETENTION_SECONDS,
                    "status": "active",
                }
                staged = copy.deepcopy(self._store)
                self._remove_expired_sessions(staged, now=float(now))
                staged.sessions[token_digest] = principal
                if scope is None:
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
            "review_records": {
                str(record["record_id"])
                for record in store.review_records
                if record.get("application_id") in application_ids
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
        operation_fault: str | None = None,
    ) -> WorkerResult:
        """Claim and execute one durable job.

        Fault controls are deterministic C-DEMO inputs. ``stale`` is the
        backwards-compatible lifecycle-revision mismatch and still passes
        through the same completion comparator as every other result.
        """
        if not worker_id or worker_id.strip() != worker_id:
            return WorkerResult(status="rejected", reason_code="INVALID_WORKER")
        if worker_id.startswith(("s09-replay", "s09-simulation")):
            # The Lifecycle RunResult interface rejects diagnostic
            # identities: reproduction replay and counterfactual simulation
            # can never enter the normal Lifecycle CAS.
            return WorkerResult(
                status="rejected", reason_code="S09_DIAGNOSTIC_IDENTITY_REJECTED"
            )
        selected_cas_fault = cas_fault or ("lifecycle_revision" if stale else None)
        if selected_cas_fault not in (None, *self._CAS_CONTEXT_FIELDS):
            return WorkerResult(status="rejected", reason_code="INVALID_CAS_FAULT")
        if operation_fault not in (None, "checker_timeout", *self._S07_FAILURES):
            return WorkerResult(status="rejected", reason_code="INVALID_OPERATION_FAULT")
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
            if (
                self._supplement_operations_state()["workers"] == "fenced"
                and any(
                    job.get("kind") == "supplement_check"
                    and job.get("status") not in {"complete", "diagnostic"}
                    for job in self._store.jobs
                )
            ):
                return WorkerResult(
                    status="stopped",
                    reason_code="supplement.workers_fenced",
                )
            if duplicate:
                return self._record_duplicate_result(worker_id)
            try:
                expired = next(
                    (
                        job
                        for job in self._store.jobs
                        if job.get("kind") in {"controlled_check", "recovery_check"}
                        and not job.get("failure_publication_pending")
                        and job.get("status") == "leased"
                        and int(job.get("lease_until", 0)) <= now
                        and int(job.get("attempt_no", 0))
                        >= self._MAX_FAILURE_ATTEMPTS
                    ),
                    None,
                )
                if expired is not None:
                    expired_attempts = [
                        attempt
                        for attempt in self._store.attempts
                        if attempt.get("job_id") == expired.get("job_id")
                        and attempt.get("fence") == expired.get("fence")
                        and attempt.get("attempt_no") == expired.get("attempt_no")
                    ]
                    if len(expired_attempts) != 1 or not isinstance(
                        expired_attempts[0].get("run_spec"), dict
                    ):
                        raise _ApplicationStateAuthorityUnavailable(
                            self._APPLICATION_STATE_FAILURE
                        )
                    expired_attempt = expired_attempts[0]
                    return self._record_s07_operation_failure(
                        self._store.applications[expired["application_id"]],
                        expired,
                        expired_attempt,
                        expired_attempt["run_spec"],
                        failure_kind="checker_transient",
                        now=now,
                        expired_lease=True,
                    )
                publication_claimed = self._claim_s07_failure_publication(
                    worker_id, now
                )
                if publication_claimed is not None:
                    job, attempt, run_spec, failure_kind = publication_claimed
                    app = self._store.applications[job["application_id"]]
                    return self._record_s07_operation_failure(
                        app,
                        job,
                        attempt,
                        run_spec,
                        failure_kind=failure_kind,
                        now=now,
                    )
                claimed = self._claim_job(worker_id, now)
            except (
                _PinnedReleaseUnavailable,
                _ApplicationStateAuthorityUnavailable,
            ) as error:
                failure_reason = (
                    self._APPLICATION_STATE_FAILURE
                    if isinstance(error, _ApplicationStateAuthorityUnavailable)
                    else getattr(error, "reason", None)
                    or self._PINNED_RELEASE_FAILURE
                )
                if (
                    isinstance(error, _PinnedReleaseUnavailable)
                    and failure_reason == "S09_POLICY_SAFETY_HOLD"
                ):
                    return WorkerResult(
                        status="stopped",
                        reason_code=failure_reason,
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
            app = self._store.applications[job["application_id"]]
            if crash:
                return WorkerResult(
                    status="crashed",
                    application_id=job["application_id"],
                    job_id=job["job_id"],
                    attempt_id=attempt_id,
                    run_id=run_spec["run_id"],
                    reason_code="WORKER_CRASH",
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

            if operation_fault is not None:
                if operation_fault == "checker_timeout":
                    return self._reconcile_s07_checker_timeout(
                        app,
                        job,
                        attempt,
                        run_spec,
                        now=now,
                    )
                return self._record_s07_operation_failure(
                    app,
                    job,
                    attempt,
                    run_spec,
                    failure_kind=operation_fault,
                    now=now,
                )

            try:
                report = self._run_checker(run_spec)
                run_result = self._convert_run_result(report, run_spec)
                semantic_differential = self._semantic_differential(app, run_result, run_spec)
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
        application = self._store.applications.get(application_id)
        if not isinstance(application, dict):
            raise RuntimeError("projection application authority is unavailable")
        visibility_scope = self._application_visibility_scope(application_id)
        # Auxiliary S09 facts (impact dispositions, hold invalidations,
        # release consumptions) share the revision of their parent
        # transition and must not participate in the contiguous transition
        # chain -- exactly as the state-authority view treats them.
        lifecycle = sorted(
            (
                event
                for event in self._store.lifecycle_events
                if event.get("application_id") == application_id
                and not event.get("auxiliary")
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
        all_blockers = self._mandatory_blocker_projections(application_id, str(run_id))
        blockers = all_blockers
        phase = str(current_event["phase"])
        if phase == "Manual Review":
            route = "manual_review"
            work_items = [
                item
                for item in self._store.work_items
                if item.get("application_id") == application_id
                and item.get("run_id") == run_id
                and item.get("kind") == "manual_review"
                and item.get("lifecycle_revision") == int(current_event["revision"])
            ]
            if len(work_items) != 1:
                raise RuntimeError("projection review work authority is unavailable")
            work_item = work_items[0]
            blockers_by_id = {
                finding["finding_id"]: finding for finding in all_blockers
            }
            if any(
                finding_id not in blockers_by_id
                for finding_id in work_item.get("finding_ids", [])
            ):
                raise RuntimeError("projection review findings are invalid")
            blockers = [
                blockers_by_id[finding_id]
                for finding_id in work_item["finding_ids"]
            ]
            if (
                work_item.get("owner") != "Lifecycle"
                or work_item.get("status") != "active"
                or work_item.get("visibility_scope") != visibility_scope
                or work_item.get("lifecycle_revision") != int(current_event["revision"])
                or work_item.get("evidence_revision") != int(spec["evidence_revision"])
                or work_item.get("evidence_snapshot_id")
                != spec["evidence_snapshot_id"]
                or not work_item.get("finding_ids")
            ):
                raise RuntimeError("projection review work authority is invalid")
        elif phase == "Verification Completed":
            route = (
                "human_complete"
                if current_event.get("reason_code")
                in {"HUMAN_REVIEW_COMPLETED", "BUSINESS_EXCEPTION_COMPLETED"}
                else "auto_complete"
            )
            work_item = None
        else:
            raise RuntimeError("published projection has no terminal lifecycle route")
        authoritative = {
            "application_id": application_id,
            "track": application.get("track"),
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
        publication_history: dict[str, list[dict[str, Any]]] = {}
        for event in self._store.outbox:
            if (
                event.get("kind") != "review_projection_requested"
                or event.get("status") != "published"
            ):
                continue
            application_id = str(event["application_id"])
            application = self._store.applications.get(application_id)
            if not isinstance(application, dict):
                raise RuntimeError("projection application authority is unavailable")
            publication_history.setdefault(application_id, []).append(event)
        published: dict[str, dict[str, Any]] = {}
        for application_id, events in publication_history.items():
            application = self._store.applications[application_id]
            if application.get("phase") not in {
                "Manual Review",
                "Verification Completed",
            }:
                continue
            current = [
                event
                for event in events
                if event.get("run_id") == application.get("current_run_id")
            ]
            current_revision = [
                event
                for event in current
                if event.get("lifecycle_revision")
                == application.get("lifecycle_revision")
            ]
            if current_revision:
                current = current_revision
            if not current and application.get("projection_pending") is True:
                continue
            if len(current) != 1:
                raise RuntimeError("projection current run binding is invalid")
            published[application_id] = current[0]
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
                    application = self._store.applications.get(application_id)
                    if not isinstance(application, dict):
                        raise RuntimeError("projection application authority is unavailable")
                    projectable = application.get("phase") in {
                        "Manual Review",
                        "Verification Completed",
                    }
                    if (
                        not projectable
                        or event.get("run_id") != application.get("current_run_id")
                        or event.get("lifecycle_revision")
                        != application.get("lifecycle_revision")
                    ):
                        if not projectable and application_id in self._store.projections:
                            self._store.projection_watermark += 1
                            del self._store.projections[application_id]
                        event["status"] = "published"
                        updated += 1
                        continue
                    self._store.projection_watermark += 1
                    self._store.projections[application_id] = (
                        self._projection_from_authority(
                            application_id,
                            projection_watermark=source_watermark,
                        )
                    )
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

    @classmethod
    def _valid_reviewer_principal(
        cls, principal: S01CommandPrincipal, *, now: float
    ) -> bool:
        return (
            isinstance(principal.subject, str)
            and bool(principal.subject)
            and principal.subject.strip() == principal.subject
            and principal.role == "reviewer"
            and cls.is_controlled_scope(principal.scope)
            and isinstance(principal.source_id, str)
            and bool(principal.source_id)
            and principal.source_id.strip() == principal.source_id
            and (
                principal.expires_at is None
                or not isinstance(principal.expires_at, bool)
                and isinstance(principal.expires_at, (int, float))
                and float(principal.expires_at) > now
            )
        )

    def _review_work_item_authority(
        self,
        *,
        principal: S01CommandPrincipal,
        work_item_id: str,
        now: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self._valid_reviewer_principal(principal, now=now):
            raise QueryNotFound(work_item_id)
        visible_scopes = {principal.scope}
        if principal.scope.startswith(self._SESSION_SCOPE_PREFIX):
            visible_scopes.add("C-DEMO")
        work_items = [
            item
            for item in self._store.work_items
            if item.get("work_item_id") == work_item_id
            and item.get("kind") == "manual_review"
            and item.get("owner") == "Lifecycle"
        ]
        if (
            len(work_items) != 1
            or work_items[0].get("visibility_scope") not in visible_scopes
            or work_items[0].get("assigned_subject") != principal.subject
        ):
            raise QueryNotFound(work_item_id)
        work_item = work_items[0]
        state = {
            "status": "unclaimed",
            "claim_subject": work_item.get("claim_subject"),
            "claim_fence": int(work_item.get("claim_fence", 0)),
            "claim_started_at": work_item.get("claim_started_at", 0),
            "claim_expires_at": work_item.get("claim_expires_at", 0),
            "decision_id": None,
            "decision_ids": [],
            "completed_finding_ids": [],
        }
        facts = sorted(
            (
                record
                for record in self._store.review_records
                if record.get("work_item_id") == work_item_id
                and record.get("record_type")
                in {
                    "work_item_claimed",
                    "work_item_renewed",
                    "work_item_released",
                    "work_item_completed",
                    "work_item_finding_completed",
                    "work_item_finding_exception_requested",
                    "work_item_invalidated",
                }
            ),
            key=lambda record: int(record["sequence"]),
        )
        if [fact.get("sequence") for fact in facts] != list(range(1, len(facts) + 1)):
            raise RuntimeError("review work-item authority is not contiguous")
        for fact in facts:
            if fact["record_type"] == "work_item_claimed":
                state.update(
                    {
                        "status": "claimed",
                        "claim_subject": fact["claim_subject"],
                        "claim_fence": fact["claim_fence"],
                        "claim_started_at": fact["claim_started_at"],
                        "claim_expires_at": fact["claim_expires_at"],
                    }
                )
            elif fact["record_type"] == "work_item_renewed":
                state["claim_expires_at"] = fact["claim_expires_at"]
            elif fact["record_type"] == "work_item_released":
                state.update(
                    {
                        "status": "released",
                        "claim_subject": None,
                        "claim_expires_at": fact["released_at"],
                    }
                )
            elif fact["record_type"] == "work_item_completed":
                state.update(
                    {
                        "status": "completed",
                        "decision_id": fact["decision_id"],
                        "decision_ids": [fact["decision_id"]],
                        "completed_finding_ids": copy.deepcopy(
                            work_item["finding_ids"]
                        ),
                    }
                )
            elif fact["record_type"] == "work_item_invalidated":
                state.update(
                    {
                        "status": "invalidated",
                        "claim_subject": None,
                        "claim_expires_at": fact["invalidated_at"],
                    }
                )
            elif fact["record_type"] == "work_item_finding_exception_requested":
                state.update(
                    {
                        "status": "exception_requested",
                        "claim_subject": None,
                        "claim_expires_at": fact["requested_at"],
                    }
                )
                state["completed_finding_ids"].append(fact["finding_id"])
            else:
                finding_id = fact.get("finding_id")
                if (
                    finding_id not in work_item["finding_ids"]
                    or finding_id in state["completed_finding_ids"]
                ):
                    raise RuntimeError("review work-item completion authority is invalid")
                state["completed_finding_ids"].append(finding_id)
                state["decision_ids"].append(fact["decision_id"])
                if set(state["completed_finding_ids"]) == set(work_item["finding_ids"]):
                    state.update(
                        {
                            "status": "completed",
                            "decision_id": None,
                        }
                    )
        return work_item, state

    def _review_current_context(
        self, work_item: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        application_id = work_item["application_id"]
        app = self._store.applications.get(application_id)
        self._require_application_state_authority(app)
        runs = [
            run
            for run in self._store.runs
            if run.get("application_id") == application_id
            and run.get("run_id") == work_item["run_id"]
            and run.get("status") == "complete"
        ]
        if app is None or len(runs) != 1:
            raise RuntimeError("review work-item current context is unavailable")
        explicitly_invalidated = any(
            record.get("record_type") == "work_item_invalidated"
            and record.get("work_item_id") == work_item["work_item_id"]
            for record in self._store.review_records
        )
        projection = self._store.projections.get(application_id)
        if isinstance(projection, dict):
            projection_watermark = projection.get("projection_watermark")
            if (
                projection.get("current_run_id") != work_item["run_id"]
                and not explicitly_invalidated
            ):
                raise RuntimeError("review work-item projection context is invalid")
        elif explicitly_invalidated:
            publications = [
                event
                for event in self._store.outbox
                if event.get("kind") == "review_projection_requested"
                and event.get("status") == "published"
                and event.get("application_id") == application_id
                and event.get("run_id") == work_item["run_id"]
            ]
            if len(publications) != 1:
                raise RuntimeError("review work-item projection context is invalid")
            projection_watermark = publications[0].get("projection_watermark")
        else:
            raise RuntimeError("review work-item current context is unavailable")
        if (
            not isinstance(projection_watermark, int)
            or isinstance(projection_watermark, bool)
            or projection_watermark < 1
        ):
            raise RuntimeError("review work-item projection context is invalid")
        run = runs[0]
        run_bytes = json.dumps(
            run,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fixed = {
            "application_id": application_id,
            "work_item_id": work_item["work_item_id"],
            "cycle": work_item["cycle"],
            "run_id": work_item["run_id"],
            "finding_ids": copy.deepcopy(work_item["finding_ids"]),
            "evidence_snapshot_id": work_item["evidence_snapshot_id"],
            "release_id": work_item["release_id"],
            "assigned_subject": work_item["assigned_subject"],
            "visibility_scope": work_item["visibility_scope"],
            "phase": app["phase"],
            "route": app["route"],
            "lifecycle_revision": app["lifecycle_revision"],
            "evidence_revision": app["evidence_revision"],
            "current_run_id": app["current_run_id"],
            "current_evidence_snapshot_id": app["current_evidence_snapshot_id"],
            "projection_watermark": projection_watermark,
            "run_authority": hashlib.sha256(run_bytes).hexdigest(),
        }
        fixed_bytes = json.dumps(
            fixed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return app, run, {
            "lifecycle_revision": app["lifecycle_revision"],
            "evidence_revision": app["evidence_revision"],
            "run_id": work_item["run_id"],
            "projection_watermark": projection_watermark,
            "current_context": hashlib.sha256(fixed_bytes).hexdigest(),
        }

    @staticmethod
    def _review_context_matches(
        expected: dict[str, Any], actual: dict[str, Any]
    ) -> bool:
        return isinstance(expected, dict) and expected == actual

    def _review_source_evidence_readable(self, app: dict[str, Any]) -> bool:
        try:
            if self._fault_injector is not None:
                self._fault_injector("review.source_read")
            evidence = self._admitted_evidence(app)
            if app.get("track") == "C-DEMO":
                source = app.get("source")
                if not isinstance(source, dict):
                    raise ValueError("controlled source authority is unavailable")
                _, source_sha256 = self._read_fixed_scenario(source.get("scenario_id"))
                if source_sha256 != source.get("source_sha256"):
                    raise ValueError("controlled source authority does not match")
            elif (
                app.get("track") == "R-OBSERVED"
                and self._registered_runtime_configured
            ):
                source = app.get("source")
                envelope = app.get("envelope")
                context = (
                    envelope.get("authenticated_context")
                    if isinstance(envelope, dict)
                    else None
                )
                if not isinstance(source, dict) or not isinstance(context, dict):
                    raise ValueError("registered source authority is unavailable")
                tenant_id = context.get("tenant_id")
                source_system_id = context.get("source_id")
                result_content = self._registered_source_boundary.read_object(
                    tenant_id=tenant_id,
                    source_system_id=source_system_id,
                    object_ref=source.get("source_result_object_ref"),
                )
                if (
                    len(result_content) != source.get("source_result_size_bytes")
                    or hashlib.sha256(result_content).hexdigest()
                    != source.get("source_result_sha256")
                ):
                    raise ValueError("registered result object does not match")
                checked: set[tuple[str, str]] = set()
                for document in evidence:
                    for observation in document.get("observations", []):
                        object_ref = observation.get("source_object_ref")
                        source_sha256 = observation.get("source_sha256")
                        if object_ref is None and source_sha256 is None:
                            continue
                        if not isinstance(object_ref, str) or not isinstance(
                            source_sha256, str
                        ):
                            raise ValueError("registered source location is invalid")
                        binding = (object_ref, source_sha256)
                        if binding in checked:
                            continue
                        content = self._registered_source_boundary.read_object(
                            tenant_id=tenant_id,
                            source_system_id=source_system_id,
                            object_ref=object_ref,
                        )
                        if hashlib.sha256(content).hexdigest() != source_sha256:
                            raise ValueError("registered source object does not match")
                        checked.add(binding)
            self._assemble_evidence(evidence)
        except Exception:
            return False
        return True

    def _review_write_gate(
        self,
        *,
        app: dict[str, Any],
    ) -> tuple[str, str] | None:
        if not self.audit_available:
            return "unavailable", "AUDIT_UNAVAILABLE"
        if not self.storage_available:
            return "unavailable", "STORAGE_UNAVAILABLE"
        cohort_stop = self._local_cohort_stop or self._store.cohort_stop
        if (
            cohort_stop is not None
            and cohort_stop.get("reason_code") == self._RUNTIME_STOP_REASON
        ):
            return (
                "stopped",
                str(
                    cohort_stop.get("failure_reason_code")
                    or self._RUNTIME_STOP_REASON
                ),
            )
        s09_reasons = self._s09_currentness_block_reasons(self._store, app)
        if "BLOCKED_POLICY_HOLD" in s09_reasons:
            return "stopped", "S09_POLICY_SAFETY_HOLD"
        if s09_reasons:
            return "stale", "S09_CURRENTNESS_FENCED"
        if self._review_source_evidence_readable(app):
            return None
        self.stop_new_cohort(
            reason_code=self._RUNTIME_STOP_REASON,
            failure_reason_code=self._REVIEW_SOURCE_FAILURE,
            principal=S01CommandPrincipal(
                subject="s03-review-gate",
                role="operator",
                scope="C-DEMO",
                source_id="s03-review-gate",
            ),
        )
        return "stopped", self._REVIEW_SOURCE_FAILURE

    def claim_review_work_item(
        self,
        *,
        principal: S01CommandPrincipal,
        work_item_id: str,
        expected_context: dict[str, Any],
        now: float | None = None,
    ) -> dict[str, Any]:
        """Claim one assigned manual-review work item with a fresh fence."""
        claim_time = int(self._clock() if now is None else now)
        with self._lock:
            for attempt in range(2):
                self._reload_store()
                work_item, state = self._review_work_item_authority(
                    principal=principal,
                    work_item_id=work_item_id,
                    now=claim_time,
                )
                app, _, actual_context = self._review_current_context(work_item)
                if not self._review_context_matches(expected_context, actual_context):
                    return {
                        "status": "stale",
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "STALE_REVIEW_CONTEXT",
                    }
                if state["status"] == "completed":
                    return {
                        "status": "completed",
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "WORK_ITEM_COMPLETED",
                    }
                has_live_successor_claim = (
                    state["status"] == "claimed"
                    and float(state["claim_expires_at"]) > claim_time
                    and any(
                        record.get("work_item_id") == work_item_id
                        and record.get("record_type") == "work_item_claimed"
                        and record.get("claim_fence") == state["claim_fence"]
                        for record in self._store.review_records
                    )
                )
                if has_live_successor_claim or (
                    state["claim_subject"] != principal.subject
                    and float(state["claim_expires_at"]) > claim_time
                ):
                    return {
                        "status": "conflict",
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "WORK_ITEM_ALREADY_CLAIMED",
                    }
                gate = self._review_write_gate(app=app)
                if gate is not None:
                    status, reason_code = gate
                    return {
                        "status": status,
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": reason_code,
                    }
                staged = copy.deepcopy(self._store)
                sequence = 1 + sum(
                    record.get("work_item_id") == work_item_id
                    and str(record.get("record_type", "")).startswith("work_item_")
                    for record in staged.review_records
                )
                claim_fence = int(state["claim_fence"]) + 1
                claim_expires_at = claim_time + self._REVIEW_CLAIM_TTL_SECONDS
                staged.review_records.append(
                    {
                        "record_id": self._stable_id(
                            "review_record", f"{work_item_id}:claim:{sequence}"
                        ),
                        "record_type": "work_item_claimed",
                        "sequence": sequence,
                        "work_item_id": work_item_id,
                        "application_id": work_item["application_id"],
                        "run_id": work_item["run_id"],
                        "claim_subject": principal.subject,
                        "claim_fence": claim_fence,
                        "claim_started_at": claim_time,
                        "claim_expires_at": claim_expires_at,
                        "recorded_at": claim_time,
                    }
                )
                try:
                    self._before_write("review.audit")
                    staged.audit_events.append(
                        {
                            "event_id": self._stable_id(
                                "audit",
                                f"review_work_item_claimed:{work_item_id}:{sequence}",
                            ),
                            "action": "review_work_item_claimed",
                            "subject": principal.subject,
                            "role": principal.role,
                            "scope": work_item["visibility_scope"],
                            "source_id": principal.source_id,
                            "application_id": work_item["application_id"],
                            "run_id": work_item["run_id"],
                            "work_item_id": work_item_id,
                            "claim_fence": claim_fence,
                            "result": "accepted",
                            **self._audit_time_fields(staged, now=claim_time),
                        }
                    )
                    staged.persist()
                except _StoreWriteFailure:
                    return {
                        "status": "unavailable",
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "AUDIT_UNAVAILABLE",
                    }
                except StaleStoreRevision:
                    if attempt == 0:
                        continue
                    raise
                self._store = staged
                return {
                    "status": "claimed",
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "claim_subject": principal.subject,
                    "claim_fence": claim_fence,
                    "claim_expires_at": claim_expires_at,
                }
            raise RuntimeError("review work-item claim retry exhausted")

    def renew_review_work_item(
        self,
        *,
        principal: S01CommandPrincipal,
        work_item_id: str,
        expected_fence: int,
        expected_context: dict[str, Any],
        idempotency_key: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Renew the caller's live work-item lease without changing its fence."""
        renew_time = int(self._clock() if now is None else now)
        with self._lock:
            for attempt in range(2):
                self._reload_store()
                work_item, state = self._review_work_item_authority(
                    principal=principal,
                    work_item_id=work_item_id,
                    now=renew_time,
                )
                app, _, actual_context = self._review_current_context(work_item)
                command_key, command_fingerprint = self._review_lifecycle_idempotency(
                    action="renew_review_work_item",
                    work_item_id=work_item_id,
                    expected_fence=expected_fence,
                    expected_context=expected_context,
                    idempotency_key=idempotency_key,
                )
                binding_key = self._review_idempotency_binding_key(
                    principal,
                    work_item_id,
                    command_key,
                    action="renew_review_work_item",
                )
                previous = self._store.idempotency.get(binding_key)
                if previous is not None:
                    previous_fingerprint, previous_result = previous
                    if previous_fingerprint == command_fingerprint:
                        return {**copy.deepcopy(previous_result), "replayed": True}
                    return {
                        "status": "conflict",
                        "replayed": False,
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "IDEMPOTENCY_KEY_CONFLICT",
                    }
                if not self._review_context_matches(expected_context, actual_context):
                    return {
                        "status": "stale",
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "STALE_REVIEW_CONTEXT",
                    }
                if (
                    state["status"] != "claimed"
                    or state["claim_subject"] != principal.subject
                    or state["claim_fence"] != expected_fence
                    or float(state["claim_expires_at"]) <= renew_time
                ):
                    return {
                        "status": "stale",
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "STALE_WORK_ITEM_CLAIM",
                    }
                gate = self._review_write_gate(app=app)
                if gate is not None:
                    status, reason_code = gate
                    return {
                        "status": status,
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": reason_code,
                    }
                staged = copy.deepcopy(self._store)
                sequence = 1 + sum(
                    record.get("work_item_id") == work_item_id
                    and str(record.get("record_type", "")).startswith("work_item_")
                    for record in staged.review_records
                )
                claim_expires_at = renew_time + self._REVIEW_CLAIM_TTL_SECONDS
                staged.review_records.append(
                    {
                        "record_id": self._stable_id(
                            "review_record", f"{work_item_id}:renew:{sequence}"
                        ),
                        "record_type": "work_item_renewed",
                        "sequence": sequence,
                        "work_item_id": work_item_id,
                        "application_id": work_item["application_id"],
                        "run_id": work_item["run_id"],
                        "claim_subject": principal.subject,
                        "claim_fence": expected_fence,
                        "claim_expires_at": claim_expires_at,
                        "recorded_at": renew_time,
                    }
                )
                result = {
                    "status": "renewed",
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "claim_subject": principal.subject,
                    "claim_fence": expected_fence,
                    "claim_expires_at": claim_expires_at,
                    "replayed": False,
                }
                try:
                    self._before_write("review.audit")
                    staged.audit_events.append(
                        {
                            "event_id": self._stable_id(
                                "audit",
                                f"review_work_item_renewed:{work_item_id}:{sequence}",
                            ),
                            "action": "review_work_item_renewed",
                            "subject": principal.subject,
                            "role": principal.role,
                            "scope": work_item["visibility_scope"],
                            "source_id": principal.source_id,
                            "application_id": work_item["application_id"],
                            "run_id": work_item["run_id"],
                            "work_item_id": work_item_id,
                            "claim_fence": expected_fence,
                            "result": "accepted",
                            **self._audit_time_fields(staged, now=renew_time),
                        }
                    )
                    self._before_write("review.idempotency")
                    staged.idempotency[binding_key] = (
                        command_fingerprint,
                        copy.deepcopy(result),
                    )
                    staged.persist()
                except _StoreWriteFailure as error:
                    return {
                        "status": "unavailable",
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": (
                            "AUDIT_UNAVAILABLE"
                            if str(error) == "review.audit"
                            else "STORAGE_UNAVAILABLE"
                        ),
                    }
                except StaleStoreRevision:
                    if attempt == 0:
                        continue
                    return {
                        "status": "unavailable",
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "STORAGE_UNAVAILABLE",
                    }
                self._store = staged
                return result
            raise RuntimeError("review work-item renew retry exhausted")

    def release_review_work_item(
        self,
        *,
        principal: S01CommandPrincipal,
        work_item_id: str,
        expected_fence: int,
        expected_context: dict[str, Any],
        idempotency_key: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Release the caller's live claim while preserving its fence history."""
        release_time = int(self._clock() if now is None else now)
        with self._lock:
            for attempt in range(2):
                self._reload_store()
                work_item, state = self._review_work_item_authority(
                    principal=principal,
                    work_item_id=work_item_id,
                    now=release_time,
                )
                app, _, actual_context = self._review_current_context(work_item)
                command_key, command_fingerprint = self._review_lifecycle_idempotency(
                    action="release_review_work_item",
                    work_item_id=work_item_id,
                    expected_fence=expected_fence,
                    expected_context=expected_context,
                    idempotency_key=idempotency_key,
                )
                binding_key = self._review_idempotency_binding_key(
                    principal,
                    work_item_id,
                    command_key,
                    action="release_review_work_item",
                )
                previous = self._store.idempotency.get(binding_key)
                if previous is not None:
                    previous_fingerprint, previous_result = previous
                    if previous_fingerprint == command_fingerprint:
                        return {**copy.deepcopy(previous_result), "replayed": True}
                    return {
                        "status": "conflict",
                        "replayed": False,
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "IDEMPOTENCY_KEY_CONFLICT",
                    }
                if not self._review_context_matches(expected_context, actual_context):
                    return {
                        "status": "stale",
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "STALE_REVIEW_CONTEXT",
                    }
                if (
                    state["status"] != "claimed"
                    or state["claim_subject"] != principal.subject
                    or state["claim_fence"] != expected_fence
                    or float(state["claim_expires_at"]) <= release_time
                ):
                    return {
                        "status": "stale",
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "STALE_WORK_ITEM_CLAIM",
                    }
                staged = copy.deepcopy(self._store)
                sequence = 1 + sum(
                    record.get("work_item_id") == work_item_id
                    and str(record.get("record_type", "")).startswith("work_item_")
                    for record in staged.review_records
                )
                staged.review_records.append(
                    {
                        "record_id": self._stable_id(
                            "review_record", f"{work_item_id}:release:{sequence}"
                        ),
                        "record_type": "work_item_released",
                        "sequence": sequence,
                        "work_item_id": work_item_id,
                        "application_id": work_item["application_id"],
                        "run_id": work_item["run_id"],
                        "claim_subject": principal.subject,
                        "claim_fence": expected_fence,
                        "released_at": release_time,
                        "recorded_at": release_time,
                    }
                )
                result = {
                    "status": "released",
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "claim_fence": expected_fence,
                    "released_at": release_time,
                    "replayed": False,
                }
                try:
                    self._before_write("review.audit")
                    staged.audit_events.append(
                        {
                            "event_id": self._stable_id(
                                "audit",
                                f"review_work_item_released:{work_item_id}:{sequence}",
                            ),
                            "action": "review_work_item_released",
                            "subject": principal.subject,
                            "role": principal.role,
                            "scope": work_item["visibility_scope"],
                            "source_id": principal.source_id,
                            "application_id": work_item["application_id"],
                            "run_id": work_item["run_id"],
                            "work_item_id": work_item_id,
                            "claim_fence": expected_fence,
                            "result": "accepted",
                            **self._audit_time_fields(staged, now=release_time),
                        }
                    )
                    self._before_write("review.idempotency")
                    staged.idempotency[binding_key] = (
                        command_fingerprint,
                        copy.deepcopy(result),
                    )
                    staged.persist()
                except _StoreWriteFailure as error:
                    return {
                        "status": "unavailable",
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": (
                            "AUDIT_UNAVAILABLE"
                            if str(error) == "review.audit"
                            else "STORAGE_UNAVAILABLE"
                        ),
                    }
                except StaleStoreRevision:
                    if attempt == 0:
                        continue
                    return {
                        "status": "unavailable",
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "STORAGE_UNAVAILABLE",
                    }
                self._store = staged
                return result
            raise RuntimeError("review work-item release retry exhausted")

    def preview_review_work_item_batch(
        self,
        *,
        principal: S01CommandPrincipal,
        items: list[dict[str, Any]],
        now: float | None = None,
    ) -> dict[str, Any]:
        """Validate and normalize a same-application/current-run review shortcut."""
        preview_time = int(self._clock() if now is None else now)
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise ValueError("review batch must contain between 1 and 100 findings")

        with self._lock:
            self._reload_store()
            normalized_items: list[dict[str, Any]] = []
            batch_authority: tuple[str, str] | None = None
            findings_by_work_item: dict[str, set[str]] = {}
            work_items_by_id: dict[str, dict[str, Any]] = {}
            for item in items:
                if not isinstance(item, dict) or set(item) != {
                    "work_item_id",
                    "finding_id",
                    "outcome",
                    "reason_code",
                    "expected_fence",
                    "expected_context",
                }:
                    raise ValueError("review batch item does not match the registered contract")
                work_item_id = item.get("work_item_id")
                finding_id = item.get("finding_id")
                expected_fence = item.get("expected_fence")
                if (
                    not isinstance(work_item_id, str)
                    or not work_item_id
                    or not isinstance(finding_id, str)
                    or not finding_id
                    or not isinstance(expected_fence, int)
                    or isinstance(expected_fence, bool)
                ):
                    raise ValueError("review batch item contains an invalid identifier")
                normalized_verification = self._canonical_review_verification(
                    {
                        "schema_version": "human-decision/1",
                        "outcome": item.get("outcome"),
                        "reason_code": item.get("reason_code"),
                        "finding_decisions": [
                            {
                                "finding_id": finding_id,
                                "outcome": item.get("outcome"),
                            }
                        ],
                    }
                )
                work_item, state = self._review_work_item_authority(
                    principal=principal,
                    work_item_id=work_item_id,
                    now=preview_time,
                )
                _, _, actual_context = self._review_current_context(work_item)
                if not self._review_context_matches(
                    item.get("expected_context"), actual_context
                ):
                    return {
                        "status": "stale",
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "STALE_REVIEW_CONTEXT",
                    }
                if (
                    state["status"] != "claimed"
                    or state["claim_subject"] != principal.subject
                    or state["claim_fence"] != expected_fence
                    or float(state["claim_expires_at"]) <= preview_time
                ):
                    return {
                        "status": "stale",
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "STALE_WORK_ITEM_CLAIM",
                    }
                authority = (work_item["application_id"], work_item["run_id"])
                if batch_authority is None:
                    batch_authority = authority
                elif batch_authority != authority:
                    return {
                        "status": "rejected",
                        "reason_code": "MIXED_REVIEW_BATCH",
                    }
                selected = findings_by_work_item.setdefault(work_item_id, set())
                if finding_id not in work_item["finding_ids"] or finding_id in selected:
                    raise ValueError("review batch finding is not unique in its work item")
                selected.add(finding_id)
                work_items_by_id[work_item_id] = work_item
                normalized_items.append(
                    {
                        "work_item_id": work_item_id,
                        "finding_id": finding_id,
                        "outcome": normalized_verification["outcome"],
                        "reason_code": normalized_verification["reason_code"],
                        "expected_fence": expected_fence,
                        "expected_context": copy.deepcopy(actual_context),
                    }
                )
            if any(
                findings_by_work_item[work_item_id] != set(work_item["finding_ids"])
                for work_item_id, work_item in work_items_by_id.items()
            ):
                raise ValueError("review batch must decide every work-item finding")
            return {
                "schema_version": "review-batch-plan/1",
                "items": normalized_items,
            }

    def submit_review_work_item_batch(
        self,
        *,
        principal: S01CommandPrincipal,
        idempotency_key: str,
        plan: dict[str, Any],
        now: float | None = None,
    ) -> dict[str, Any]:
        """Commit one validated review shortcut as per-finding immutable facts."""
        submit_time = int(self._clock() if now is None else now)
        target_id = "review-batch"
        if isinstance(plan, dict) and isinstance(plan.get("items"), list):
            first = next(
                (
                    item.get("work_item_id")
                    for item in plan["items"]
                    if isinstance(item, dict)
                    and isinstance(item.get("work_item_id"), str)
                ),
                None,
            )
            if first:
                target_id = first
        if not self._valid_reviewer_principal(principal, now=submit_time):
            raise QueryNotFound(target_id)
        if not self._valid_idempotency_key(idempotency_key):
            raise ValueError("review idempotency key is invalid")
        if (
            not isinstance(plan, dict)
            or set(plan) != {"schema_version", "items"}
            or plan.get("schema_version") != "review-batch-plan/1"
        ):
            raise ValueError("review batch plan does not match the registered contract")
        try:
            fingerprint_bytes = json.dumps(
                plan,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("review batch plan is not canonical JSON") from error
        command_fingerprint = hashlib.sha256(fingerprint_bytes).hexdigest()
        binding_bytes = json.dumps(
            {
                "action": "submit_review_work_item_batch",
                "key": idempotency_key,
                "role": principal.role,
                "scope": principal.scope,
                "source_id": principal.source_id,
                "subject": principal.subject,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        binding_key = f"s03_idempotency_{hashlib.sha256(binding_bytes).hexdigest()}"

        with self._lock:
            self._reload_store()
            previous = self._store.idempotency.get(binding_key)
            if previous is not None:
                previous_fingerprint, previous_result = previous
                if previous_fingerprint == command_fingerprint:
                    return {**copy.deepcopy(previous_result), "replayed": True}
                return {
                    "status": "conflict",
                    "replayed": False,
                    "application_id": previous_result["application_id"],
                    "reason_code": "IDEMPOTENCY_KEY_CONFLICT",
                }

            validated = self.preview_review_work_item_batch(
                principal=principal,
                items=plan["items"],
                now=submit_time,
            )
            if validated.get("schema_version") != "review-batch-plan/1":
                return {**validated, "replayed": False}
            if validated != plan:
                raise ValueError("review batch plan is not canonical")

            work_items: dict[str, dict[str, Any]] = {}
            contexts: dict[str, dict[str, Any]] = {}
            app: dict[str, Any] | None = None
            run: dict[str, Any] | None = None
            run_id: str | None = None
            for item in plan["items"]:
                work_item_id = item["work_item_id"]
                if work_item_id in work_items:
                    continue
                work_item, _ = self._review_work_item_authority(
                    principal=principal,
                    work_item_id=work_item_id,
                    now=submit_time,
                )
                current_app, current_run, actual_context = self._review_current_context(
                    work_item
                )
                work_items[work_item_id] = work_item
                contexts[work_item_id] = actual_context
                if app is None:
                    app = current_app
                    run = current_run
                    run_id = work_item["run_id"]
            if app is None or run is None or run_id is None:
                raise RuntimeError("review batch authority is unavailable")
            if any(
                work_item["application_id"] != app["application_id"]
                or work_item["run_id"] != run_id
                or work_item["lifecycle_revision"] != app["lifecycle_revision"]
                or work_item["evidence_revision"] != app["evidence_revision"]
                for work_item in work_items.values()
            ) or app.get("phase") != "Manual Review" or app.get("current_run_id") != run_id:
                return {
                    "status": "stale",
                    "replayed": False,
                    "reason_code": "STALE_REVIEW_CONTEXT",
                }
            gate = self._review_write_gate(app=app)
            if gate is not None:
                status, reason_code = gate
                return {
                    "status": status,
                    "replayed": False,
                    "application_id": app["application_id"],
                    "reason_code": reason_code,
                }

            staged = copy.deepcopy(self._store)
            staged_app = staged.applications[app["application_id"]]
            result_items: list[dict[str, str]] = []
            sequences = {
                work_item_id: 1
                + sum(
                    record.get("work_item_id") == work_item_id
                    and str(record.get("record_type", "")).startswith("work_item_")
                    for record in staged.review_records
                )
                for work_item_id in work_items
            }
            try:
                self._before_write("review.lifecycle")
                self._transition_lifecycle(
                    staged_app,
                    "Verification Completed",
                    "HUMAN_REVIEW_COMPLETED",
                    store=staged,
                )
                staged.lifecycle_events[-1]["run_id"] = run_id
                staged_app["route"] = "human_complete"
                for item in plan["items"]:
                    work_item_id = item["work_item_id"]
                    finding_id = item["finding_id"]
                    work_item = work_items[work_item_id]
                    sequence = sequences[work_item_id]
                    sequences[work_item_id] += 1
                    decision_id = self._stable_id(
                        "decision",
                        f"{binding_key}:{command_fingerprint}:{work_item_id}:{finding_id}",
                    )
                    compatibility = (
                        self._review_compatibility_summary(
                            app,
                            run,
                            work_item,
                            reason_code=item["reason_code"],
                        )
                        if app.get("legacy_oracle_outcomes") or (
                            run.get("semantic_differential", {}).get("status")
                            == "bundle_bound"
                        )
                        else None
                    )
                    decision_record = {
                        "record_id": decision_id,
                        "record_type": "human_decision",
                        "decision_id": decision_id,
                        "work_item_id": work_item_id,
                        "application_id": work_item["application_id"],
                        "run_id": work_item["run_id"],
                        "reviewer_subject": principal.subject,
                        "reviewer_role": principal.role,
                        "reviewer_source_id": principal.source_id,
                        "assigned_subject": work_item["assigned_subject"],
                        "cycle": work_item["cycle"],
                        "finding_ids": [finding_id],
                        "evidence_snapshot_id": work_item[
                            "evidence_snapshot_id"
                        ],
                        "release_id": work_item["release_id"],
                        "fixed_context": copy.deepcopy(contexts[work_item_id]),
                        "claim_fence": item["expected_fence"],
                        "lifecycle_revision": staged_app[
                            "lifecycle_revision"
                        ],
                        "evidence_revision": staged_app["evidence_revision"],
                        "submitted_at": submit_time,
                        "schema_version": "human-decision/1",
                        "outcome": item["outcome"],
                        "reason_code": item["reason_code"],
                        "finding_decisions": [
                            {
                                "finding_id": finding_id,
                                "outcome": item["outcome"],
                            }
                        ],
                    }
                    if compatibility is not None:
                        decision_record["compatibility"] = compatibility
                    self._before_write("review.decision")
                    staged.review_records.append(decision_record)
                    self._before_write("review.work_item")
                    staged.review_records.append(
                        {
                            "record_id": self._stable_id(
                                "review_record",
                                f"{work_item_id}:finding-complete:{sequence}:{finding_id}",
                            ),
                            "record_type": "work_item_finding_completed",
                            "sequence": sequence,
                            "work_item_id": work_item_id,
                            "application_id": work_item["application_id"],
                            "run_id": work_item["run_id"],
                            "finding_id": finding_id,
                            "claim_subject": principal.subject,
                            "claim_fence": item["expected_fence"],
                            "decision_id": decision_id,
                            "completed_at": submit_time,
                            "recorded_at": submit_time,
                        }
                    )
                    self._before_write("review.audit")
                    staged.audit_events.append(
                        {
                            "event_id": self._stable_id(
                                "audit", f"human_decision_submitted:{decision_id}"
                            ),
                            "action": "human_decision_submitted",
                            "subject": principal.subject,
                            "role": principal.role,
                            "scope": work_item["visibility_scope"],
                            "source_id": principal.source_id,
                            "application_id": work_item["application_id"],
                            "run_id": work_item["run_id"],
                            "route": "human_complete",
                            "lifecycle_revision": staged_app[
                                "lifecycle_revision"
                            ],
                            "evidence_revision": staged_app["evidence_revision"],
                            "work_item_id": work_item_id,
                            "finding_id": finding_id,
                            "decision_id": decision_id,
                            "outcome": item["outcome"],
                            "reason_code": item["reason_code"],
                            "claim_fence": item["expected_fence"],
                            "result": "accepted",
                            **self._audit_time_fields(staged, now=submit_time),
                        }
                    )
                    result_items.append(
                        {
                            "work_item_id": work_item_id,
                            "finding_id": finding_id,
                            "decision_id": decision_id,
                        }
                    )
                result = {
                    "status": "accepted",
                    "replayed": False,
                    "application_id": app["application_id"],
                    "run_id": run_id,
                    "work_item_ids": list(work_items),
                    "items": result_items,
                    "lifecycle_revision": staged_app["lifecycle_revision"],
                    "evidence_revision": staged_app["evidence_revision"],
                    "route": "human_complete",
                }
                self._before_write("review.idempotency")
                staged.idempotency[binding_key] = (
                    command_fingerprint,
                    copy.deepcopy(result),
                )
                staged.persist()
            except (StaleStoreRevision, _StoreWriteFailure) as error:
                return {
                    "status": "unavailable",
                    "replayed": False,
                    "application_id": app["application_id"],
                    "reason_code": (
                        "AUDIT_UNAVAILABLE"
                        if str(error) == "review.audit"
                        else "STORAGE_UNAVAILABLE"
                    ),
                }
            self._store = staged
            return result

    @staticmethod
    def _review_compatibility_summary(
        app: dict[str, Any],
        run: dict[str, Any],
        work_item: dict[str, Any],
        *,
        reason_code: str,
    ) -> dict[str, Any]:
        differential = run.get("semantic_differential")
        source = app.get("source")
        finding_ids = run.get("finding_ids")
        if differential is not None and differential.get("status") == "bundle_bound":
            # Governed runs reference the immutable validation/migration
            # bundle; the legacy oracle never executes on the target path.
            if (
                not isinstance(differential.get("bundle_id"), str)
                or not differential["bundle_id"]
                or not isinstance(differential.get("bundle_digest"), str)
                or len(differential["bundle_digest"]) != 64
                or not isinstance(differential.get("checks_compared"), int)
                or not isinstance(source, dict)
                or not isinstance(finding_ids, list)
            ):
                raise RuntimeError("review compatibility authority is unavailable")
            differential_bytes = json.dumps(
                differential,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            return {
                "schema_version": "human-review-compatibility/1",
                "differential_source": "s08_validation_bundle",
                "intent": "manual_review",
                "target_reason_code": reason_code,
                "conformance": "bundle_bound",
                "target_context": {
                    "run_id": work_item["run_id"],
                    "evidence_snapshot_id": work_item["evidence_snapshot_id"],
                    "release_id": work_item["release_id"],
                    "source_sha256": source.get("source_sha256")
                    or source.get("source_result_sha256"),
                },
                "fact_counts": {
                    "legacy_checks": 0,
                    "target_findings": len(finding_ids),
                    "checks_compared": differential["checks_compared"],
                    "mismatches": 0,
                },
                "semantic_differential_digest": hashlib.sha256(
                    differential_bytes
                ).hexdigest(),
            }
        legacy_outcomes = app.get("legacy_oracle_outcomes")
        if (
            not isinstance(differential, dict)
            or differential.get("status") not in {"match", "mismatch"}
            or not isinstance(differential.get("checks_compared"), int)
            or not isinstance(differential.get("mismatches"), list)
            or not isinstance(source, dict)
            or not isinstance(legacy_outcomes, (list, tuple))
            or not isinstance(finding_ids, list)
        ):
            raise RuntimeError("review compatibility authority is unavailable")
        source_sha256 = source.get("source_sha256") or source.get(
            "source_result_sha256"
        )
        if not isinstance(source_sha256, str) or not source_sha256:
            raise RuntimeError("review compatibility source binding is unavailable")
        differential_bytes = json.dumps(
            differential,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "schema_version": "human-review-compatibility/1",
            "differential_source": "frozen_admission_oracle",
            "intent": "manual_review",
            "target_reason_code": reason_code,
            "conformance": differential["status"],
            "target_context": {
                "run_id": work_item["run_id"],
                "evidence_snapshot_id": work_item["evidence_snapshot_id"],
                "release_id": work_item["release_id"],
                "source_sha256": source_sha256,
            },
            "fact_counts": {
                "legacy_checks": len(legacy_outcomes),
                "target_findings": len(finding_ids),
                "checks_compared": differential["checks_compared"],
                "mismatches": len(differential["mismatches"]),
            },
            "semantic_differential_digest": hashlib.sha256(
                differential_bytes
            ).hexdigest(),
        }

    @classmethod
    def _canonical_review_verification(
        cls, verification: dict[str, Any]
    ) -> dict[str, Any]:
        required_fields = {
            "schema_version",
            "outcome",
            "reason_code",
            "finding_decisions",
        }
        if not isinstance(verification, dict) or set(verification) not in {
            frozenset(required_fields),
            frozenset(required_fields | {"note"}),
        }:
            raise ValueError("review verification does not match the registered contract")
        allowed_outcomes = {"confirmed", "not_confirmed", "inconclusive"}
        outcome = verification.get("outcome")
        reason_code = verification.get("reason_code")
        decisions = verification.get("finding_decisions")
        note_present = "note" in verification
        note = verification.get("note")
        try:
            note_bytes = note.encode("utf-8") if isinstance(note, str) else b""
        except UnicodeEncodeError as error:
            raise ValueError(
                "review verification contains an invalid structured value"
            ) from error
        if (
            verification.get("schema_version") != "human-decision/1"
            or outcome not in allowed_outcomes
            or reason_code not in cls._REVIEW_REASON_CODES
            or note_present
            and (
                not isinstance(note, str)
                or len(note) > cls._REVIEW_NOTE_MAX_CHARACTERS
                or len(note_bytes) > cls._REVIEW_NOTE_MAX_BYTES
            )
            or not isinstance(decisions, list)
            or not decisions
            or len(decisions) > 100
        ):
            raise ValueError("review verification contains an invalid structured value")
        normalized_decisions: list[dict[str, str]] = []
        finding_ids: set[str] = set()
        for decision in decisions:
            if not isinstance(decision, dict) or set(decision) != {
                "finding_id",
                "outcome",
            }:
                raise ValueError("finding decision does not match the registered contract")
            finding_id = decision.get("finding_id")
            finding_outcome = decision.get("outcome")
            if (
                not isinstance(finding_id, str)
                or not finding_id
                or len(finding_id) > 200
                or finding_id in finding_ids
                or finding_outcome not in allowed_outcomes
            ):
                raise ValueError("finding decision contains an invalid structured value")
            finding_ids.add(finding_id)
            normalized_decisions.append(
                {"finding_id": finding_id, "outcome": finding_outcome}
            )
        normalized = {
            "schema_version": "human-decision/1",
            "outcome": outcome,
            "reason_code": reason_code,
            "finding_decisions": normalized_decisions,
        }
        if note_present:
            normalized["note_metadata"] = {
                "present": True,
                "character_count": len(note),
                "byte_count": len(note_bytes),
                "sha256": hashlib.sha256(note_bytes).hexdigest(),
            }
        return normalized

    @classmethod
    def _review_lifecycle_idempotency(
        cls,
        *,
        action: str,
        work_item_id: str,
        expected_fence: int,
        expected_context: dict[str, Any],
        idempotency_key: str,
    ) -> tuple[str, str]:
        encoded = json.dumps(
            {
                "action": action,
                "expected_context": expected_context,
                "expected_fence": expected_fence,
                "work_item_id": work_item_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprint = hashlib.sha256(encoded).hexdigest()
        if not cls._valid_idempotency_key(idempotency_key):
            raise ValueError("review idempotency key is invalid")
        return idempotency_key, fingerprint

    @staticmethod
    def _review_idempotency_binding_key(
        principal: S01CommandPrincipal,
        work_item_id: str,
        idempotency_key: str,
        *,
        action: str = "submit_review_work_item",
    ) -> str:
        encoded = json.dumps(
            {
                "action": action,
                "key": idempotency_key,
                "role": principal.role,
                "scope": principal.scope,
                "source_id": principal.source_id,
                "subject": principal.subject,
                "work_item_id": work_item_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"s03_idempotency_{hashlib.sha256(encoded).hexdigest()}"

    def reveal_field_observation(
        self,
        *,
        principal: S01CommandPrincipal,
        application_id: str,
        work_item_id: str,
        observation_id: str,
        expected_fence: int,
        expected_context: dict[str, Any],
        idempotency_key: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Reveal one claimed C-DEMO source region after recording its audit fact."""
        reveal_time = int(self._clock() if now is None else now)
        if not self._valid_reviewer_principal(principal, now=reveal_time):
            raise QueryNotFound(work_item_id)
        if (
            not isinstance(application_id, str)
            or not application_id
            or not isinstance(observation_id, str)
            or not observation_id
            or isinstance(expected_fence, bool)
            or not isinstance(expected_fence, int)
            or expected_fence < 1
        ):
            raise ValueError("source reveal command is invalid")

        with self._lock:
            self._reload_store()
            work_item, state = self._review_work_item_authority(
                principal=principal,
                work_item_id=work_item_id,
                now=reveal_time,
            )
            if work_item["application_id"] != application_id:
                raise QueryNotFound(work_item_id)
            app, _, actual_context = self._review_current_context(work_item)
            command_key, command_fingerprint = self._review_lifecycle_idempotency(
                action=(
                    f"reveal_field_observation:{application_id}:{observation_id}"
                ),
                work_item_id=work_item_id,
                expected_fence=expected_fence,
                expected_context=expected_context,
                idempotency_key=idempotency_key,
            )
            binding_key = self._review_idempotency_binding_key(
                principal,
                work_item_id,
                command_key,
                action="reveal_field_observation",
            )
            previous = self._store.idempotency.get(binding_key)
            if previous is not None and previous[0] != command_fingerprint:
                return {
                    "status": "conflict",
                    "replayed": False,
                    "application_id": application_id,
                    "work_item_id": work_item_id,
                    "reason_code": "IDEMPOTENCY_KEY_CONFLICT",
                }
            if not self._review_context_matches(expected_context, actual_context):
                return {
                    "status": "stale",
                    "application_id": application_id,
                    "work_item_id": work_item_id,
                    "reason_code": "STALE_REVIEW_CONTEXT",
                }
            if (
                state["status"] != "claimed"
                or state["claim_subject"] != principal.subject
                or state["claim_fence"] != expected_fence
                or float(state["claim_expires_at"]) <= reveal_time
            ):
                return {
                    "status": "stale",
                    "application_id": application_id,
                    "work_item_id": work_item_id,
                    "reason_code": "STALE_WORK_ITEM_CLAIM",
                }
            if (
                app.get("track") != "C-DEMO"
                or app.get("phase") != "Manual Review"
                or app.get("current_run_id") != work_item["run_id"]
                or app.get("lifecycle_revision") != work_item["lifecycle_revision"]
                or app.get("evidence_revision") != work_item["evidence_revision"]
            ):
                return {
                    "status": "stale",
                    "application_id": application_id,
                    "work_item_id": work_item_id,
                    "reason_code": "STALE_REVIEW_CONTEXT",
                }
            gate = self._review_write_gate(app=app)
            if gate is not None:
                status, reason_code = gate
                return {
                    "status": status,
                    "application_id": application_id,
                    "work_item_id": work_item_id,
                    "reason_code": reason_code,
                }

            links = [
                link
                for finding in self._store.findings
                if finding.get("application_id") == application_id
                and finding.get("run_id") == work_item["run_id"]
                and finding.get("finding_id") in work_item["finding_ids"]
                for link in finding.get("evidence_links", [])
                if link.get("observation_id") == observation_id
            ]
            if not links:
                raise ValueError("source reveal observation is unavailable")
            link = links[0]
            binding = {
                key: link.get(key)
                for key in (
                    "document_id",
                    "document_role",
                    "field",
                    "source_sha256",
                    "source_page",
                    "source_region",
                )
            }
            if any(
                {
                    key: candidate.get(key)
                    for key in binding
                }
                != binding
                for candidate in links[1:]
            ):
                raise RuntimeError("source reveal observation authority is ambiguous")
            selected_documents = [
                document
                for document in self._assemble_evidence(self._admitted_evidence(app))
                if document.get("document_id") == binding["document_id"]
                and document.get("document_role") == binding["document_role"]
            ]
            selected = (
                selected_documents[0].get("fields", {}).get(binding["field"])
                if len(selected_documents) == 1
                and isinstance(selected_documents[0].get("fields"), dict)
                else None
            )
            if (
                not isinstance(selected, dict)
                or selected.get("observation_id") != observation_id
                or any(selected.get(key) != binding[key] for key in (
                    "source_sha256",
                    "source_page",
                    "source_region",
                ))
            ):
                raise ValueError("source reveal observation is not current")

            source = app.get("source")
            if not isinstance(source, dict) or binding["source_sha256"] != source.get(
                "source_sha256"
            ):
                raise RuntimeError("source reveal authority does not match")
            payload, _ = self._read_fixed_scenario(source.get("scenario_id"))
            source_documents = [
                document
                for document in payload["documents"]
                if document.get("doc_id") == binding["document_id"]
                and document.get("doc_type") == binding["document_role"]
            ]
            source_field = (
                source_documents[0].get("fields", {}).get(binding["field"])
                if len(source_documents) == 1
                and isinstance(source_documents[0].get("fields"), dict)
                else None
            )
            source_text = (
                source_field.get("source_text")
                if isinstance(source_field, dict)
                else None
            )
            if (
                not isinstance(source_text, str)
                or not source_text
                or len(source_text) > self._REVIEW_NOTE_MAX_CHARACTERS
                or len(source_text.encode("utf-8")) > self._REVIEW_NOTE_MAX_BYTES
            ):
                return {
                    "status": "rejected",
                    "application_id": application_id,
                    "work_item_id": work_item_id,
                    "reason_code": "SOURCE_REVEAL_UNAVAILABLE",
                }

            if previous is not None:
                return {
                    **copy.deepcopy(previous[1]),
                    "source_text": source_text,
                    "replayed": True,
                }
            result = {
                "status": "revealed",
                "replayed": False,
                "application_id": application_id,
                "work_item_id": work_item_id,
                "observation_id": observation_id,
                "source_location": {
                    "source_sha256": binding["source_sha256"],
                    "source_page": binding["source_page"],
                    "source_region": binding["source_region"],
                },
                "source_text": source_text,
                "revealed_at": reveal_time,
            }
            staged = copy.deepcopy(self._store)
            try:
                self._before_write("reveal.audit")
                staged.audit_events.append(
                    {
                        "event_id": self._stable_id(
                            "audit",
                            f"evidence_source_revealed:{work_item_id}:{observation_id}:{reveal_time}:{len(staged.audit_events) + 1}",
                        ),
                        "action": "evidence_source_revealed",
                        "subject": principal.subject,
                        "role": principal.role,
                        "scope": work_item["visibility_scope"],
                        "source_id": principal.source_id,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "observation_id": observation_id,
                        "finding_id": next(
                            finding["finding_id"]
                            for finding in self._store.findings
                            if finding.get("application_id") == application_id
                            and finding.get("run_id") == work_item["run_id"]
                            and finding.get("finding_id") in work_item["finding_ids"]
                            and any(
                                item.get("observation_id") == observation_id
                                for item in finding.get("evidence_links", [])
                            )
                        ),
                        "result": "revealed",
                        "reason_code": "SOURCE_REVEAL_AUTHORIZED",
                        **self._audit_time_fields(staged, now=reveal_time),
                    }
                )
                self._before_write("reveal.idempotency")
                staged.idempotency[binding_key] = (
                    command_fingerprint,
                    {
                        key: copy.deepcopy(value)
                        for key, value in result.items()
                        if key != "source_text"
                    },
                )
                staged.persist()
            except _StoreWriteFailure as error:
                return {
                    "status": "unavailable",
                    "application_id": application_id,
                    "work_item_id": work_item_id,
                    "reason_code": (
                        "AUDIT_UNAVAILABLE"
                        if str(error) == "reveal.audit"
                        else "STORAGE_UNAVAILABLE"
                    ),
                }
            except StaleStoreRevision:
                self._reload_store()
                return {
                    "status": "stale",
                    "application_id": application_id,
                    "work_item_id": work_item_id,
                    "reason_code": "STALE_REVIEW_CONTEXT",
                }
            except Exception:
                return {
                    "status": "unavailable",
                    "application_id": application_id,
                    "work_item_id": work_item_id,
                    "reason_code": "STORAGE_UNAVAILABLE",
                }
            self._store = staged
            return result

    @classmethod
    def _c_demo_supplement_policy(cls) -> dict[str, Any]:
        policy = {
            "material_requirement_id": cls._SUPPLEMENT_REQUIREMENT_ID,
            "document_role": "financing_lease_contract",
            "material_kind": "financing_lease_contract",
            "operation": "replacement",
            "required_fact_kinds": [
                "attachment",
                "page",
                "producer",
                "vin_observation",
            ],
            "responsible_party": "application_material_provider",
            "allowed_tenant_id": "c-demo",
            "allowed_source_system_ids": ["s06-material-source"],
            "allowed_workload_identity_ids": ["s06-material-workload"],
            "satisfaction_policy_id": cls._SUPPLEMENT_SATISFACTION_POLICY_ID,
            "batch_item_count": 2,
            "batch_closure_required": True,
            "integrity_required": True,
            "provenance_required": True,
            "evidence_eligibility_required": True,
        }
        encoded = json.dumps(
            policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return {**policy, "satisfaction_policy_digest": hashlib.sha256(encoded).hexdigest()}

    def request_supplement(
        self,
        *,
        principal: S01CommandPrincipal,
        work_item_id: str,
        finding_id: str,
        reason_code: str,
        expected_fence: int,
        expected_context: dict[str, Any],
        idempotency_key: str,
        predecessor_request_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Create one Lifecycle-owned request for the fixed C-DEMO material gap."""
        request_time = int(self._clock() if now is None else now)
        if not self._valid_reviewer_principal(principal, now=request_time):
            raise QueryNotFound(work_item_id)
        if (
            not isinstance(finding_id, str)
            or not finding_id
            or finding_id.strip() != finding_id
            or len(finding_id) > 200
            or reason_code != self._SUPPLEMENT_REQUEST_REASON
            or isinstance(expected_fence, bool)
            or not isinstance(expected_fence, int)
            or expected_fence < 1
            or not self._valid_idempotency_key(idempotency_key)
            or predecessor_request_id is not None
        ):
            raise ValueError("supplement request is invalid")
        fingerprint_bytes = json.dumps(
            {
                "expected_context": expected_context,
                "expected_fence": expected_fence,
                "finding_id": finding_id,
                "predecessor_request_id": predecessor_request_id,
                "reason_code": reason_code,
                "work_item_id": work_item_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        command_fingerprint = hashlib.sha256(fingerprint_bytes).hexdigest()
        binding_key = self._review_idempotency_binding_key(
            principal,
            work_item_id,
            idempotency_key,
            action="request_supplement",
        )

        with self._lock:
            for attempt in range(2):
                self._reload_store()
                work_item, state = self._review_work_item_authority(
                    principal=principal,
                    work_item_id=work_item_id,
                    now=request_time,
                )
                app, run, actual_context = self._review_current_context(work_item)
                previous = self._store.idempotency.get(binding_key)
                if previous is not None:
                    if previous[0] == command_fingerprint:
                        return {**copy.deepcopy(previous[1]), "replayed": True}
                    return {
                        "status": "conflict",
                        "replayed": False,
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "IDEMPOTENCY_KEY_CONFLICT",
                    }
                if self._supplement_operations_state()["requests"] == "closed":
                    return {
                        "status": "stopped",
                        "replayed": False,
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "supplement.requests_stopped",
                    }
                if not self._review_context_matches(expected_context, actual_context):
                    return {
                        "status": "stale",
                        "replayed": False,
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "STALE_REVIEW_CONTEXT",
                    }
                if (
                    state["status"] != "claimed"
                    or state["claim_subject"] != principal.subject
                    or state["claim_fence"] != expected_fence
                    or float(state["claim_expires_at"]) <= request_time
                    or app.get("phase") != "Manual Review"
                    or app.get("current_run_id") != work_item["run_id"]
                    or app.get("lifecycle_revision") != work_item["lifecycle_revision"]
                    or app.get("evidence_revision") != work_item["evidence_revision"]
                ):
                    return {
                        "status": "stale",
                        "replayed": False,
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "STALE_REVIEW_CONTEXT",
                    }
                findings = [
                    finding
                    for finding in self._store.findings
                    if finding.get("application_id") == work_item["application_id"]
                    and finding.get("run_id") == work_item["run_id"]
                    and finding.get("finding_id") == finding_id
                ]
                finding = findings[0] if len(findings) == 1 else None
                if (
                    finding is None
                    or finding_id not in work_item["finding_ids"]
                    or finding.get("mandatory") is not True
                    or finding.get("rule_id") != "R_VIN_CROSS"
                    or finding.get("verdict") != "uncertain"
                    or finding.get("reason_code") != "MISSING_DOCS"
                    or app.get("artifact_manifest", {}).get("scenario_id")
                    != "app_missing_vin_docs.json"
                ):
                    return {
                        "status": "rejected",
                        "replayed": False,
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "FINDING_NOT_SUPPLEMENT_ELIGIBLE",
                    }
                terminal_request_ids = {
                    record["request_id"]
                    for record in self._store.review_records
                    if record.get("record_type")
                    in {
                        "supplement_request_fulfilled",
                        "supplement_request_expired",
                        "supplement_request_invalidated",
                    }
                    and isinstance(record.get("request_id"), str)
                }
                active_requests = [
                    record
                    for record in self._store.review_records
                    if record.get("record_type") == "supplement_request"
                    and record.get("application_id") == work_item["application_id"]
                    and record.get("cycle") == work_item["cycle"]
                    and record.get("run_id") == work_item["run_id"]
                    and record.get("finding_id") == finding_id
                    and record.get("material_requirement_id")
                    == self._SUPPLEMENT_REQUIREMENT_ID
                    and record.get("request_id") not in terminal_request_ids
                ]
                if active_requests:
                    return {
                        "status": "conflict",
                        "replayed": False,
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "ACTIVE_SUPPLEMENT_REQUEST_EXISTS",
                    }
                gate = self._review_write_gate(app=app)
                if gate is not None:
                    status, failure = gate
                    return {
                        "status": status,
                        "replayed": False,
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": failure,
                    }
                evidence = self._assemble_evidence(self._admitted_evidence(app))
                lease_documents = [
                    document
                    for document in evidence
                    if document.get("document_role")
                    == self._SUPPLEMENT_TARGET_DOCUMENT_ROLE
                ]
                predecessor_attachment = (
                    lease_documents[0].get("attachment")
                    if len(lease_documents) == 1
                    else None
                )
                if (
                    not isinstance(predecessor_attachment, dict)
                    or not isinstance(
                        predecessor_attachment.get("attachment_id"), str
                    )
                    or predecessor_attachment.get("version") != 1
                ):
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "SOURCE_EVIDENCE_UNAVAILABLE",
                    }

                request_id = self._stable_id(
                    "supplement_request", f"{binding_key}:{command_fingerprint}"
                )
                supplement_work_item_id = self._stable_id(
                    "work", f"supplement:{request_id}"
                )
                policy = self._c_demo_supplement_policy()
                due_at = request_time + self._SUPPLEMENT_TTL_SECONDS
                staged = copy.deepcopy(self._store)
                staged_app = staged.applications[work_item["application_id"]]
                source_sequence = 1 + sum(
                    record.get("work_item_id") == work_item_id
                    and str(record.get("record_type", "")).startswith("work_item_")
                    for record in staged.review_records
                )
                try:
                    self._before_write("supplement_request.lifecycle")
                    staged_app["route"] = "supplement_pending"
                    staged_app["projection_visible"] = False
                    staged_app["projection_pending"] = False
                    self._transition_lifecycle(
                        staged_app,
                        "Supplement",
                        "SUPPLEMENT_REQUESTED",
                        store=staged,
                    )
                    staged.lifecycle_events[-1].update(
                        {
                            "run_id": work_item["run_id"],
                            "request_id": request_id,
                            "finding_id": finding_id,
                            "source_work_item_id": work_item_id,
                        }
                    )
                    spec = run["spec"]
                    request_record = {
                        "record_id": request_id,
                        "record_type": "supplement_request",
                        "schema_version": "supplement-request/1",
                        "request_id": request_id,
                        "work_item_id": supplement_work_item_id,
                        "source_work_item_id": work_item_id,
                        "application_id": work_item["application_id"],
                        "tenant_id": policy["allowed_tenant_id"],
                        "visibility_scope": work_item["visibility_scope"],
                        "cycle": work_item["cycle"],
                        "run_id": work_item["run_id"],
                        "finding_id": finding_id,
                        "rule_id": finding["rule_id"],
                        "finding_reason_code": finding["reason_code"],
                        "finding_verdict": finding["verdict"],
                        "severity": finding["severity"],
                        "evidence_revision": work_item["evidence_revision"],
                        "evidence_snapshot_id": spec["evidence_snapshot_id"],
                        "evidence_snapshot_digest": spec[
                            "evidence_snapshot_digest"
                        ],
                        "release_id": spec["release_id"],
                        "release_digest": spec["release_digest"],
                        "checker_build": spec["checker_build"],
                        "requester_subject": principal.subject,
                        "requester_role": principal.role,
                        "requester_source_id": principal.source_id,
                        "request_reason_code": reason_code,
                        "material_requirement_id": policy[
                            "material_requirement_id"
                        ],
                        "document_role": policy["document_role"],
                        "target_document_role": self._SUPPLEMENT_TARGET_DOCUMENT_ROLE,
                        "material_kind": policy["material_kind"],
                        "operation": policy["operation"],
                        "expected_predecessor_attachment_id": (
                            predecessor_attachment["attachment_id"]
                        ),
                        "expected_predecessor_attachment_version": (
                            predecessor_attachment["version"]
                        ),
                        "responsible_party": policy["responsible_party"],
                        "allowed_source_policy": {
                            "tenant_id": policy["allowed_tenant_id"],
                            "source_system_ids": copy.deepcopy(
                                policy["allowed_source_system_ids"]
                            ),
                            "workload_identity_ids": copy.deepcopy(
                                policy["allowed_workload_identity_ids"]
                            ),
                        },
                        "satisfaction_policy_id": policy[
                            "satisfaction_policy_id"
                        ],
                        "satisfaction_policy_digest": policy[
                            "satisfaction_policy_digest"
                        ],
                        "batch_item_count": policy["batch_item_count"],
                        "required_fact_kinds": copy.deepcopy(
                            policy["required_fact_kinds"]
                        ),
                        "batch_closure_required": policy[
                            "batch_closure_required"
                        ],
                        "integrity_required": policy["integrity_required"],
                        "provenance_required": policy["provenance_required"],
                        "evidence_eligibility_required": policy[
                            "evidence_eligibility_required"
                        ],
                        "requester_claim_fence": expected_fence,
                        "pre_request_lifecycle_revision": work_item[
                            "lifecycle_revision"
                        ],
                        "post_request_lifecycle_revision": staged_app[
                            "lifecycle_revision"
                        ],
                        "expected_evidence_revision": work_item[
                            "evidence_revision"
                        ],
                        "projection_watermark": actual_context[
                            "projection_watermark"
                        ],
                        "fixed_context": copy.deepcopy(actual_context),
                        "requested_at": request_time,
                        "due_at": due_at,
                        "predecessor_request_id": predecessor_request_id,
                        "idempotency_fingerprint": command_fingerprint,
                    }
                    request_bytes = json.dumps(
                        request_record,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    request_record["context_digest"] = hashlib.sha256(
                        request_bytes
                    ).hexdigest()
                    self._before_write("supplement_request.request")
                    staged.review_records.append(request_record)
                    self._before_write("supplement_request.review_work")
                    staged.review_records.append(
                        {
                            "record_id": self._stable_id(
                                "review_record",
                                f"{work_item_id}:supplement:{source_sequence}",
                            ),
                            "record_type": "work_item_invalidated",
                            "sequence": source_sequence,
                            "work_item_id": work_item_id,
                            "application_id": work_item["application_id"],
                            "run_id": work_item["run_id"],
                            "claim_subject": principal.subject,
                            "claim_fence": expected_fence,
                            "request_id": request_id,
                            "reason_code": "SUPPLEMENT_REQUESTED",
                            "invalidated_at": request_time,
                            "recorded_at": request_time,
                        }
                    )
                    self._before_write("supplement_request.work_item")
                    staged.work_items.append(
                        {
                            "work_item_id": supplement_work_item_id,
                            "owner": "Lifecycle",
                            "kind": "supplement",
                            "status": "active",
                            "request_id": request_id,
                            "application_id": work_item["application_id"],
                            "cycle": work_item["cycle"],
                            "run_id": work_item["run_id"],
                            "finding_ids": [finding_id],
                            "material_requirement_id": policy[
                                "material_requirement_id"
                            ],
                            "lifecycle_revision": staged_app[
                                "lifecycle_revision"
                            ],
                            "evidence_revision": work_item["evidence_revision"],
                            "visibility_scope": work_item["visibility_scope"],
                            "responsible_party": policy["responsible_party"],
                            "due_at": due_at,
                        }
                    )
                    self._before_write("supplement_request.audit")
                    staged.audit_events.append(
                        {
                            "event_id": self._stable_id(
                                "audit", f"supplement_requested:{request_id}"
                            ),
                            "action": "supplement_requested",
                            "subject": principal.subject,
                            "role": principal.role,
                            "scope": work_item["visibility_scope"],
                            "source_id": principal.source_id,
                            "application_id": work_item["application_id"],
                            "run_id": work_item["run_id"],
                            "finding_id": finding_id,
                            "request_id": request_id,
                            "work_item_id": supplement_work_item_id,
                            "reason_code": reason_code,
                            "claim_fence": expected_fence,
                            "due_at": due_at,
                            "lifecycle_revision": staged_app[
                                "lifecycle_revision"
                            ],
                            "evidence_revision": staged_app["evidence_revision"],
                            "result": "accepted",
                            **self._audit_time_fields(staged, now=request_time),
                        }
                    )
                    result = {
                        "status": "accepted",
                        "replayed": False,
                        "application_id": work_item["application_id"],
                        "request_id": request_id,
                        "work_item_id": supplement_work_item_id,
                        "finding_id": finding_id,
                        "material_requirement_id": policy[
                            "material_requirement_id"
                        ],
                        "phase": "Supplement",
                        "route": "supplement_pending",
                        "due_at": due_at,
                        "lifecycle_revision": staged_app[
                            "lifecycle_revision"
                        ],
                        "evidence_revision": staged_app["evidence_revision"],
                    }
                    self._before_write("supplement_request.idempotency")
                    staged.idempotency[binding_key] = (
                        command_fingerprint,
                        copy.deepcopy(result),
                    )
                    self._before_write("supplement_request.publish")
                    staged.persist()
                except StaleStoreRevision:
                    if attempt == 0:
                        continue
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "STORAGE_UNAVAILABLE",
                    }
                except _StoreWriteFailure as error:
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "application_id": work_item["application_id"],
                        "work_item_id": work_item_id,
                        "reason_code": (
                            "AUDIT_UNAVAILABLE"
                            if str(error) == "supplement_request.audit"
                            else "STORAGE_UNAVAILABLE"
                        ),
                    }
                self._store = staged
                return result
            raise RuntimeError("supplement request retry exhausted")

    def supplement_request_view(
        self,
        *,
        principal: S01CommandPrincipal,
        request_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Return one request from immutable Lifecycle facts."""
        query_time = float(self._clock() if now is None else now)
        if not self._valid_reviewer_principal(principal, now=query_time):
            raise QueryNotFound(request_id)
        with self._lock:
            self._reload_store()
            requests = [
                record
                for record in self._store.review_records
                if record.get("record_type") == "supplement_request"
                and record.get("request_id") == request_id
            ]
            if len(requests) != 1:
                raise QueryNotFound(request_id)
            request = requests[0]
            app = self._reviewer_application_authority(
                principal, request["application_id"]
            )
            derivation = self._supplement_request_lifecycle_derivation(
                request=request,
                app=app,
                query_time=query_time,
            )
            successors = derivation["successors"]
            status = derivation["status"]
            progress = derivation["progress"]
            expected_evidence_revision = derivation["expected_evidence_revision"]
            current = derivation["current"]
            material_requirement = {
                "material_requirement_id": request["material_requirement_id"],
                "document_role": request["document_role"],
                "material_kind": request["material_kind"],
                "operation": request["operation"],
                "required_fact_kinds": copy.deepcopy(
                    request["required_fact_kinds"]
                ),
                "responsible_party": request["responsible_party"],
                "allowed_tenant_id": request["allowed_source_policy"][
                    "tenant_id"
                ],
                "allowed_source_system_ids": copy.deepcopy(
                    request["allowed_source_policy"]["source_system_ids"]
                ),
                "allowed_workload_identity_ids": copy.deepcopy(
                    request["allowed_source_policy"]["workload_identity_ids"]
                ),
                "satisfaction_policy_id": request["satisfaction_policy_id"],
                "batch_item_count": request["batch_item_count"],
                "batch_closure_required": request["batch_closure_required"],
                "integrity_required": request["integrity_required"],
                "provenance_required": request["provenance_required"],
                "evidence_eligibility_required": request[
                    "evidence_eligibility_required"
                ],
            }
            result = {
                key: copy.deepcopy(request[key])
                for key in (
                    "schema_version",
                    "request_id",
                    "work_item_id",
                    "source_work_item_id",
                    "application_id",
                    "cycle",
                    "run_id",
                    "finding_id",
                    "rule_id",
                    "finding_reason_code",
                    "finding_verdict",
                    "requester_claim_fence",
                    "requested_at",
                    "due_at",
                    "fixed_context",
                    "context_digest",
                    "expected_predecessor_attachment_id",
                    "expected_predecessor_attachment_version",
                    "satisfaction_policy_digest",
                )
            } | {
                "status": status,
                "current": current,
                "phase": app["phase"],
                "route": app["route"],
                "lifecycle_revision": app["lifecycle_revision"],
                "evidence_revision": app["evidence_revision"],
                "projection_watermark": request["projection_watermark"],
                "material_requirement": material_requirement,
            }
            if successors and successors[0].get("reason_code") is not None:
                result["failure"] = {
                    key: copy.deepcopy(successors[0][key])
                    for key in (
                        "reason_code",
                        "responsible_party",
                        "recovery_action",
                        "recovery_target",
                    )
                }
            return result

    def integrator_supplement_request_view(
        self,
        *,
        principal: S01CommandPrincipal,
        request_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Return the minimized current request-binding projection for one
        registered source.

        This is a read-only query projection of Lifecycle-owned facts, not a
        state owner: the registered Integrator principal may read only the
        request whose allowed source policy names its exact tenant scope and
        source system.  Every failure mode (unknown request, wrong role,
        expired identity, wrong tenant, wrong source) is the same sanitized
        ``QueryNotFound``.  The projection carries exactly the fields needed
        to bind the next ``submit_attachment_version`` command and never
        exposes application, reviewer, finding, run, snapshot or policy
        internals.
        """
        query_time = float(self._clock() if now is None else now)
        if not self._registered_principal_live(principal, now=query_time):
            raise QueryNotFound(request_id)
        with self._lock:
            self._reload_store()
            requests = [
                record
                for record in self._store.review_records
                if record.get("record_type") == "supplement_request"
                and record.get("request_id") == request_id
            ]
            if len(requests) != 1:
                raise QueryNotFound(request_id)
            request = requests[0]
            allowed = request["allowed_source_policy"]
            tenant_id = tenant_from_scope(str(principal.scope))
            if (
                tenant_id != allowed["tenant_id"]
                or principal.source_id not in allowed["source_system_ids"]
            ):
                raise QueryNotFound(request_id)
            app = self._store.applications.get(request["application_id"])
            self._require_application_state_authority(app)
            assert app is not None
            derivation = self._supplement_request_lifecycle_derivation(
                request=request,
                app=app,
                query_time=query_time,
            )
            status = derivation["status"]
            progress = derivation["progress"]
            expected_evidence_revision = derivation["expected_evidence_revision"]
            current = derivation["current"]
            material_requirement = {
                "material_requirement_id": request["material_requirement_id"],
                "document_role": request["document_role"],
                "material_kind": request["material_kind"],
                "operation": request["operation"],
                "required_fact_kinds": copy.deepcopy(
                    request["required_fact_kinds"]
                ),
                "responsible_party": request["responsible_party"],
                "allowed_tenant_id": allowed["tenant_id"],
                "allowed_source_system_ids": copy.deepcopy(
                    allowed["source_system_ids"]
                ),
                "allowed_workload_identity_ids": copy.deepcopy(
                    allowed["workload_identity_ids"]
                ),
                "batch_item_count": request["batch_item_count"],
                "batch_closure_required": request["batch_closure_required"],
                "integrity_required": request["integrity_required"],
                "provenance_required": request["provenance_required"],
                "evidence_eligibility_required": request[
                    "evidence_eligibility_required"
                ],
            }
            next_progress_revision = len(progress) + 1
            previous_progress = progress[-1] if progress else None
            batch = (
                {
                    "batch_id": previous_progress["batch_id"],
                    "manifest_digest": previous_progress[
                        "batch_manifest_digest"
                    ],
                    "stream_id": previous_progress["stream_id"],
                }
                if previous_progress is not None
                else {"batch_id": None, "manifest_digest": None, "stream_id": None}
            )
            return {
                "schema_version": "supplement-request-integrator/1",
                "request_id": request_id,
                "status": status,
                "current": current,
                "requested_at": request["requested_at"],
                "due_at": request["due_at"],
                "context_digest": request["context_digest"],
                "upstream_application_ref": app[
                    "upstream_application_reference"
                ],
                "material_requirement": material_requirement,
                "expected_predecessor_attachment_id": request[
                    "expected_predecessor_attachment_id"
                ],
                "expected_predecessor_attachment_version": request[
                    "expected_predecessor_attachment_version"
                ],
                "next_attachment_version": request[
                    "expected_predecessor_attachment_version"
                ]
                + 1,
                "next_request_progress_revision": next_progress_revision,
                "next_source_revision": next_progress_revision,
                "expected_predecessor_revision": (
                    previous_progress["source_revision"]
                    if previous_progress is not None
                    else None
                ),
                "next_batch_item_sequence": next_progress_revision,
                "batch": batch,
            }

    def _supplement_request_lifecycle_derivation(
        self,
        *,
        request: dict[str, Any],
        app: dict[str, Any],
        query_time: float,
    ) -> dict[str, Any]:
        """The single server derivation of a supplement request's terminal
        authority, progress authority, expected evidence revision and
        currentness.  Both the Reviewer and Integrator projections call it
        from immutable Lifecycle facts; each keeps its own authorization and
        minimized response shape."""
        successors = [
            record
            for record in self._store.review_records
            if record.get("request_id") == request["request_id"]
            and record.get("record_type")
            in {
                "supplement_request_fulfilled",
                "supplement_request_expired",
                "supplement_request_invalidated",
            }
        ]
        if len(successors) > 1:
            raise RuntimeError("supplement request terminal authority is not unique")
        status = successors[0]["status"] if successors else "open"
        progress = sorted(
            (
                record
                for record in self._store.review_records
                if record.get("record_type") == "supplement_request_progress"
                and record.get("request_id") == request["request_id"]
            ),
            key=lambda record: int(record["request_progress_revision"]),
        )
        expected_evidence_revision = (
            progress[-1]["evidence_revision"]
            if progress
            else request["expected_evidence_revision"]
        )
        current = (
            status == "open"
            and query_time < float(request["due_at"])
            and app.get("cycle") == request["cycle"]
            and app.get("evidence_revision") == expected_evidence_revision
            and (
                app.get("phase") == "Supplement"
                and app.get("route") == "supplement_pending"
                and not progress
                or app.get("phase") == "Awaiting Evidence"
                and app.get("route") == "awaiting_evidence"
                and bool(progress)
                and app.get("current_run_id") is None
            )
        )
        return {
            "successors": successors,
            "status": status,
            "progress": progress,
            "expected_evidence_revision": expected_evidence_revision,
            "current": current,
        }

    def correct_field_observation(
        self,
        *,
        principal: S01CommandPrincipal,
        application_id: str,
        work_item_id: str,
        expected_fence: int,
        expected_context: dict[str, Any],
        idempotency_key: str,
        correction: dict[str, Any],
        now: float | None = None,
    ) -> dict[str, Any]:
        """Append one source-backed field successor and enqueue a new run."""
        correction_time = int(self._clock() if now is None else now)
        if not self._valid_reviewer_principal(principal, now=correction_time):
            raise QueryNotFound(work_item_id)
        if isinstance(expected_fence, bool) or not isinstance(expected_fence, int) or expected_fence < 1:
            raise ValueError("correction claim fence is invalid")
        if not self._valid_idempotency_key(idempotency_key):
            raise ValueError("correction idempotency key is invalid")
        required = {
            "schema_version",
            "finding_id",
            "observation_id",
            "document_id",
            "document_role",
            "field",
            "raw",
            "source_location",
            "reason_code",
        }
        if not isinstance(correction, dict) or set(correction) != required:
            raise ValueError("field correction does not match the registered contract")
        if correction.get("schema_version") != "field-observation-correction/1":
            raise ValueError("field correction schema version is unsupported")
        for key in (
            "finding_id",
            "observation_id",
            "document_id",
            "document_role",
            "field",
        ):
            value = correction.get(key)
            if not isinstance(value, str) or not value or value.strip() != value:
                raise ValueError(f"field correction {key} is invalid")
        raw = correction.get("raw")
        if (
            not isinstance(raw, str)
            or not raw
            or len(raw) > self._REVIEW_NOTE_MAX_CHARACTERS
            or len(raw.encode("utf-8")) > self._REVIEW_NOTE_MAX_BYTES
        ):
            raise ValueError("field correction raw value is invalid")
        if correction.get("reason_code") not in self._CORRECTION_REASON_CODES:
            raise ValueError("field correction reason is not registered")
        source_location = correction.get("source_location")
        if not isinstance(source_location, dict) or set(source_location) != {
            "source_sha256",
            "source_page",
            "source_region",
        }:
            raise ValueError("field correction source location is invalid")
        source_sha256 = source_location.get("source_sha256")
        source_page = source_location.get("source_page")
        source_region = source_location.get("source_region")
        if (
            not isinstance(source_sha256, str)
            or len(source_sha256) != 64
            or any(character not in "0123456789abcdef" for character in source_sha256)
            or isinstance(source_page, bool)
            or not isinstance(source_page, int)
            or source_page < 1
            or not isinstance(source_region, str)
            or not source_region.startswith("region:")
            or not source_region[7:].isdigit()
        ):
            raise ValueError("field correction source location is invalid")
        normalized = copy.deepcopy(correction)

        with self._lock:
            for attempt in range(2):
                self._reload_store()
                work_item, state = self._review_work_item_authority(
                    principal=principal,
                    work_item_id=work_item_id,
                    now=correction_time,
                )
                if work_item["application_id"] != application_id:
                    raise QueryNotFound(work_item_id)
                app, _, actual_context = self._review_current_context(work_item)
                fingerprint_bytes = json.dumps(
                    {
                        "application_id": application_id,
                        "correction": normalized,
                        "expected_context": expected_context,
                        "expected_fence": expected_fence,
                        "work_item_id": work_item_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                command_fingerprint = hashlib.sha256(fingerprint_bytes).hexdigest()
                binding_key = self._review_idempotency_binding_key(
                    principal,
                    work_item_id,
                    idempotency_key,
                    action="correct_field_observation",
                )
                previous = self._store.idempotency.get(binding_key)
                if previous is not None:
                    previous_fingerprint, previous_result = previous
                    if previous_fingerprint == command_fingerprint:
                        return {**copy.deepcopy(previous_result), "replayed": True}
                    return {
                        "status": "conflict",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": "IDEMPOTENCY_KEY_CONFLICT",
                    }
                if not self._review_context_matches(expected_context, actual_context):
                    return {
                        "status": "stale",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": "STALE_REVIEW_CONTEXT",
                    }
                if (
                    state["status"] != "claimed"
                    or state["claim_subject"] != principal.subject
                    or state["claim_fence"] != expected_fence
                    or float(state["claim_expires_at"]) <= correction_time
                ):
                    return {
                        "status": "stale",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": "STALE_WORK_ITEM_CLAIM",
                    }
                if (
                    app.get("phase") != "Manual Review"
                    or app.get("current_run_id") != work_item["run_id"]
                    or app.get("lifecycle_revision") != work_item["lifecycle_revision"]
                    or app.get("evidence_revision") != work_item["evidence_revision"]
                ):
                    return {
                        "status": "stale",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": "STALE_REVIEW_CONTEXT",
                    }

                findings = [
                    finding
                    for finding in self._store.findings
                    if finding.get("application_id") == application_id
                    and finding.get("run_id") == work_item["run_id"]
                    and finding.get("finding_id") == normalized["finding_id"]
                ]
                if (
                    len(findings) != 1
                    or normalized["finding_id"] not in work_item["finding_ids"]
                    or findings[0].get("mandatory") is not True
                    or findings[0].get("verdict") == "consistent"
                ):
                    raise ValueError("field correction finding is not correctable")
                finding = findings[0]
                links = [
                    link
                    for link in finding["evidence_links"]
                    if link.get("observation_id") == normalized["observation_id"]
                ]
                if len(links) != 1:
                    raise ValueError("field correction observation is not in the finding")
                link = links[0]
                if any(
                    normalized[key] != link.get(key)
                    for key in ("document_id", "document_role", "field")
                ):
                    raise ValueError("field correction semantic binding does not match")
                projected = next(
                    item
                    for item in self._mandatory_blocker_projections(
                        application_id, work_item["run_id"]
                    )
                    if item["finding_id"] == normalized["finding_id"]
                )
                public_links = [
                    item
                    for item in projected["evidence_links"]
                    if item.get("observation_id") == normalized["observation_id"]
                ]
                expected_source = (
                    {
                        key: public_links[0].get(key)
                        for key in ("source_sha256", "source_page", "source_region")
                    }
                    if len(public_links) == 1
                    else None
                )
                if expected_source != source_location:
                    return {
                        "status": "rejected",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": "SOURCE_PROOF_MISMATCH",
                    }
                gate = self._review_write_gate(app=app)
                if gate is not None:
                    status, reason_code = gate
                    return {
                        "status": status,
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": reason_code,
                    }

                evidence = self._admitted_evidence(app)
                graph_documents = [
                    document
                    for document in evidence
                    if document.get("document_id") == normalized["document_id"]
                    and document.get("document_role") == normalized["document_role"]
                ]
                selected_documents = [
                    document
                    for document in self._assemble_evidence(evidence)
                    if document.get("document_id") == normalized["document_id"]
                    and document.get("document_role") == normalized["document_role"]
                ]
                if len(graph_documents) != 1 or len(selected_documents) != 1:
                    raise ValueError("field correction document is unavailable")
                graph_observations = [
                    observation
                    for observation in graph_documents[0].get("observations", [])
                    if isinstance(observation, dict)
                    and observation.get("observation_id")
                    == normalized["observation_id"]
                ]
                fields = selected_documents[0].get("fields")
                if len(graph_observations) == 1:
                    old_observation = graph_observations[0]
                else:
                    old_observation = (
                        fields.get(normalized["field"])
                        if not graph_observations and isinstance(fields, dict)
                        else None
                    )
                if (
                    not isinstance(old_observation, dict)
                    or old_observation.get("observation_id")
                    != normalized["observation_id"]
                    or old_observation.get("source_sha256") != link.get("source_sha256")
                    or old_observation.get("source_page") != link.get("source_page")
                    or old_observation.get("source_region") != link.get("source_region")
                ):
                    raise ValueError("field correction source observation is unavailable")

                correction_id = self._stable_id(
                    "correction", f"{binding_key}:{command_fingerprint}"
                )
                observation_id = self._stable_id(
                    "observation", f"{correction_id}:{normalized['observation_id']}"
                )
                new_observation = {
                    **copy.deepcopy(old_observation),
                    "field": normalized["field"],
                    "raw": normalized["raw"],
                    "raw_type": "string",
                    "raw_lexeme": normalized["raw"],
                    "value_state": "present",
                    "confidence": 1.0,
                    "observation_id": observation_id,
                    "producer_id": "human-reviewer",
                    "producer_version": "1",
                    "evidence_eligible": True,
                    "eligibility_reason": "HUMAN_SOURCE_BACKED_CORRECTION",
                    "supersedes_observation_id": normalized["observation_id"],
                    "correction_id": correction_id,
                    "actor": principal.subject,
                    "recorded_at": correction_time,
                }
                observations = graph_documents[0].setdefault("observations", [])
                if not any(
                    item.get("observation_id") == normalized["observation_id"]
                    for item in observations
                    if isinstance(item, dict)
                ):
                    observations.append(
                        {"field": normalized["field"], **copy.deepcopy(old_observation)}
                    )
                observations.append(copy.deepcopy(new_observation))

                staged = copy.deepcopy(self._store)
                staged_app = staged.applications[application_id]
                next_evidence_revision = int(staged_app["evidence_revision"]) + 1
                evidence_payload = {
                    "schema_version": "s04-corrected-evidence/1",
                    "evidence": evidence,
                    # The admitted graph (including any S10 page-membership
                    # ledger) must survive every Evidence successor or the
                    # correction would silently erase memberships.
                    "graph": copy.deepcopy(self._admitted_graph(app)),
                    "correction": {
                        "correction_id": correction_id,
                        "observation_id": observation_id,
                        "supersedes_observation_id": normalized["observation_id"],
                        "finding_id": normalized["finding_id"],
                        "document_id": normalized["document_id"],
                        "document_role": normalized["document_role"],
                        "field": normalized["field"],
                        "source_location": copy.deepcopy(source_location),
                        "reason_code": normalized["reason_code"],
                        "actor": principal.subject,
                        "recorded_at": correction_time,
                    },
                }
                evidence_bytes = json.dumps(
                    evidence_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                job_id = self._stable_id(
                    "job", f"{application_id}:correction:{correction_id}"
                )
                sequence = 1 + sum(
                    record.get("work_item_id") == work_item_id
                    and str(record.get("record_type", "")).startswith("work_item_")
                    for record in staged.review_records
                )
                old_route = staged_app["route"]
                invalidated_decision_ids = sorted(
                    {
                        record["decision_id"]
                        for record in staged.review_records
                        if record.get("application_id") == application_id
                        and record.get("run_id") == work_item["run_id"]
                        and isinstance(record.get("decision_id"), str)
                    }
                )
                inactive_exception_ids = {
                    record["request_id"]
                    for record in staged.review_records
                    if record.get("record_type")
                    in {
                        "business_exception_expired",
                        "business_exception_invalidated",
                    }
                    and isinstance(record.get("request_id"), str)
                }
                inactive_exception_ids.update(
                    record["request_id"]
                    for record in staged.review_records
                    if record.get("record_type") == "business_exception_decision"
                    and record.get("decision") == "rejected"
                    and isinstance(record.get("request_id"), str)
                )
                active_exception_requests = [
                    record
                    for record in staged.review_records
                    if record.get("record_type") == "business_exception_request"
                    and record.get("application_id") == application_id
                    and record.get("run_id") == work_item["run_id"]
                    and record.get("request_id") not in inactive_exception_ids
                ]
                invalidated_exception_ids = sorted(
                    request["request_id"] for request in active_exception_requests
                )
                try:
                    self._before_write("correction.evidence")
                    staged.evidence_events.append(
                        {
                            "event_id": self._stable_id(
                                "evidence", f"{application_id}:correction:{correction_id}"
                            ),
                            "application_id": application_id,
                            "revision": next_evidence_revision,
                            "kind": "field_correction",
                            "content_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
                            "content_bytes": len(evidence_bytes),
                            "payload": evidence_payload,
                        }
                    )
                    staged_app["evidence_revision"] = next_evidence_revision
                    staged_app["evidence_ready"] = False
                    staged_app["route"] = "pending_check"
                    staged_app["current_run_id"] = None
                    staged_app["current_evidence_snapshot_id"] = None
                    staged_app["current_evidence_snapshot_digest"] = None
                    staged_app["projection_visible"] = False
                    staged_app["projection_pending"] = False
                    self._before_write("correction.lifecycle")
                    self._transition_lifecycle(
                        staged_app,
                        "Assembly",
                        "EVIDENCE_CORRECTION_ACCEPTED",
                        store=staged,
                    )
                    staged.lifecycle_events[-1].update(
                        {
                            "correction_id": correction_id,
                            "invalidated_run_id": work_item["run_id"],
                            "invalidated_route": old_route,
                            "invalidated_work_item_id": work_item_id,
                            "invalidated_decision_ids": invalidated_decision_ids,
                        }
                    )
                    if invalidated_exception_ids:
                        staged.lifecycle_events[-1]["invalidated_exception_ids"] = (
                            invalidated_exception_ids
                        )
                    self._before_write("correction.exception_invalidation")
                    for exception_request in active_exception_requests:
                        exception_id = exception_request["request_id"]
                        staged.review_records.append(
                            {
                                "record_id": self._stable_id(
                                    "exception_invalidation",
                                    f"{exception_id}:correction:{correction_id}",
                                ),
                                "record_type": "business_exception_invalidated",
                                "schema_version": "business-exception-invalidation/1",
                                "request_id": exception_id,
                                "exception_id": exception_id,
                                "work_item_id": exception_request["work_item_id"],
                                "application_id": application_id,
                                "run_id": work_item["run_id"],
                                "finding_id": exception_request["finding_id"],
                                "status": "invalidated",
                                "reason_code": "EVIDENCE_CORRECTION_ACCEPTED",
                                "correction_id": correction_id,
                                "invalidated_at": correction_time,
                                "lifecycle_revision": staged_app[
                                    "lifecycle_revision"
                                ],
                                "evidence_revision": staged_app[
                                    "evidence_revision"
                                ],
                            }
                        )
                        exception_sequence = 1 + sum(
                            record.get("work_item_id")
                            == exception_request["work_item_id"]
                            and str(record.get("record_type", "")).startswith(
                                "exception_work_item_"
                            )
                            for record in staged.review_records
                        )
                        staged.review_records.append(
                            {
                                "record_id": self._stable_id(
                                    "review_record",
                                    f"{exception_request['work_item_id']}:correction:"
                                    f"{exception_sequence}",
                                ),
                                "record_type": "exception_work_item_invalidated",
                                "sequence": exception_sequence,
                                "work_item_id": exception_request["work_item_id"],
                                "request_id": exception_id,
                                "exception_id": exception_id,
                                "application_id": application_id,
                                "run_id": work_item["run_id"],
                                "correction_id": correction_id,
                                "invalidated_at": correction_time,
                                "reason_code": "EVIDENCE_CORRECTION_ACCEPTED",
                                "recorded_at": correction_time,
                            }
                        )
                    self._before_write("correction.work_item")
                    staged.review_records.append(
                        {
                            "record_id": self._stable_id(
                                "review_record", f"{work_item_id}:invalidate:{sequence}"
                            ),
                            "record_type": "work_item_invalidated",
                            "sequence": sequence,
                            "work_item_id": work_item_id,
                            "application_id": application_id,
                            "run_id": work_item["run_id"],
                            "claim_subject": principal.subject,
                            "claim_fence": expected_fence,
                            "correction_id": correction_id,
                            "invalidated_at": correction_time,
                            "recorded_at": correction_time,
                        }
                    )
                    self._before_write("correction.job")
                    staged.jobs.append(
                        self._admission_job_record(job_id, application_id, correction_id)
                    )
                    self._before_write("correction.outbox")
                    staged.outbox.append(
                        {
                            "event_id": self._stable_id("outbox", job_id),
                            "kind": "controlled_check_requested",
                            "application_id": application_id,
                            "job_id": job_id,
                            "fingerprint": correction_id,
                            "status": "pending",
                        }
                    )
                    self._before_write("correction.audit")
                    for exception_request in active_exception_requests:
                        exception_id = exception_request["request_id"]
                        staged.audit_events.append(
                            {
                                "event_id": self._stable_id(
                                    "audit",
                                    f"business_exception_invalidated:"
                                    f"{exception_id}:{correction_id}",
                                ),
                                "action": "business_exception_invalidated",
                                "subject": principal.subject,
                                "role": principal.role,
                                "scope": work_item["visibility_scope"],
                                "source_id": principal.source_id,
                                "application_id": application_id,
                                "run_id": work_item["run_id"],
                                "finding_id": exception_request["finding_id"],
                                "request_id": exception_id,
                                "exception_id": exception_id,
                                "work_item_id": exception_request["work_item_id"],
                                "correction_id": correction_id,
                                "reason_code": "EVIDENCE_CORRECTION_ACCEPTED",
                                "lifecycle_revision": staged_app[
                                    "lifecycle_revision"
                                ],
                                "evidence_revision": staged_app[
                                    "evidence_revision"
                                ],
                                "result": "accepted",
                                **self._audit_time_fields(
                                    staged, now=correction_time
                                ),
                            }
                        )
                    correction_audit = {
                            "event_id": self._stable_id(
                                "audit", f"evidence_correction:{correction_id}"
                            ),
                            "action": "evidence_correction",
                            "subject": principal.subject,
                            "role": principal.role,
                            "scope": work_item["visibility_scope"],
                            "source_id": principal.source_id,
                            "application_id": application_id,
                            "work_item_id": work_item_id,
                            "finding_id": normalized["finding_id"],
                            "old_observation_id": normalized["observation_id"],
                            "new_observation_id": observation_id,
                            "correction_id": correction_id,
                            "invalidated_run_id": work_item["run_id"],
                            "job_id": job_id,
                            "reason_code": normalized["reason_code"],
                            "claim_fence": expected_fence,
                            "lifecycle_revision": staged_app["lifecycle_revision"],
                            "evidence_revision": staged_app["evidence_revision"],
                            "result": "accepted",
                            **self._audit_time_fields(staged, now=correction_time),
                    }
                    if invalidated_exception_ids:
                        correction_audit["invalidated_exception_ids"] = (
                            invalidated_exception_ids
                        )
                    staged.audit_events.append(correction_audit)
                    result = {
                        "status": "accepted",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "correction_id": correction_id,
                        "observation_id": observation_id,
                        "invalidated_run_id": work_item["run_id"],
                        "job_id": job_id,
                        "phase": "Assembly",
                        "route": "pending_check",
                        "lifecycle_revision": staged_app["lifecycle_revision"],
                        "evidence_revision": staged_app["evidence_revision"],
                    }
                    if invalidated_exception_ids:
                        result["invalidated_exception_ids"] = invalidated_exception_ids
                    self._before_write("correction.idempotency")
                    staged.idempotency[binding_key] = (
                        command_fingerprint,
                        copy.deepcopy(result),
                    )
                    self._before_write("correction.publish")
                    staged.persist()
                except _StoreWriteFailure as error:
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": (
                            "AUDIT_UNAVAILABLE"
                            if str(error) == "correction.audit"
                            else "STORAGE_UNAVAILABLE"
                        ),
                    }
                except StaleStoreRevision:
                    if attempt == 0:
                        continue
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": "STORAGE_UNAVAILABLE",
                    }
                except Exception:
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": "STORAGE_UNAVAILABLE",
                    }
                self._store = staged
                return result
            raise RuntimeError("field correction retry exhausted")

    def correct_page_membership(
        self,
        *,
        principal: S01CommandPrincipal,
        application_id: str,
        work_item_id: str,
        expected_fence: int,
        expected_context: dict[str, Any],
        idempotency_key: str,
        membership: dict[str, Any],
        now: float | None = None,
    ) -> dict[str, Any]:
        """Append one source-backed page-membership successor and enqueue a rerun.

        An accepted decision binds the page, an explicit document instance and
        role, the actor, reason, time and the withdrawal/supersession
        predecessors, then advances the Evidence revision and invalidates the
        affected Lifecycle work and readiness.  An explicit ``unassign`` is a
        first-class accepted disposition that withdraws or supersedes an
        accepted membership.  Eligibility for the checker projection comes only
        from these explicit accepted facts; candidate confidence, order, count,
        majority and last write never select a page.
        """
        membership_time = int(self._clock() if now is None else now)
        if not self._valid_reviewer_principal(principal, now=membership_time):
            raise QueryNotFound(work_item_id)
        if (
            isinstance(expected_fence, bool)
            or not isinstance(expected_fence, int)
            or expected_fence < 1
        ):
            raise ValueError("membership claim fence is invalid")
        if not self._valid_idempotency_key(idempotency_key):
            raise ValueError("membership idempotency key is invalid")
        base_contract = {
            "schema_version",
            "finding_id",
            "page_source_sha256",
            "page_ordinal",
            "decision",
            "reason_code",
        }
        if not isinstance(membership, dict):
            raise ValueError("page membership does not match the registered contract")
        decision = membership.get("decision")
        if decision not in self._MEMBERSHIP_DECISIONS:
            raise ValueError("page membership decision is invalid")
        if decision == "accept":
            contract = base_contract | {"document_instance_id", "document_role"}
        else:
            contract = base_contract
        if set(membership) != contract:
            raise ValueError("page membership does not match the registered contract")
        if membership.get("schema_version") != "page-membership-correction/1":
            raise ValueError("page membership schema version is unsupported")
        for key in ("finding_id", "page_source_sha256"):
            value = membership.get(key)
            if not isinstance(value, str) or not value or value.strip() != value:
                raise ValueError(f"page membership {key} is invalid")
        page_source_sha256 = membership["page_source_sha256"]
        if len(page_source_sha256) != 64 or any(
            character not in "0123456789abcdef"
            for character in page_source_sha256
        ):
            raise ValueError("page membership target is invalid")
        page_ordinal = membership.get("page_ordinal")
        if (
            isinstance(page_ordinal, bool)
            or not isinstance(page_ordinal, int)
            or page_ordinal < 1
        ):
            raise ValueError("page membership target is invalid")
        reason_code = membership.get("reason_code")
        if reason_code not in self._MEMBERSHIP_REASON_CODES:
            raise ValueError("page membership reason is not registered")
        instance_id: str | None = None
        role: str | None = None
        if decision == "accept":
            instance_id = membership.get("document_instance_id")
            role = membership.get("document_role")
            if (
                not isinstance(instance_id, str)
                or not instance_id
                or instance_id.strip() != instance_id
                or not isinstance(role, str)
                or not role
                or role.strip() != role
            ):
                # Ambiguous role input is a validation conflict with no revision.
                raise ValueError("page membership role decision is ambiguous")
        normalized = copy.deepcopy(membership)

        with self._lock:
            for attempt in range(2):
                self._reload_store()
                work_item, state = self._review_work_item_authority(
                    principal=principal,
                    work_item_id=work_item_id,
                    now=membership_time,
                )
                if work_item["application_id"] != application_id:
                    raise QueryNotFound(work_item_id)
                app, _, actual_context = self._review_current_context(work_item)
                fingerprint_bytes = json.dumps(
                    {
                        "application_id": application_id,
                        "membership": normalized,
                        "expected_context": expected_context,
                        "expected_fence": expected_fence,
                        "work_item_id": work_item_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                command_fingerprint = hashlib.sha256(fingerprint_bytes).hexdigest()
                binding_key = self._review_idempotency_binding_key(
                    principal,
                    work_item_id,
                    idempotency_key,
                    action="correct_page_membership",
                )
                previous = self._store.idempotency.get(binding_key)
                if previous is not None:
                    previous_fingerprint, previous_result = previous
                    if previous_fingerprint == command_fingerprint:
                        return {**copy.deepcopy(previous_result), "replayed": True}
                    return {
                        "status": "conflict",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": "IDEMPOTENCY_KEY_CONFLICT",
                    }
                if not self._review_context_matches(expected_context, actual_context):
                    return {
                        "status": "stale",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": "STALE_REVIEW_CONTEXT",
                    }
                if (
                    state["status"] != "claimed"
                    or state["claim_subject"] != principal.subject
                    or state["claim_fence"] != expected_fence
                    or float(state["claim_expires_at"]) <= membership_time
                ):
                    return {
                        "status": "stale",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": "STALE_WORK_ITEM_CLAIM",
                    }
                if (
                    app.get("phase") != "Manual Review"
                    or app.get("current_run_id") != work_item["run_id"]
                    or app.get("lifecycle_revision") != work_item["lifecycle_revision"]
                    or app.get("evidence_revision") != work_item["evidence_revision"]
                ):
                    return {
                        "status": "stale",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": "STALE_REVIEW_CONTEXT",
                    }

                findings = [
                    finding
                    for finding in self._store.findings
                    if finding.get("application_id") == application_id
                    and finding.get("run_id") == work_item["run_id"]
                    and finding.get("finding_id") == normalized["finding_id"]
                ]
                if (
                    len(findings) != 1
                    or normalized["finding_id"] not in work_item["finding_ids"]
                    or findings[0].get("mandatory") is not True
                    or findings[0].get("verdict") == "consistent"
                    or findings[0].get("rule_id") not in self._MEMBERSHIP_RULE_IDS
                ):
                    raise ValueError("page membership finding is not correctable")
                gate = self._review_write_gate(app=app)
                if gate is not None:
                    status, gate_reason_code = gate
                    return {
                        "status": status,
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": gate_reason_code,
                    }

                graph = self._admitted_graph(app)
                memberships = (
                    graph.get("page_memberships")
                    if isinstance(graph, dict)
                    else None
                )
                if not isinstance(memberships, list) or not memberships:
                    return {
                        "status": "rejected",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": "MEMBERSHIP_LEDGER_UNAVAILABLE",
                    }
                # Locality: the target page must belong to this application
                # boundary -- an admitted attachment page or a declared ledger
                # page.  A page from another attachment or application cannot
                # become a target.
                attachment_shas: set[str] = set()
                if isinstance(graph, dict):
                    attachments = graph.get("attachments")
                    if isinstance(attachments, list):
                        attachment_shas = {
                            item.get("source_sha256")
                            for item in attachments
                            if isinstance(item, dict)
                            and isinstance(item.get("source_sha256"), str)
                        }
                ledger_pages = {
                    record["page"]["source_sha256"]
                    for record in memberships
                    if isinstance(record, dict)
                    and isinstance(record.get("page"), dict)
                    and isinstance(record["page"].get("source_sha256"), str)
                }
                if (
                    page_source_sha256 not in attachment_shas
                    and page_source_sha256 not in ledger_pages
                ):
                    return {
                        "status": "rejected",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": "MEMBERSHIP_PAGE_OUTSIDE_APPLICATION",
                    }
                candidates = [
                    record
                    for record in memberships
                    if isinstance(record, dict)
                    and record.get("record_kind") == "candidate"
                    and isinstance(record.get("page"), dict)
                    and record["page"].get("source_sha256") == page_source_sha256
                ]
                if not candidates:
                    return {
                        "status": "rejected",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": "MEMBERSHIP_CLAIM_MISSING",
                    }
                finding_membership = findings[0].get("membership")
                if (
                    not isinstance(finding_membership, dict)
                    or finding_membership.get("page_source_sha256")
                    != page_source_sha256
                ):
                    raise ValueError(
                        "page membership finding does not match the target page"
                    )

                staged = copy.deepcopy(self._store)
                staged_app = staged.applications[application_id]
                next_evidence_revision = int(staged_app["evidence_revision"]) + 1
                correction_id = self._stable_id(
                    "membership", f"{binding_key}:{command_fingerprint}"
                )
                decision_id = self._stable_id("decision", f"{correction_id}")
                superseded = sorted(
                    {
                        record["decision_id"]
                        for record in memberships
                        if isinstance(record, dict)
                        and record.get("record_kind") in {"accepted", "unassigned"}
                        and record.get("status") == "active"
                        and isinstance(record.get("page"), dict)
                        and record["page"].get("source_sha256") == page_source_sha256
                        and isinstance(record.get("decision_id"), str)
                    }
                )
                updated_memberships = copy.deepcopy(memberships)
                for record in updated_memberships:
                    if (
                        isinstance(record, dict)
                        and record.get("decision_id") in set(superseded)
                    ):
                        record["status"] = "superseded"
                successor: dict[str, Any] = {
                    "record_kind": (
                        "accepted" if decision == "accept" else "unassigned"
                    ),
                    "decision_id": decision_id,
                    "membership_id": correction_id,
                    "application_id": application_id,
                    "page": {
                        "source_sha256": page_source_sha256,
                        "page_ordinal": page_ordinal,
                    },
                    "actor": principal.subject,
                    "reason_code": reason_code,
                    "time": membership_time,
                    "source_evidence": {
                        "evidence_revision": next_evidence_revision,
                        "event_id": self._stable_id(
                            "evidence",
                            f"{application_id}:membership:{correction_id}",
                        ),
                    },
                    "supersedes": superseded,
                    "status": "active",
                }
                if decision == "accept":
                    successor["document_instance_id"] = instance_id
                    successor["document_role"] = role
                updated_memberships.append(successor)

                updated_graph = (
                    copy.deepcopy(graph) if isinstance(graph, dict) else {}
                )
                updated_graph["page_memberships"] = updated_memberships

                evidence = self._admitted_evidence(app)
                evidence_payload = {
                    "schema_version": "s10-corrected-evidence/1",
                    "evidence": evidence,
                    "graph": updated_graph,
                    "correction": {
                        "correction_id": correction_id,
                        "kind": "page_membership",
                        "decision_id": decision_id,
                        "page_source_sha256": page_source_sha256,
                        "page_ordinal": page_ordinal,
                        "decision": decision,
                        "document_instance_id": instance_id,
                        "document_role": role,
                        "reason_code": reason_code,
                        "actor": principal.subject,
                        "recorded_at": membership_time,
                        "supersedes": superseded,
                    },
                }
                evidence_bytes = json.dumps(
                    evidence_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                job_id = self._stable_id(
                    "job", f"{application_id}:membership:{correction_id}"
                )
                sequence = 1 + sum(
                    record.get("work_item_id") == work_item_id
                    and str(record.get("record_type", "")).startswith("work_item_")
                    for record in staged.review_records
                )
                old_route = staged_app["route"]
                invalidated_decision_ids = sorted(
                    {
                        record["decision_id"]
                        for record in staged.review_records
                        if record.get("application_id") == application_id
                        and record.get("run_id") == work_item["run_id"]
                        and isinstance(record.get("decision_id"), str)
                    }
                )
                try:
                    self._before_write("membership.evidence")
                    staged.evidence_events.append(
                        {
                            "event_id": self._stable_id(
                                "evidence",
                                f"{application_id}:membership:{correction_id}",
                            ),
                            "application_id": application_id,
                            "revision": next_evidence_revision,
                            "kind": "membership_correction",
                            "content_sha256": hashlib.sha256(
                                evidence_bytes
                            ).hexdigest(),
                            "content_bytes": len(evidence_bytes),
                            "payload": evidence_payload,
                        }
                    )
                    staged_app["evidence_revision"] = next_evidence_revision
                    staged_app["evidence_ready"] = False
                    staged_app["route"] = "pending_check"
                    staged_app["current_run_id"] = None
                    staged_app["current_evidence_snapshot_id"] = None
                    staged_app["current_evidence_snapshot_digest"] = None
                    staged_app["projection_visible"] = False
                    staged_app["projection_pending"] = False
                    self._before_write("membership.lifecycle")
                    self._transition_lifecycle(
                        staged_app,
                        "Assembly",
                        "MEMBERSHIP_CORRECTION_ACCEPTED",
                        store=staged,
                    )
                    staged.lifecycle_events[-1].update(
                        {
                            "correction_id": correction_id,
                            "membership_decision_id": decision_id,
                            "page_source_sha256": page_source_sha256,
                            "invalidated_run_id": work_item["run_id"],
                            "invalidated_route": old_route,
                            "invalidated_work_item_id": work_item_id,
                            "invalidated_decision_ids": invalidated_decision_ids,
                        }
                    )
                    self._before_write("membership.work_item")
                    staged.review_records.append(
                        {
                            "record_id": self._stable_id(
                                "review_record",
                                f"{work_item_id}:membership:{sequence}",
                            ),
                            "record_type": "work_item_invalidated",
                            "sequence": sequence,
                            "work_item_id": work_item_id,
                            "application_id": application_id,
                            "run_id": work_item["run_id"],
                            "claim_subject": principal.subject,
                            "claim_fence": expected_fence,
                            "correction_id": correction_id,
                            "membership_decision_id": decision_id,
                            "invalidated_at": membership_time,
                            "recorded_at": membership_time,
                        }
                    )
                    self._before_write("membership.job")
                    staged.jobs.append(
                        self._admission_job_record(
                            job_id, application_id, correction_id
                        )
                    )
                    self._before_write("membership.outbox")
                    staged.outbox.append(
                        {
                            "event_id": self._stable_id("outbox", job_id),
                            "kind": "controlled_check_requested",
                            "application_id": application_id,
                            "job_id": job_id,
                            "fingerprint": correction_id,
                            "status": "pending",
                        }
                    )
                    self._before_write("membership.audit")
                    staged.audit_events.append(
                        {
                            "event_id": self._stable_id(
                                "audit", f"page_membership:{decision_id}"
                            ),
                            "action": "page_membership_corrected",
                            "subject": principal.subject,
                            "role": principal.role,
                            "scope": work_item["visibility_scope"],
                            "source_id": principal.source_id,
                            "application_id": application_id,
                            "work_item_id": work_item_id,
                            "finding_id": normalized["finding_id"],
                            "page_source_sha256": page_source_sha256,
                            "invalidated_run_id": work_item["run_id"],
                            "decision_id": decision_id,
                            "correction_id": correction_id,
                            "job_id": job_id,
                            "reason_code": reason_code,
                            "claim_fence": expected_fence,
                            "lifecycle_revision": staged_app[
                                "lifecycle_revision"
                            ],
                            "evidence_revision": staged_app["evidence_revision"],
                            "result": "accepted",
                            **self._audit_time_fields(staged, now=membership_time),
                        }
                    )
                    result = {
                        "status": "accepted",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "correction_id": correction_id,
                        "membership_decision_id": decision_id,
                        "page_source_sha256": page_source_sha256,
                        "decision": decision,
                        "document_instance_id": instance_id,
                        "document_role": role,
                        "invalidated_run_id": work_item["run_id"],
                        "job_id": job_id,
                        "phase": "Assembly",
                        "route": "pending_check",
                        "lifecycle_revision": staged_app["lifecycle_revision"],
                        "evidence_revision": staged_app["evidence_revision"],
                    }
                    self._before_write("membership.idempotency")
                    staged.idempotency[binding_key] = (
                        command_fingerprint,
                        copy.deepcopy(result),
                    )
                    self._before_write("membership.publish")
                    staged.persist()
                except _StoreWriteFailure as error:
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": (
                            "AUDIT_UNAVAILABLE"
                            if str(error) == "membership.audit"
                            else "STORAGE_UNAVAILABLE"
                        ),
                    }
                except StaleStoreRevision:
                    if attempt == 0:
                        continue
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": "STORAGE_UNAVAILABLE",
                    }
                except Exception:
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "application_id": application_id,
                        "work_item_id": work_item_id,
                        "reason_code": "STORAGE_UNAVAILABLE",
                    }
                self._store = staged
                return result
            raise RuntimeError("membership correction retry exhausted")

    @classmethod
    def _valid_exception_approver_principal(
        cls, principal: S01CommandPrincipal, *, now: float
    ) -> bool:
        return (
            isinstance(principal.subject, str)
            and bool(principal.subject)
            and principal.subject.strip() == principal.subject
            and principal.role == "exception_approver"
            and cls.is_c_demo_scope(principal.scope)
            and isinstance(principal.source_id, str)
            and bool(principal.source_id)
            and principal.source_id.strip() == principal.source_id
            and (
                principal.expires_at is None
                or not isinstance(principal.expires_at, bool)
                and isinstance(principal.expires_at, (int, float))
                and float(principal.expires_at) > now
            )
        )

    @classmethod
    def _valid_exception_router_principal(
        cls, principal: S01CommandPrincipal, *, now: float
    ) -> bool:
        return (
            isinstance(principal.subject, str)
            and bool(principal.subject)
            and principal.subject.strip() == principal.subject
            and principal.role == "operator"
            and cls.is_c_demo_scope(principal.scope)
            and isinstance(principal.source_id, str)
            and bool(principal.source_id)
            and principal.source_id.strip() == principal.source_id
            and (
                principal.expires_at is None
                or not isinstance(principal.expires_at, bool)
                and isinstance(principal.expires_at, (int, float))
                and float(principal.expires_at) > now
            )
        )

    @staticmethod
    def _exception_idempotency_binding_key(
        principal: S01CommandPrincipal,
        target_id: str,
        idempotency_key: str,
        *,
        action: str,
    ) -> str:
        encoded = json.dumps(
            {
                "action": action,
                "key": idempotency_key,
                "role": principal.role,
                "scope": principal.scope,
                "source_id": principal.source_id,
                "subject": principal.subject,
                "target_id": target_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"s05_idempotency_{hashlib.sha256(encoded).hexdigest()}"

    def _business_exception_request_authority(
        self, request_id: str
    ) -> dict[str, Any]:
        requests = [
            record
            for record in self._store.review_records
            if record.get("record_type") == "business_exception_request"
            and record.get("request_id") == request_id
        ]
        if len(requests) != 1:
            raise QueryNotFound(request_id)
        return requests[0]

    def _business_exception_operations_state(
        self, store: _TargetStore | None = None
    ) -> dict[str, Any]:
        owner = self._store if store is None else store
        facts = sorted(
            (
                record
                for record in owner.review_records
                if record.get("record_type")
                == "business_exception_operations_changed"
            ),
            key=lambda record: int(record["sequence"]),
        )
        if [fact.get("sequence") for fact in facts] != list(
            range(1, len(facts) + 1)
        ):
            raise RuntimeError("business exception operations authority is not contiguous")
        if not facts:
            return {
                "operations": "open",
                "revision": 0,
                "reason_code": self._EXCEPTION_OPERATIONS_RESUMED,
                "changed_at": None,
            }
        latest = facts[-1]
        if (
            latest.get("operations") not in {"open", "closed"}
            or latest.get("revision") != latest.get("sequence")
            or not isinstance(latest.get("changed_at"), (int, float))
            or isinstance(latest.get("changed_at"), bool)
        ):
            raise RuntimeError("business exception operations authority is invalid")
        return {
            "operations": latest["operations"],
            "revision": latest["revision"],
            "reason_code": latest["reason_code"],
            "changed_at": latest["changed_at"],
        }

    def _unresolved_business_exception_ids(
        self, store: _TargetStore | None = None
    ) -> list[str]:
        owner = self._store if store is None else store
        inactive = {
            str(record["request_id"])
            for record in owner.review_records
            if record.get("record_type")
            in {
                "business_exception_expired",
                "business_exception_invalidated",
            }
            and isinstance(record.get("request_id"), str)
        }
        inactive.update(
            str(record["request_id"])
            for record in owner.review_records
            if record.get("record_type") == "business_exception_decision"
            and record.get("decision") == "rejected"
            and isinstance(record.get("request_id"), str)
        )
        return sorted(
            str(request["request_id"])
            for request in owner.review_records
            if request.get("record_type") == "business_exception_request"
            and request.get("request_id") not in inactive
            and owner.applications.get(str(request.get("application_id")), {}).get(
                "phase"
            )
            != "Verification Completed"
        )

    def business_exception_operations_status(
        self,
        *,
        principal: S01CommandPrincipal,
        now: float | None = None,
    ) -> dict[str, Any]:
        query_time = int(self._clock() if now is None else now)
        if not self._valid_exception_router_principal(principal, now=query_time):
            raise QueryNotFound("business_exception_operations")
        with self._lock:
            self._reload_store()
            state = self._business_exception_operations_state()
            return {
                **state,
                "unresolved_request_count": len(
                    self._unresolved_business_exception_ids()
                ),
            }

    def close_business_exception_operations(
        self,
        *,
        principal: S01CommandPrincipal,
        idempotency_key: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        return self._change_business_exception_operations(
            principal=principal,
            operations="closed",
            idempotency_key=idempotency_key,
            event_time=int(self._clock() if now is None else now),
        )

    def resume_business_exception_operations(
        self,
        *,
        principal: S01CommandPrincipal,
        idempotency_key: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        return self._change_business_exception_operations(
            principal=principal,
            operations="open",
            idempotency_key=idempotency_key,
            event_time=int(self._clock() if now is None else now),
        )

    def _change_business_exception_operations(
        self,
        *,
        principal: S01CommandPrincipal,
        operations: str,
        idempotency_key: str,
        event_time: int,
    ) -> dict[str, Any]:
        if not self._valid_exception_router_principal(principal, now=event_time):
            raise QueryNotFound("business_exception_operations")
        if operations not in {"open", "closed"} or not self._valid_idempotency_key(
            idempotency_key
        ):
            raise ValueError("business exception operations command is invalid")
        reason_code = (
            self._EXCEPTION_OPERATIONS_CLOSED
            if operations == "closed"
            else self._EXCEPTION_OPERATIONS_RESUMED
        )
        command_fingerprint = hashlib.sha256(
            json.dumps(
                {"operations": operations},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        binding_key = self._exception_idempotency_binding_key(
            principal,
            "business_exception_operations",
            idempotency_key,
            action=f"{operations}_business_exception_operations",
        )
        with self._lock:
            for attempt in range(2):
                self._reload_store()
                previous = self._store.idempotency.get(binding_key)
                if previous is not None:
                    if previous[0] == command_fingerprint:
                        return {**copy.deepcopy(previous[1]), "replayed": True}
                    return {
                        "status": "conflict",
                        "replayed": False,
                        "reason_code": "IDEMPOTENCY_KEY_CONFLICT",
                    }
                current = self._business_exception_operations_state()
                unresolved = self._unresolved_business_exception_ids()
                cohort_stop = self._store.cohort_stop
                if (
                    operations == "open"
                    and cohort_stop is not None
                    and cohort_stop.get("reason_code") == self._RUNTIME_STOP_REASON
                ):
                    return {
                        "status": "stopped",
                        "replayed": False,
                        "operations": current["operations"],
                        "revision": current["revision"],
                        "unresolved_request_count": len(unresolved),
                        "reason_code": "S01_RUNTIME_REPAIR_NOT_VERIFIED",
                    }
                if operations == "open" and unresolved:
                    return {
                        "status": "stopped",
                        "replayed": False,
                        "operations": "closed",
                        "revision": current["revision"],
                        "unresolved_request_count": len(unresolved),
                        "reason_code": "BUSINESS_EXCEPTION_DRAIN_INCOMPLETE",
                    }
                if current["operations"] == operations:
                    return {
                        "status": "accepted",
                        "replayed": False,
                        "operations": operations,
                        "revision": current["revision"],
                        "changed_at": current["changed_at"],
                        "unresolved_request_count": len(unresolved),
                        "invalidated_request_ids": [],
                        "reason_code": current["reason_code"],
                        "unchanged": True,
                    }
                if not self.audit_available:
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "reason_code": "AUDIT_UNAVAILABLE",
                    }
                if not self.storage_available:
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "reason_code": "STORAGE_UNAVAILABLE",
                    }
                staged = copy.deepcopy(self._store)
                sequence = int(current["revision"]) + 1
                record_id = self._stable_id(
                    "exception_operations",
                    f"{sequence}:{operations}:{event_time}",
                )
                unresolved_requests = [
                    next(
                        record
                        for record in self._store.review_records
                        if record.get("record_type")
                        == "business_exception_request"
                        and record.get("request_id") == request_id
                    )
                    for request_id in unresolved
                ]
                try:
                    self._before_write("exception_operations.record")
                    staged.review_records.append(
                        {
                            "record_id": record_id,
                            "record_type": "business_exception_operations_changed",
                            "schema_version": "business-exception-operations/1",
                            "sequence": sequence,
                            "revision": sequence,
                            "operations": operations,
                            "reason_code": reason_code,
                            "changed_at": event_time,
                        }
                    )
                    close_outcomes: list[dict[str, Any]] = []
                    if operations == "closed":
                        for request in unresolved_requests:
                            staged_app = staged.applications[
                                request["application_id"]
                            ]
                            staged_runs = [
                                run
                                for run in staged.runs
                                if run.get("application_id")
                                == request["application_id"]
                                and run.get("run_id") == request["run_id"]
                                and run.get("status") == "complete"
                            ]
                            if (
                                len(staged_runs) != 1
                                or staged_app.get("phase")
                                not in {
                                    "Pending Exception Approval",
                                    "Routing Determination",
                                    "Manual Review",
                                }
                            ):
                                raise _StoreWriteFailure(
                                    "exception_operations.lifecycle"
                                )
                            staged_run = staged_runs[0]
                            current_manual_work = [
                                item
                                for item in staged.work_items
                                if item.get("application_id")
                                == request["application_id"]
                                and item.get("run_id") == request["run_id"]
                                and item.get("kind") == "manual_review"
                                and item.get("lifecycle_revision")
                                == staged_app.get("lifecycle_revision")
                            ]
                            self._before_write("exception_operations.lifecycle")
                            staged_app["route"] = "manual_review"
                            self._transition_lifecycle(
                                staged_app,
                                "Manual Review",
                                "BUSINESS_EXCEPTION_INVALIDATED",
                                store=staged,
                            )
                            staged.lifecycle_events[-1].update(
                                {
                                    "run_id": request["run_id"],
                                    "request_id": request["request_id"],
                                    "exception_id": request["request_id"],
                                    "expires_at": request["expires_at"],
                                    "invalidation_reason_code": reason_code,
                                    "operations_revision": sequence,
                                }
                            )
                            invalidation_record_id = self._stable_id(
                                "exception_invalidation",
                                f"{request['request_id']}:operations:{sequence}",
                            )
                            self._before_write("exception_operations.invalidation")
                            staged.review_records.append(
                                {
                                    "record_id": invalidation_record_id,
                                    "record_type": "business_exception_invalidated",
                                    "schema_version": (
                                        "business-exception-invalidation/1"
                                    ),
                                    "request_id": request["request_id"],
                                    "exception_id": request["request_id"],
                                    "work_item_id": request["work_item_id"],
                                    "application_id": request["application_id"],
                                    "run_id": request["run_id"],
                                    "finding_id": request["finding_id"],
                                    "status": "invalidated",
                                    "reason_code": reason_code,
                                    "operations_revision": sequence,
                                    "invalidated_at": event_time,
                                    "lifecycle_revision": staged_app[
                                        "lifecycle_revision"
                                    ],
                                    "evidence_revision": staged_app[
                                        "evidence_revision"
                                    ],
                                }
                            )
                            exception_sequence = 1 + sum(
                                record.get("work_item_id")
                                == request["work_item_id"]
                                and str(record.get("record_type", "")).startswith(
                                    "exception_work_item_"
                                )
                                for record in staged.review_records
                            )
                            invalidated_claim_fence = max(
                                (
                                    int(record["claim_fence"])
                                    for record in staged.review_records
                                    if record.get("work_item_id")
                                    == request["work_item_id"]
                                    and record.get("record_type")
                                    == "exception_work_item_claimed"
                                ),
                                default=0,
                            )
                            self._before_write("exception_operations.claim_fence")
                            staged.review_records.append(
                                {
                                    "record_id": self._stable_id(
                                        "review_record",
                                        f"{request['work_item_id']}:operations:"
                                        f"{exception_sequence}",
                                    ),
                                    "record_type": (
                                        "exception_work_item_invalidated"
                                    ),
                                    "sequence": exception_sequence,
                                    "work_item_id": request["work_item_id"],
                                    "request_id": request["request_id"],
                                    "exception_id": request["request_id"],
                                    "application_id": request["application_id"],
                                    "run_id": request["run_id"],
                                    "invalidated_claim_fence": (
                                        invalidated_claim_fence
                                    ),
                                    "invalidated_at": event_time,
                                    "reason_code": reason_code,
                                    "recorded_at": event_time,
                                }
                            )
                            for manual_work in current_manual_work:
                                manual_sequence = 1 + sum(
                                    record.get("work_item_id")
                                    == manual_work["work_item_id"]
                                    and str(
                                        record.get("record_type", "")
                                    ).startswith("work_item_")
                                    for record in staged.review_records
                                )
                                staged.review_records.append(
                                    {
                                        "record_id": self._stable_id(
                                            "review_record",
                                            f"{manual_work['work_item_id']}:"
                                            f"operations:{manual_sequence}",
                                        ),
                                        "record_type": "work_item_invalidated",
                                        "sequence": manual_sequence,
                                        "work_item_id": manual_work["work_item_id"],
                                        "application_id": request[
                                            "application_id"
                                        ],
                                        "run_id": request["run_id"],
                                        "request_id": request["request_id"],
                                        "exception_id": request["request_id"],
                                        "invalidated_at": event_time,
                                        "reason_code": reason_code,
                                        "recorded_at": event_time,
                                    }
                                )
                            blocker_ids = [
                                finding["finding_id"]
                                for finding in staged.findings
                                if finding.get("application_id")
                                == request["application_id"]
                                and finding.get("run_id") == request["run_id"]
                                and finding.get("mandatory") is True
                                and finding.get("verdict") != "consistent"
                            ]
                            self._before_write(
                                "exception_operations.review_successor"
                            )
                            successor_work_item_id = (
                                self._create_manual_review_successor(
                                    staged,
                                    staged_app,
                                    staged_run,
                                    finding_ids=blocker_ids,
                                    predecessor_request_id=request["request_id"],
                                )
                            )
                            close_outcomes.append(
                                {
                                    "request": request,
                                    "invalidation_record_id": (
                                        invalidation_record_id
                                    ),
                                    "invalidated_claim_fence": (
                                        invalidated_claim_fence
                                    ),
                                    "successor_work_item_id": (
                                        successor_work_item_id
                                    ),
                                    "lifecycle_revision": staged_app[
                                        "lifecycle_revision"
                                    ],
                                    "evidence_revision": staged_app[
                                        "evidence_revision"
                                    ],
                                }
                            )
                    self._before_write("exception_operations.audit")
                    for outcome in close_outcomes:
                        request = outcome["request"]
                        staged.audit_events.append(
                            {
                                "event_id": self._stable_id(
                                    "audit",
                                    "business_exception_invalidated:"
                                    f"{outcome['invalidation_record_id']}",
                                ),
                                "action": "business_exception_invalidated",
                                "subject": principal.subject,
                                "role": principal.role,
                                "scope": request["visibility_scope"],
                                "source_id": principal.source_id,
                                "application_id": request["application_id"],
                                "run_id": request["run_id"],
                                "finding_id": request["finding_id"],
                                "request_id": request["request_id"],
                                "exception_id": request["request_id"],
                                "work_item_id": request["work_item_id"],
                                "successor_work_item_id": outcome[
                                    "successor_work_item_id"
                                ],
                                "invalidated_claim_fence": outcome[
                                    "invalidated_claim_fence"
                                ],
                                "operations_revision": sequence,
                                "reason_code": reason_code,
                                "lifecycle_revision": outcome[
                                    "lifecycle_revision"
                                ],
                                "evidence_revision": outcome[
                                    "evidence_revision"
                                ],
                                "result": "accepted",
                                **self._audit_time_fields(
                                    staged,
                                    now=event_time,
                                ),
                            }
                        )
                    staged.audit_events.append(
                        {
                            "event_id": self._stable_id(
                                "audit", f"business_exception_operations:{record_id}"
                            ),
                            "action": f"business_exception_operations_{operations}",
                            "subject": principal.subject,
                            "role": principal.role,
                            "scope": principal.scope,
                            "source_id": principal.source_id,
                            "operations": operations,
                            "operations_revision": sequence,
                            "unresolved_request_count": (
                                0 if operations == "closed" else len(unresolved)
                            ),
                            "invalidated_request_ids": [
                                outcome["request"]["request_id"]
                                for outcome in close_outcomes
                            ],
                            "reason_code": reason_code,
                            "result": "accepted",
                            **self._audit_time_fields(staged, now=event_time),
                        }
                    )
                    result = {
                        "status": "accepted",
                        "replayed": False,
                        "operations": operations,
                        "revision": sequence,
                        "changed_at": event_time,
                        "unresolved_request_count": (
                            0 if operations == "closed" else len(unresolved)
                        ),
                        "invalidated_request_ids": [
                            outcome["request"]["request_id"]
                            for outcome in close_outcomes
                        ],
                        "reason_code": reason_code,
                        "unchanged": False,
                    }
                    self._before_write("exception_operations.idempotency")
                    staged.idempotency[binding_key] = (
                        command_fingerprint,
                        copy.deepcopy(result),
                    )
                    self._before_write("exception_operations.publish")
                    staged.persist()
                except _StoreWriteFailure as error:
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "reason_code": (
                            "AUDIT_UNAVAILABLE"
                            if str(error) == "exception_operations.audit"
                            else "STORAGE_UNAVAILABLE"
                        ),
                    }
                except StaleStoreRevision:
                    if attempt == 0:
                        continue
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "reason_code": "STORAGE_UNAVAILABLE",
                    }
                self._store = staged
                return result
            raise RuntimeError("business exception operations retry exhausted")

    def _exception_work_item_authority(
        self,
        *,
        principal: S01CommandPrincipal,
        work_item_id: str,
        now: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self._valid_exception_approver_principal(principal, now=now):
            raise QueryNotFound(work_item_id)
        work_items = [
            item
            for item in self._store.work_items
            if item.get("work_item_id") == work_item_id
            and item.get("kind") == "exception_approval"
            and item.get("owner") == "Lifecycle"
            and (
                item.get("visibility_scope") == principal.scope
                or principal.scope == "C-DEMO"
                and isinstance(item.get("visibility_scope"), str)
                and item["visibility_scope"].startswith(self._SESSION_SCOPE_PREFIX)
            )
            and item.get("assigned_subject") == principal.subject
        ]
        if len(work_items) != 1:
            raise QueryNotFound(work_item_id)
        work_item = work_items[0]
        state = {
            "status": "unclaimed",
            "claim_subject": None,
            "claim_fence": 0,
            "claim_expires_at": 0,
            "decision_id": None,
        }
        facts = sorted(
            (
                record
                for record in self._store.review_records
                if record.get("work_item_id") == work_item_id
                and record.get("record_type")
                in {
                    "exception_work_item_claimed",
                    "exception_work_item_completed",
                    "exception_work_item_invalidated",
                }
            ),
            key=lambda record: int(record["sequence"]),
        )
        if [fact.get("sequence") for fact in facts] != list(range(1, len(facts) + 1)):
            raise RuntimeError("exception work-item authority is not contiguous")
        for fact in facts:
            if fact["record_type"] == "exception_work_item_claimed":
                state.update(
                    {
                        "status": "claimed",
                        "claim_subject": fact["claim_subject"],
                        "claim_fence": fact["claim_fence"],
                        "claim_expires_at": fact["claim_expires_at"],
                    }
                )
            elif fact["record_type"] == "exception_work_item_completed":
                state.update(
                    {
                        "status": "completed",
                        "decision_id": fact["decision_id"],
                    }
                )
            else:
                state.update(
                    {
                        "status": "invalidated",
                        "claim_subject": None,
                        "claim_expires_at": fact["invalidated_at"],
                    }
                )
        return work_item, state

    def _exception_policy_rule(
        self, app: dict[str, Any], run: dict[str, Any], finding: dict[str, Any]
    ) -> Any | None:
        try:
            self._require_admitted_release(app)
        except _PinnedReleaseUnavailable:
            return None
        spec = run.get("spec")
        baseline = spec.get("baseline_release") if isinstance(spec, dict) else None
        try:
            release = self._pinned_release_for(spec)["target_release"]
        except Exception:
            return None
        if (
            not isinstance(spec, dict)
            or not isinstance(baseline, dict)
            or spec.get("release_id") != release.release_id
            or spec.get("release_digest") != release.release_digest
            or spec.get("checker_build") != release.checker_build
            or baseline.get("waiver_policy_id") != release.waiver_policy_id
            or baseline.get("waiver_policy_digest") != release.waiver_policy_digest
        ):
            return None
        rules = [rule for rule in release.rules if rule.rule_id == finding.get("rule_id")]
        return rules[0] if len(rules) == 1 else None

    def _exception_waiver_satisfied(
        self,
        *,
        app: dict[str, Any],
        run: dict[str, Any],
        rule: Any | None,
    ) -> bool:
        """True when the pinned release's rule admits a business exception
        for this finding.  Shared by the command and its read-only
        projection so the eligibility contract cannot drift from the
        commit-time check."""

        if (
            rule is None
            or rule.waivable is not True
            or rule.waiver_policy_id != self._EXCEPTION_POLICY_ID
            or rule.waiver_policy_digest
            != run["spec"]["baseline_release"].get("waiver_policy_digest")
            or rule.waiver_reasons != (self._EXCEPTION_REQUEST_REASON,)
            or rule.waiver_scope != self._EXCEPTION_SCOPE
            or rule.waiver_ttl_seconds != self._EXCEPTION_TTL_SECONDS
        ):
            return False
        return True

    def _exception_request_history(
        self, *, application_id: str, rule_id: str
    ) -> list[dict[str, Any]]:
        """The recorded business-exception requests for one rule of one
        application, oldest first.  Shared by the command and its read-only
        projection."""

        return [
            record
            for record in self._store.review_records
            if record.get("record_type") == "business_exception_request"
            and record.get("application_id") == application_id
            and record.get("rule_id") == rule_id
        ]

    def _exception_request_conflict_reason(
        self,
        *,
        work_item: dict[str, Any],
        finding_id: str,
        rule: Any | None,
        request_history: list[dict[str, Any]],
        active_request_ids: set[str],
        reason_code: str,
        validate_supplied_predecessor: bool = False,
        supplied_predecessor: str | None = None,
    ) -> str | None:
        """The shared request-history conflict chain of the command and its
        read-only projection: the stable reason code of the first blocking
        condition, or None when the request does not collide with history.

        The commit path additionally validates the client-supplied
        predecessor against the recorded chain in the exact order the
        command enforces; the projection never has a client value and skips
        those branches."""

        if any(
            record.get("cycle") == work_item["cycle"]
            and record.get("run_id") == work_item["run_id"]
            and record.get("finding_id") == finding_id
            and record.get("request_id") in active_request_ids
            for record in request_history
        ):
            return "ACTIVE_EXCEPTION_REQUEST_EXISTS"
        predecessor = request_history[-1] if request_history else None
        if predecessor is None:
            if validate_supplied_predecessor and supplied_predecessor is not None:
                return "EXCEPTION_PREDECESSOR_MISMATCH"
            return None
        if validate_supplied_predecessor:
            if supplied_predecessor is None:
                return "EXCEPTION_PREDECESSOR_REQUIRED"
            if supplied_predecessor != predecessor["request_id"]:
                return "EXCEPTION_PREDECESSOR_MISMATCH"
        if predecessor["request_id"] in active_request_ids:
            return "ACTIVE_EXCEPTION_REQUEST_EXISTS"
        if rule is not None and (
            predecessor["run_id"] == work_item["run_id"]
            and predecessor["waiver_policy_id"] == rule.waiver_policy_id
            and predecessor["waiver_policy_digest"] == rule.waiver_policy_digest
            and predecessor["reason_code"] == reason_code
        ):
            return "EXCEPTION_REREQUEST_NOT_MATERIAL"
        return None

    def _exception_write_gate_failure(
        self, *, app: dict[str, Any]
    ) -> tuple[str, str] | None:
        """The side-effect-free subset of ``_review_write_gate`` as the
        ``(status, failure_reason_code)`` pair it returns, for read-only
        projections: unlike the write gate it never stops a cohort.  Shared
        by the command and its eligibility projection so both observe the
        same failures."""

        if not self.audit_available:
            return "unavailable", "AUDIT_UNAVAILABLE"
        if not self.storage_available:
            return "unavailable", "STORAGE_UNAVAILABLE"
        cohort_stop = self._local_cohort_stop or self._store.cohort_stop
        if (
            cohort_stop is not None
            and cohort_stop.get("reason_code") == self._RUNTIME_STOP_REASON
        ):
            return (
                "stopped",
                str(
                    cohort_stop.get("failure_reason_code")
                    or self._RUNTIME_STOP_REASON
                ),
            )
        if not self._review_source_evidence_readable(app):
            return "stopped", self._REVIEW_SOURCE_FAILURE
        return None

    def _business_exception_current_context(
        self, request: dict[str, Any], *, now: float
    ) -> tuple[
        dict[str, Any],
        dict[str, Any],
        dict[str, Any],
        str,
        bool,
        dict[str, Any],
    ]:
        application_id = request["application_id"]
        app = self._store.applications.get(application_id)
        self._require_application_state_authority(app)
        assert app is not None
        runs = [
            run
            for run in self._store.runs
            if run.get("application_id") == application_id
            and run.get("run_id") == request["run_id"]
            and run.get("status") == "complete"
        ]
        if len(runs) != 1:
            raise RuntimeError("business exception run authority is unavailable")
        run = runs[0]
        findings = [
            finding
            for finding in self._store.findings
            if finding.get("application_id") == application_id
            and finding.get("run_id") == request["run_id"]
            and finding.get("finding_id") == request["finding_id"]
        ]
        if len(findings) != 1:
            raise RuntimeError("business exception finding authority is unavailable")
        finding = findings[0]
        decisions = [
            record
            for record in self._store.review_records
            if record.get("record_type") == "business_exception_decision"
            and record.get("request_id") == request["request_id"]
        ]
        invalidations = [
            record
            for record in self._store.review_records
            if record.get("record_type")
            in {"business_exception_expired", "business_exception_invalidated"}
            and record.get("request_id") == request["request_id"]
        ]
        if len(decisions) > 1:
            raise RuntimeError("business exception decision authority is not unique")
        status = "pending"
        if invalidations:
            status = str(invalidations[-1]["status"])
        elif decisions:
            status = str(decisions[0]["decision"])
        spec = run["spec"]
        release = self._pinned_release_for(spec)
        live_release = release["target_release"]
        exact_context = (
            app.get("cycle") == request["cycle"]
            and app.get("current_run_id") == request["run_id"]
            and app.get("evidence_revision") == request["evidence_revision"]
            and app.get("current_evidence_snapshot_id")
            == request["evidence_snapshot_id"]
            and app.get("current_evidence_snapshot_digest")
            == request["evidence_snapshot_digest"]
            and spec.get("release_id") == request["release_id"]
            and spec.get("release_digest") == request["release_digest"]
            and spec.get("checker_build") == request["checker_build"]
            and spec.get("baseline_release", {}).get("waiver_policy_id")
            == request["waiver_policy_id"]
            and spec.get("baseline_release", {}).get("waiver_policy_digest")
            == request["waiver_policy_digest"]
            and release["release_id"] == request["release_id"]
            and release["digest"] == request["release_digest"]
            and release["checker_build"] == request["checker_build"]
            and live_release.waiver_policy_id == request["waiver_policy_id"]
            and live_release.waiver_policy_digest
            == request["waiver_policy_digest"]
            and finding.get("verdict") == "inconsistent"
        )
        current = (
            exact_context
            and not invalidations
            and status != "rejected"
            and float(now) < float(request["expires_at"])
            and app.get("phase")
            in {
                "Pending Exception Approval",
                "Routing Determination",
                "Manual Review",
            }
        )
        fixed = {
            "application_id": application_id,
            "request_id": request["request_id"],
            "work_item_id": request["work_item_id"],
            "cycle": app["cycle"],
            "lifecycle_revision": app["lifecycle_revision"],
            "evidence_revision": app["evidence_revision"],
            "run_id": app["current_run_id"],
            "evidence_snapshot_id": app["current_evidence_snapshot_id"],
            "release_id": spec["release_id"],
            "release_digest": spec["release_digest"],
            "checker_build": spec["checker_build"],
            "waiver_policy_id": request["waiver_policy_id"],
            "waiver_policy_digest": request["waiver_policy_digest"],
            "phase": app["phase"],
            "route": app["route"],
            "projection_watermark": request["projection_watermark"],
            "status": status,
        }
        encoded = json.dumps(
            fixed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        command_context = {
            "cycle": app["cycle"],
            "lifecycle_revision": app["lifecycle_revision"],
            "evidence_revision": app["evidence_revision"],
            "run_id": app["current_run_id"],
            "projection_watermark": request["projection_watermark"],
            "current_context": hashlib.sha256(encoded).hexdigest(),
        }
        return app, run, finding, status, current, command_context

    def request_business_exception(
        self,
        *,
        principal: S01CommandPrincipal,
        work_item_id: str,
        finding_id: str,
        reason_code: str,
        expected_fence: int,
        expected_context: dict[str, Any],
        idempotency_key: str,
        predecessor_request_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        request_time = int(self._clock() if now is None else now)
        if not self._valid_reviewer_principal(
            principal, now=request_time
        ) or not self.is_c_demo_scope(principal.scope):
            raise QueryNotFound(work_item_id)
        if (
            not isinstance(finding_id, str)
            or not finding_id
            or len(finding_id) > 200
            or finding_id.strip() != finding_id
            or reason_code != self._EXCEPTION_REQUEST_REASON
            or isinstance(expected_fence, bool)
            or not isinstance(expected_fence, int)
            or expected_fence < 1
            or not self._valid_idempotency_key(idempotency_key)
            or predecessor_request_id is not None
            and (
                not isinstance(predecessor_request_id, str)
                or not predecessor_request_id
                or len(predecessor_request_id) > 200
                or predecessor_request_id.strip() != predecessor_request_id
            )
        ):
            raise ValueError("business exception request is invalid")
        fingerprint_bytes = json.dumps(
            {
                "expected_context": expected_context,
                "expected_fence": expected_fence,
                "finding_id": finding_id,
                "predecessor_request_id": predecessor_request_id,
                "reason_code": reason_code,
                "work_item_id": work_item_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        command_fingerprint = hashlib.sha256(fingerprint_bytes).hexdigest()
        binding_key = self._exception_idempotency_binding_key(
            principal,
            work_item_id,
            idempotency_key,
            action="request_business_exception",
        )
        with self._lock:
            self._reload_store()
            work_item, state = self._review_work_item_authority(
                principal=principal,
                work_item_id=work_item_id,
                now=request_time,
            )
            previous = self._store.idempotency.get(binding_key)
            if previous is not None:
                if previous[0] == command_fingerprint:
                    return {**copy.deepcopy(previous[1]), "replayed": True}
                return {
                    "status": "conflict",
                    "replayed": False,
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "reason_code": "IDEMPOTENCY_KEY_CONFLICT",
                }
            if self._business_exception_operations_state()["operations"] != "open":
                return {
                    "status": "stopped",
                    "replayed": False,
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "reason_code": self._EXCEPTION_OPERATIONS_CLOSED,
                }
            app, run, actual_context = self._review_current_context(work_item)
            if not self._review_context_matches(expected_context, actual_context):
                return {
                    "status": "stale",
                    "replayed": False,
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "reason_code": "STALE_REVIEW_CONTEXT",
                }
            if (
                state["status"] != "claimed"
                or state["claim_subject"] != principal.subject
                or state["claim_fence"] != expected_fence
                or float(state["claim_expires_at"]) <= request_time
                or app.get("phase") != "Manual Review"
                or app.get("current_run_id") != work_item["run_id"]
                or app.get("lifecycle_revision") != work_item["lifecycle_revision"]
                or app.get("evidence_revision") != work_item["evidence_revision"]
            ):
                return {
                    "status": "stale",
                    "replayed": False,
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "reason_code": "STALE_REVIEW_CONTEXT",
                }
            findings = [
                finding
                for finding in self._store.findings
                if finding.get("application_id") == work_item["application_id"]
                and finding.get("run_id") == work_item["run_id"]
                and finding.get("finding_id") == finding_id
            ]
            finding = findings[0] if len(findings) == 1 else None
            if (
                finding is None
                or finding_id not in work_item["finding_ids"]
                or finding.get("mandatory") is not True
                or finding.get("verdict") != "inconsistent"
            ):
                return {
                    "status": "rejected",
                    "replayed": False,
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "reason_code": "FINDING_NOT_EXCEPTION_ELIGIBLE",
                }
            rule = self._exception_policy_rule(app, run, finding)
            if finding.get("rule_id") in self._PROTECTED_EXCEPTION_CHECKS:
                return {
                    "status": "rejected",
                    "replayed": False,
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "reason_code": "PROTECTED_CHECK_NOT_WAIVABLE",
                }
            if not self._exception_waiver_satisfied(
                app=app, run=run, rule=rule
            ):
                return {
                    "status": "rejected",
                    "replayed": False,
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "reason_code": "CHECK_NOT_WAIVABLE_BY_PINNED_RELEASE",
                }
            request_history = self._exception_request_history(
                application_id=work_item["application_id"],
                rule_id=finding["rule_id"],
            )
            active_request_ids = set(self._unresolved_business_exception_ids())
            conflict = {
                "status": "conflict",
                "replayed": False,
                "application_id": work_item["application_id"],
                "work_item_id": work_item_id,
            }
            conflict_reason = self._exception_request_conflict_reason(
                work_item=work_item,
                finding_id=finding_id,
                rule=rule,
                request_history=request_history,
                active_request_ids=active_request_ids,
                reason_code=reason_code,
                validate_supplied_predecessor=True,
                supplied_predecessor=predecessor_request_id,
            )
            if conflict_reason is not None:
                return {**conflict, "reason_code": conflict_reason}
            gate = self._review_write_gate(app=app)
            if gate is not None:
                status, failure = gate
                return {
                    "status": status,
                    "replayed": False,
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "reason_code": failure,
                }
            request_id = self._stable_id(
                "exception_request", f"{binding_key}:{command_fingerprint}"
            )
            exception_work_item_id = self._stable_id(
                "work", f"exception_approval:{request_id}"
            )
            expires_at = request_time + rule.waiver_ttl_seconds
            staged = copy.deepcopy(self._store)
            staged_app = staged.applications[work_item["application_id"]]
            source_sequence = 1 + sum(
                record.get("work_item_id") == work_item_id
                and str(record.get("record_type", "")).startswith("work_item_")
                for record in staged.review_records
            )
            try:
                self._before_write("exception_request.lifecycle")
                staged_app["route"] = "pending_exception_approval"
                staged_app["projection_visible"] = False
                staged_app["projection_pending"] = False
                self._transition_lifecycle(
                    staged_app,
                    "Pending Exception Approval",
                    "BUSINESS_EXCEPTION_REQUESTED",
                    store=staged,
                )
                staged.lifecycle_events[-1].update(
                    {
                        "run_id": work_item["run_id"],
                        "request_id": request_id,
                        "finding_id": finding_id,
                        "predecessor_request_id": predecessor_request_id,
                    }
                )
                spec = run["spec"]
                request_record = {
                    "record_id": request_id,
                    "record_type": "business_exception_request",
                    "schema_version": "business-exception-request/1",
                    "request_id": request_id,
                    "exception_id": request_id,
                    "work_item_id": exception_work_item_id,
                    "source_work_item_id": work_item_id,
                    "application_id": work_item["application_id"],
                    "visibility_scope": work_item["visibility_scope"],
                    "cycle": work_item["cycle"],
                    "run_id": work_item["run_id"],
                    "finding_id": finding_id,
                    "rule_id": finding["rule_id"],
                    "verdict": "inconsistent",
                    "severity": finding["severity"],
                    "evidence_revision": work_item["evidence_revision"],
                    "evidence_snapshot_id": spec["evidence_snapshot_id"],
                    "evidence_snapshot_digest": spec["evidence_snapshot_digest"],
                    "release_id": spec["release_id"],
                    "release_digest": spec["release_digest"],
                    "checker_build": spec["checker_build"],
                    "waiver_policy_id": rule.waiver_policy_id,
                    "waiver_policy_digest": rule.waiver_policy_digest,
                    "requester_subject": principal.subject,
                    "requester_role": principal.role,
                    "requester_source_id": principal.source_id,
                    "assigned_approver_subject": self._exception_approver_subject,
                    "predecessor_request_id": predecessor_request_id,
                    "reason_code": reason_code,
                    "scope": rule.waiver_scope,
                    "requester_claim_fence": expected_fence,
                    "pre_request_lifecycle_revision": work_item[
                        "lifecycle_revision"
                    ],
                    "post_request_lifecycle_revision": staged_app[
                        "lifecycle_revision"
                    ],
                    "projection_watermark": actual_context[
                        "projection_watermark"
                    ],
                    "fixed_context": copy.deepcopy(actual_context),
                    "requested_at": request_time,
                    "expires_at": expires_at,
                    "idempotency_fingerprint": command_fingerprint,
                }
                fixed_bytes = json.dumps(
                    request_record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                request_record["context_digest"] = hashlib.sha256(
                    fixed_bytes
                ).hexdigest()
                self._before_write("exception_request.request")
                staged.review_records.append(request_record)
                self._before_write("exception_request.review_responsibility")
                staged.review_records.append(
                    {
                        "record_id": self._stable_id(
                            "review_record",
                            f"{work_item_id}:exception_requested:{source_sequence}",
                        ),
                        "record_type": "work_item_finding_exception_requested",
                        "sequence": source_sequence,
                        "work_item_id": work_item_id,
                        "application_id": work_item["application_id"],
                        "run_id": work_item["run_id"],
                        "finding_id": finding_id,
                        "request_id": request_id,
                        "exception_id": request_id,
                        "claim_subject": principal.subject,
                        "claim_fence": expected_fence,
                        "requested_at": request_time,
                        "recorded_at": request_time,
                    }
                )
                self._before_write("exception_request.work_item")
                staged.work_items.append(
                    {
                        "work_item_id": exception_work_item_id,
                        "owner": "Lifecycle",
                        "kind": "exception_approval",
                        "status": "active",
                        "request_id": request_id,
                        "application_id": work_item["application_id"],
                        "cycle": work_item["cycle"],
                        "run_id": work_item["run_id"],
                        "finding_ids": [finding_id],
                        "lifecycle_revision": staged_app["lifecycle_revision"],
                        "evidence_revision": work_item["evidence_revision"],
                        "evidence_snapshot_id": spec["evidence_snapshot_id"],
                        "release_id": spec["release_id"],
                        "waiver_policy_id": rule.waiver_policy_id,
                        "visibility_scope": work_item["visibility_scope"],
                        "assigned_subject": self._exception_approver_subject,
                    }
                )
                self._before_write("exception_request.audit")
                staged.audit_events.append(
                    {
                        "event_id": self._stable_id(
                            "audit", f"business_exception_requested:{request_id}"
                        ),
                        "action": "business_exception_requested",
                        "subject": principal.subject,
                        "role": principal.role,
                        "scope": work_item["visibility_scope"],
                        "source_id": principal.source_id,
                        "application_id": work_item["application_id"],
                        "run_id": work_item["run_id"],
                        "finding_id": finding_id,
                        "work_item_id": exception_work_item_id,
                        "request_id": request_id,
                        "exception_id": request_id,
                        "predecessor_request_id": predecessor_request_id,
                        "reason_code": reason_code,
                        "waiver_policy_id": rule.waiver_policy_id,
                        "waiver_policy_digest": rule.waiver_policy_digest,
                        "claim_fence": expected_fence,
                        "expires_at": expires_at,
                        "lifecycle_revision": staged_app["lifecycle_revision"],
                        "evidence_revision": staged_app["evidence_revision"],
                        "result": "accepted",
                        **self._audit_time_fields(staged, now=request_time),
                    }
                )
                result = {
                    "status": "accepted",
                    "replayed": False,
                    "application_id": work_item["application_id"],
                    "request_id": request_id,
                    "work_item_id": exception_work_item_id,
                    "finding_id": finding_id,
                    "phase": "Pending Exception Approval",
                    "route": "pending_exception_approval",
                    "expires_at": expires_at,
                    "lifecycle_revision": staged_app["lifecycle_revision"],
                    "evidence_revision": staged_app["evidence_revision"],
                }
                self._before_write("exception_request.idempotency")
                staged.idempotency[binding_key] = (
                    command_fingerprint,
                    copy.deepcopy(result),
                )
                self._before_write("exception_request.publish")
                staged.persist()
            except StaleStoreRevision:
                self._reload_store()
                previous = self._store.idempotency.get(binding_key)
                if previous is not None and previous[0] == command_fingerprint:
                    return {**copy.deepcopy(previous[1]), "replayed": True}
                duplicate_winner = any(
                    record.get("record_type") == "business_exception_request"
                    and record.get("application_id") == work_item["application_id"]
                    and record.get("cycle") == work_item["cycle"]
                    and record.get("run_id") == work_item["run_id"]
                    and record.get("finding_id") == finding_id
                    and record.get("request_id")
                    in set(self._unresolved_business_exception_ids())
                    for record in self._store.review_records
                )
                return {
                    "status": "conflict" if duplicate_winner else "unavailable",
                    "replayed": False,
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "reason_code": (
                        "ACTIVE_EXCEPTION_REQUEST_EXISTS"
                        if duplicate_winner
                        else "STORAGE_UNAVAILABLE"
                    ),
                }
            except _StoreWriteFailure as error:
                return {
                    "status": "unavailable",
                    "replayed": False,
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "reason_code": (
                        "AUDIT_UNAVAILABLE"
                        if str(error) == "exception_request.audit"
                        else "STORAGE_UNAVAILABLE"
                    ),
                }
            self._store = staged
            return result

    def _business_exception_eligibility(
        self,
        work_item: dict[str, Any],
        review_state: dict[str, Any],
        finding: dict[str, Any],
        *,
        now: float,
        subject: str,
    ) -> dict[str, Any]:
        """The server-owned closed eligibility projection for one finding.

        Mirrors every side-effect-free precondition of
        ``request_business_exception`` over the same policy/context helpers
        (no rule id is hard-coded here): a request posted right now is
        accepted only when ``eligible`` is true, and the projection never
        writes.  The command re-verifies at commit the conditions the
        read-only projection cannot see — the claim fence, the app phase and
        run/revision binding, and the full write gate — so a projection miss
        can only fail a later commit, never admit one.  Every authority
        failure fails closed with the stable reason code."""

        closed: dict[str, Any] = {
            "eligible": False,
            "request_reason": None,
            "ineligible_reason_code": None,
            "predecessor_request_id": None,
        }
        try:
            if (
                self._business_exception_operations_state()["operations"]
                != "open"
            ):
                return {
                    **closed,
                    "ineligible_reason_code": self._EXCEPTION_OPERATIONS_CLOSED,
                }
            if (
                review_state["status"] != "claimed"
                or review_state["claim_subject"] != subject
                or float(review_state["claim_expires_at"]) <= float(now)
            ):
                return {
                    **closed,
                    "ineligible_reason_code": "STALE_REVIEW_CONTEXT",
                }
            if (
                finding.get("mandatory") is not True
                or finding.get("verdict") != "inconsistent"
            ):
                return {
                    **closed,
                    "ineligible_reason_code": "FINDING_NOT_EXCEPTION_ELIGIBLE",
                }
            if finding.get("rule_id") in self._PROTECTED_EXCEPTION_CHECKS:
                return {
                    **closed,
                    "ineligible_reason_code": "PROTECTED_CHECK_NOT_WAIVABLE",
                }
            app, run, _ = self._review_current_context(work_item)
            rule = self._exception_policy_rule(app, run, finding)
            if not self._exception_waiver_satisfied(
                app=app, run=run, rule=rule
            ):
                return {
                    **closed,
                    "ineligible_reason_code": (
                        "CHECK_NOT_WAIVABLE_BY_PINNED_RELEASE"
                    ),
                }
            request_history = self._exception_request_history(
                application_id=work_item["application_id"],
                rule_id=finding["rule_id"],
            )
            active_request_ids = set(self._unresolved_business_exception_ids())
            predecessor = request_history[-1] if request_history else None
            conflict_reason = self._exception_request_conflict_reason(
                work_item=work_item,
                finding_id=finding["finding_id"],
                rule=rule,
                request_history=request_history,
                active_request_ids=active_request_ids,
                reason_code=self._EXCEPTION_REQUEST_REASON,
            )
            if conflict_reason is not None:
                return {
                    **closed,
                    "ineligible_reason_code": conflict_reason,
                }
            write_failure = self._exception_write_gate_failure(app=app)
            if write_failure is not None:
                return {
                    **closed,
                    "ineligible_reason_code": write_failure[1],
                }
        except (KeyError, RuntimeError):
            return {
                **closed,
                "ineligible_reason_code": self._APPLICATION_STATE_FAILURE,
            }
        return {
            "eligible": True,
            "request_reason": self._EXCEPTION_REQUEST_REASON,
            "ineligible_reason_code": None,
            "predecessor_request_id": (
                str(predecessor["request_id"])
                if predecessor is not None
                else None
            ),
        }

    def business_exception_view(
        self,
        *,
        principal: S01CommandPrincipal,
        request_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        query_time = float(self._clock() if now is None else now)
        with self._lock:
            self._reload_store()
            request = self._business_exception_request_authority(request_id)
            work_item, work_state = self._exception_work_item_authority(
                principal=principal,
                work_item_id=request["work_item_id"],
                now=query_time,
            )
            if work_item.get("request_id") != request_id:
                raise RuntimeError("exception request work authority does not match")
            app, _, finding, status, current, command_context = (
                self._business_exception_current_context(request, now=query_time)
            )
            claim_status = work_state["status"]
            if (
                claim_status == "claimed"
                and float(work_state["claim_expires_at"]) <= query_time
            ):
                claim_status = "expired"
            operations_open = (
                self._business_exception_operations_state()["operations"] == "open"
            )
            actions: list[str] = []
            if status == "pending" and current and operations_open:
                if claim_status in {"unclaimed", "expired"}:
                    actions.append("claim")
                elif work_state["claim_subject"] == principal.subject:
                    actions.append("decide")
            evidence_references = [
                {
                    key: copy.deepcopy(link[key])
                    for key in (
                        "observation_id",
                        "document_role",
                        "field",
                        "source_page",
                        "source_region",
                    )
                    if link.get(key) is not None
                }
                for link in finding["evidence_links"]
            ]
            application_hash = hashlib.sha256(
                str(app["application_id"]).encode("utf-8")
            ).hexdigest()[:12]
            return {
                "schema_version": "business-exception-approver-view/1",
                "request_id": request_id,
                "work_item_id": request["work_item_id"],
                "status": status,
                "current": current,
                "currentness_reason": (
                    "CURRENT_FIXED_CONTEXT"
                    if current
                    else "PROCESSING_CYCLE_SEALED"
                    if app.get("phase") == "Verification Completed"
                    else "EXPIRED"
                    if status == "expired"
                    or query_time >= float(request["expires_at"])
                    else "INVALIDATED"
                    if status == "invalidated"
                    else "REJECTED"
                    if status == "rejected"
                    else "CONTEXT_NOT_CURRENT"
                ),
                "application_reference": f"application:{application_hash}",
                "finding": {
                    key: finding[key]
                    for key in (
                        "finding_id",
                        "rule_id",
                        "verdict",
                        "severity",
                        "reason_code",
                    )
                },
                "evidence_references": evidence_references,
                "requester": {
                    "subject": request["requester_subject"],
                    "role": request["requester_role"],
                    "source_id": request["requester_source_id"],
                },
                "request_reason": request["reason_code"],
                "scope": request["scope"],
                "requested_at": request["requested_at"],
                "expires_at": request["expires_at"],
                "run_id": request["run_id"],
                "evidence_snapshot_id": request["evidence_snapshot_id"],
                "evidence_snapshot_digest": request["evidence_snapshot_digest"],
                "release_id": request["release_id"],
                "release_digest": request["release_digest"],
                "checker_build": request["checker_build"],
                "waiver_policy_id": request["waiver_policy_id"],
                "waiver_policy_digest": request["waiver_policy_digest"],
                "claim_status": claim_status,
                "claim_subject": work_state["claim_subject"],
                "claim_fence": work_state["claim_fence"],
                "claim_expires_at": work_state["claim_expires_at"],
                "command_context": command_context,
                "projection_watermark": request["projection_watermark"],
                "actions": actions,
            }

    def claim_exception_work_item(
        self,
        *,
        principal: S01CommandPrincipal,
        work_item_id: str,
        expected_context: dict[str, Any],
        now: float | None = None,
    ) -> dict[str, Any]:
        claim_time = int(self._clock() if now is None else now)
        with self._lock:
            for attempt in range(2):
                self._reload_store()
                work_item, state = self._exception_work_item_authority(
                    principal=principal,
                    work_item_id=work_item_id,
                    now=claim_time,
                )
                request = self._business_exception_request_authority(
                    str(work_item["request_id"])
                )
                if (
                    self._business_exception_operations_state()["operations"]
                    != "open"
                ):
                    return {
                        "status": "stopped",
                        "request_id": request["request_id"],
                        "work_item_id": work_item_id,
                        "reason_code": self._EXCEPTION_OPERATIONS_CLOSED,
                    }
                app, _, _, status, current, actual_context = (
                    self._business_exception_current_context(request, now=claim_time)
                )
                if expected_context != actual_context:
                    return {
                        "status": "stale",
                        "request_id": request["request_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "STALE_EXCEPTION_CONTEXT",
                    }
                if status != "pending" or not current:
                    return {
                        "status": "stale",
                        "request_id": request["request_id"],
                        "work_item_id": work_item_id,
                        "reason_code": (
                            "BUSINESS_EXCEPTION_EXPIRED"
                            if claim_time >= request["expires_at"]
                            else "STALE_EXCEPTION_CONTEXT"
                        ),
                    }
                if (
                    state["status"] == "claimed"
                    and float(state["claim_expires_at"]) > claim_time
                ):
                    return {
                        "status": "conflict",
                        "request_id": request["request_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "EXCEPTION_WORK_ITEM_ALREADY_CLAIMED",
                    }
                gate = self._review_write_gate(app=app)
                if gate is not None:
                    failure_status, reason_code = gate
                    return {
                        "status": failure_status,
                        "request_id": request["request_id"],
                        "work_item_id": work_item_id,
                        "reason_code": reason_code,
                    }
                staged = copy.deepcopy(self._store)
                sequence = 1 + sum(
                    record.get("work_item_id") == work_item_id
                    and str(record.get("record_type", "")).startswith(
                        "exception_work_item_"
                    )
                    for record in staged.review_records
                )
                claim_fence = int(state["claim_fence"]) + 1
                claim_expires_at = claim_time + self._EXCEPTION_CLAIM_TTL_SECONDS
                staged.review_records.append(
                    {
                        "record_id": self._stable_id(
                            "review_record",
                            f"{work_item_id}:exception_claim:{sequence}",
                        ),
                        "record_type": "exception_work_item_claimed",
                        "sequence": sequence,
                        "work_item_id": work_item_id,
                        "request_id": request["request_id"],
                        "exception_id": request["request_id"],
                        "application_id": request["application_id"],
                        "run_id": request["run_id"],
                        "claim_subject": principal.subject,
                        "claim_fence": claim_fence,
                        "claim_started_at": claim_time,
                        "claim_expires_at": claim_expires_at,
                        "recorded_at": claim_time,
                    }
                )
                try:
                    self._before_write("exception_claim.audit")
                    staged.audit_events.append(
                        {
                            "event_id": self._stable_id(
                                "audit",
                                f"exception_work_item_claimed:{work_item_id}:{sequence}",
                            ),
                            "action": "exception_work_item_claimed",
                            "subject": principal.subject,
                            "role": principal.role,
                            "scope": work_item["visibility_scope"],
                            "source_id": principal.source_id,
                            "application_id": request["application_id"],
                            "run_id": request["run_id"],
                            "request_id": request["request_id"],
                            "exception_id": request["request_id"],
                            "work_item_id": work_item_id,
                            "claim_fence": claim_fence,
                            "result": "accepted",
                            **self._audit_time_fields(staged, now=claim_time),
                        }
                    )
                    staged.persist()
                except _StoreWriteFailure:
                    return {
                        "status": "unavailable",
                        "request_id": request["request_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "AUDIT_UNAVAILABLE",
                    }
                except StaleStoreRevision:
                    if attempt == 0:
                        continue
                    return {
                        "status": "unavailable",
                        "request_id": request["request_id"],
                        "work_item_id": work_item_id,
                        "reason_code": "STORAGE_UNAVAILABLE",
                    }
                self._store = staged
                return {
                    "status": "claimed",
                    "request_id": request["request_id"],
                    "work_item_id": work_item_id,
                    "claim_subject": principal.subject,
                    "claim_fence": claim_fence,
                    "claim_expires_at": claim_expires_at,
                }
            raise RuntimeError("exception work-item claim retry exhausted")

    @staticmethod
    def _business_exception_routing_context(
        request: dict[str, Any], decision: dict[str, Any], app: dict[str, Any]
    ) -> dict[str, Any]:
        fixed = {
            "application_id": request["application_id"],
            "request_id": request["request_id"],
            "decision_id": decision["decision_id"],
            "cycle": app["cycle"],
            "lifecycle_revision": app["lifecycle_revision"],
            "evidence_revision": app["evidence_revision"],
            "run_id": app["current_run_id"],
            "evidence_snapshot_id": app["current_evidence_snapshot_id"],
            "release_id": request["release_id"],
            "release_digest": request["release_digest"],
            "checker_build": request["checker_build"],
            "waiver_policy_id": request["waiver_policy_id"],
            "waiver_policy_digest": request["waiver_policy_digest"],
            "expires_at": request["expires_at"],
            "phase": app["phase"],
            "route": app["route"],
        }
        encoded = json.dumps(
            fixed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return {
            "cycle": app["cycle"],
            "lifecycle_revision": app["lifecycle_revision"],
            "evidence_revision": app["evidence_revision"],
            "run_id": app["current_run_id"],
            "request_id": request["request_id"],
            "decision_id": decision["decision_id"],
            "current_context": hashlib.sha256(encoded).hexdigest(),
        }

    def _create_manual_review_successor(
        self,
        store: _TargetStore,
        app: dict[str, Any],
        run: dict[str, Any],
        *,
        finding_ids: list[str],
        predecessor_request_id: str,
    ) -> str:
        work_item_id = self._stable_id(
            "work",
            ":".join(
                (
                    app["application_id"],
                    str(app["cycle"]),
                    str(run["run_id"]),
                    str(app["lifecycle_revision"]),
                    predecessor_request_id,
                )
            ),
        )
        spec = run["spec"]
        visibility_scope = self._application_visibility_scope(app["application_id"])
        store.work_items.append(
            {
                "work_item_id": work_item_id,
                "owner": "Lifecycle",
                "kind": "manual_review",
                "status": "active",
                "application_id": app["application_id"],
                "cycle": app["cycle"],
                "run_id": run["run_id"],
                "lifecycle_revision": app["lifecycle_revision"],
                "evidence_revision": app["evidence_revision"],
                "evidence_snapshot_id": spec["evidence_snapshot_id"],
                "release_id": spec["release_id"],
                "finding_ids": copy.deepcopy(finding_ids),
                "visibility_scope": visibility_scope,
                "assigned_subject": self._application_review_assignee(
                    app["application_id"]
                ),
                "claim_subject": None,
                "claim_fence": 0,
                "claim_started_at": 0,
                "claim_expires_at": 0,
                "predecessor_request_id": predecessor_request_id,
            }
        )
        app["projection_pending"] = True
        app["projection_visible"] = False
        store.outbox.append(
            {
                "event_id": self._stable_id(
                    "outbox",
                    f"projection:{run['run_id']}:{app['lifecycle_revision']}",
                ),
                "kind": "review_projection_requested",
                "application_id": app["application_id"],
                "run_id": run["run_id"],
                "lifecycle_revision": app["lifecycle_revision"],
                "visibility_scope": visibility_scope,
                "projection_watermark": 1
                + sum(
                    event.get("kind") == "review_projection_requested"
                    and event.get("visibility_scope", "C-DEMO") == visibility_scope
                    for event in store.outbox
                ),
                "status": "pending",
            }
        )
        return work_item_id

    def decide_business_exception(
        self,
        *,
        principal: S01CommandPrincipal,
        request_id: str,
        work_item_id: str,
        decision: str,
        reason_code: str,
        expected_fence: int,
        expected_context: dict[str, Any],
        idempotency_key: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        decision_time = int(self._clock() if now is None else now)
        expected_reason = {
            "approved": "DOCUMENTED_VARIANCE_ACCEPTED",
            "rejected": "DOCUMENTED_VARIANCE_REJECTED",
        }.get(decision)
        if (
            expected_reason is None
            or reason_code != expected_reason
            or isinstance(expected_fence, bool)
            or not isinstance(expected_fence, int)
            or expected_fence < 1
            or not self._valid_idempotency_key(idempotency_key)
        ):
            raise ValueError("business exception decision is invalid")
        if not self._valid_exception_approver_principal(
            principal, now=decision_time
        ):
            raise QueryNotFound(request_id)
        fingerprint_bytes = json.dumps(
            {
                "decision": decision,
                "expected_context": expected_context,
                "expected_fence": expected_fence,
                "reason_code": reason_code,
                "request_id": request_id,
                "work_item_id": work_item_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        command_fingerprint = hashlib.sha256(fingerprint_bytes).hexdigest()
        binding_key = self._exception_idempotency_binding_key(
            principal,
            request_id,
            idempotency_key,
            action="decide_business_exception",
        )
        with self._lock:
            for attempt in range(2):
                self._reload_store()
                request = self._business_exception_request_authority(request_id)
                work_item, work_state = self._exception_work_item_authority(
                    principal=principal,
                    work_item_id=work_item_id,
                    now=decision_time,
                )
                if work_item.get("request_id") != request_id:
                    raise QueryNotFound(request_id)
                previous = self._store.idempotency.get(binding_key)
                if previous is not None:
                    if previous[0] == command_fingerprint:
                        return {**copy.deepcopy(previous[1]), "replayed": True}
                    return {
                        "status": "conflict",
                        "replayed": False,
                        "request_id": request_id,
                        "work_item_id": work_item_id,
                        "reason_code": "IDEMPOTENCY_KEY_CONFLICT",
                    }
                if (
                    self._business_exception_operations_state()["operations"]
                    != "open"
                ):
                    return {
                        "status": "stopped",
                        "replayed": False,
                        "request_id": request_id,
                        "work_item_id": work_item_id,
                        "reason_code": self._EXCEPTION_OPERATIONS_CLOSED,
                    }
                winners = [
                    record
                    for record in self._store.review_records
                    if record.get("record_type") == "business_exception_decision"
                    and record.get("request_id") == request_id
                ]
                if winners:
                    return {
                        "status": "already_decided",
                        "replayed": False,
                        "request_id": request_id,
                        "work_item_id": work_item_id,
                        "decision_id": winners[0]["decision_id"],
                        "decision": winners[0]["decision"],
                        "reason_code": "BUSINESS_EXCEPTION_ALREADY_DECIDED",
                    }
                app, run, finding, status, current, actual_context = (
                    self._business_exception_current_context(
                        request, now=decision_time
                    )
                )
                if expected_context != actual_context:
                    return {
                        "status": "stale",
                        "replayed": False,
                        "request_id": request_id,
                        "work_item_id": work_item_id,
                        "reason_code": "STALE_EXCEPTION_CONTEXT",
                    }
                if (
                    status != "pending"
                    or not current
                    or app.get("phase") != "Pending Exception Approval"
                    or app.get("route") != "pending_exception_approval"
                ):
                    return {
                        "status": "stale",
                        "replayed": False,
                        "request_id": request_id,
                        "work_item_id": work_item_id,
                        "reason_code": (
                            "BUSINESS_EXCEPTION_EXPIRED"
                            if decision_time >= request["expires_at"]
                            else "STALE_EXCEPTION_CONTEXT"
                        ),
                    }
                if (
                    work_state["status"] != "claimed"
                    or work_state["claim_subject"] != principal.subject
                    or work_state["claim_fence"] != expected_fence
                    or float(work_state["claim_expires_at"]) <= decision_time
                ):
                    return {
                        "status": "stale",
                        "replayed": False,
                        "request_id": request_id,
                        "work_item_id": work_item_id,
                        "reason_code": "STALE_EXCEPTION_WORK_ITEM_CLAIM",
                    }
                if principal.subject == request["requester_subject"]:
                    return {
                        "status": "rejected",
                        "replayed": False,
                        "request_id": request_id,
                        "work_item_id": work_item_id,
                        "reason_code": "SEPARATION_OF_DUTIES_REQUIRED",
                    }
                rule = self._exception_policy_rule(app, run, finding)
                if (
                    finding.get("rule_id") in self._PROTECTED_EXCEPTION_CHECKS
                    or rule is None
                    or rule.waivable is not True
                    or rule.waiver_policy_digest != request["waiver_policy_digest"]
                ):
                    return {
                        "status": "stale",
                        "replayed": False,
                        "request_id": request_id,
                        "work_item_id": work_item_id,
                        "reason_code": "WAIVER_POLICY_NOT_CURRENT",
                    }
                gate = self._review_write_gate(app=app)
                if gate is not None:
                    failure_status, failure_reason = gate
                    return {
                        "status": failure_status,
                        "replayed": False,
                        "request_id": request_id,
                        "work_item_id": work_item_id,
                        "reason_code": failure_reason,
                    }
                decision_id = self._stable_id(
                    "exception_decision", f"{binding_key}:{command_fingerprint}"
                )
                staged = copy.deepcopy(self._store)
                staged_app = staged.applications[request["application_id"]]
                staged_run = next(
                    item
                    for item in staged.runs
                    if item.get("run_record_id") == run.get("run_record_id")
                )
                try:
                    self._before_write("exception_decision.lifecycle")
                    if decision == "approved":
                        staged_app["route"] = "routing_determination"
                        self._transition_lifecycle(
                            staged_app,
                            "Routing Determination",
                            "BUSINESS_EXCEPTION_APPROVED",
                            store=staged,
                        )
                    else:
                        staged_app["route"] = "manual_review"
                        self._transition_lifecycle(
                            staged_app,
                            "Manual Review",
                            "BUSINESS_EXCEPTION_REJECTED",
                            store=staged,
                        )
                    staged.lifecycle_events[-1].update(
                        {
                            "run_id": request["run_id"],
                            "request_id": request_id,
                            "decision_id": decision_id,
                            "exception_id": request_id,
                            "expires_at": request["expires_at"],
                        }
                    )
                    decision_record = {
                        "record_id": decision_id,
                        "record_type": "business_exception_decision",
                        "schema_version": "business-exception-decision/1",
                        "decision_id": decision_id,
                        "request_id": request_id,
                        "exception_id": request_id,
                        "work_item_id": work_item_id,
                        "application_id": request["application_id"],
                        "cycle": request["cycle"],
                        "run_id": request["run_id"],
                        "finding_id": request["finding_id"],
                        "decision": decision,
                        "reason_code": reason_code,
                        "approver_subject": principal.subject,
                        "approver_role": principal.role,
                        "approver_source_id": principal.source_id,
                        "requester_subject": request["requester_subject"],
                        "claim_fence": expected_fence,
                        "fixed_context": copy.deepcopy(actual_context),
                        "lifecycle_revision": staged_app["lifecycle_revision"],
                        "evidence_revision": staged_app["evidence_revision"],
                        "decided_at": decision_time,
                        "expires_at": request["expires_at"],
                    }
                    self._before_write("exception_decision.decision")
                    staged.review_records.append(decision_record)
                    sequence = 1 + sum(
                        record.get("work_item_id") == work_item_id
                        and str(record.get("record_type", "")).startswith(
                            "exception_work_item_"
                        )
                        for record in staged.review_records
                    )
                    self._before_write("exception_decision.work_item")
                    staged.review_records.append(
                        {
                            "record_id": self._stable_id(
                                "review_record",
                                f"{work_item_id}:exception_complete:{sequence}",
                            ),
                            "record_type": "exception_work_item_completed",
                            "sequence": sequence,
                            "work_item_id": work_item_id,
                            "request_id": request_id,
                            "exception_id": request_id,
                            "application_id": request["application_id"],
                            "run_id": request["run_id"],
                            "claim_subject": principal.subject,
                            "claim_fence": expected_fence,
                            "decision_id": decision_id,
                            "completed_at": decision_time,
                            "recorded_at": decision_time,
                        }
                    )
                    successor_work_item_id = None
                    if decision == "rejected":
                        blocker_ids = [
                            item["finding_id"]
                            for item in staged.findings
                            if item.get("application_id") == request["application_id"]
                            and item.get("run_id") == request["run_id"]
                            and item.get("mandatory") is True
                            and item.get("verdict") != "consistent"
                        ]
                        self._before_write("exception_decision.review_successor")
                        successor_work_item_id = self._create_manual_review_successor(
                            staged,
                            staged_app,
                            staged_run,
                            finding_ids=blocker_ids,
                            predecessor_request_id=request_id,
                        )
                    self._before_write("exception_decision.audit")
                    staged.audit_events.append(
                        {
                            "event_id": self._stable_id(
                                "audit", f"business_exception_decided:{decision_id}"
                            ),
                            "action": "business_exception_decided",
                            "subject": principal.subject,
                            "role": principal.role,
                            "scope": work_item["visibility_scope"],
                            "source_id": principal.source_id,
                            "application_id": request["application_id"],
                            "run_id": request["run_id"],
                            "finding_id": request["finding_id"],
                            "request_id": request_id,
                            "exception_id": request_id,
                            "decision_id": decision_id,
                            "work_item_id": work_item_id,
                            "successor_work_item_id": successor_work_item_id,
                            "outcome": decision,
                            "reason_code": reason_code,
                            "claim_fence": expected_fence,
                            "expires_at": request["expires_at"],
                            "lifecycle_revision": staged_app[
                                "lifecycle_revision"
                            ],
                            "evidence_revision": staged_app["evidence_revision"],
                            "result": "accepted",
                            **self._audit_time_fields(staged, now=decision_time),
                        }
                    )
                    result = {
                        "status": "accepted",
                        "replayed": False,
                        "request_id": request_id,
                        "work_item_id": work_item_id,
                        "decision_id": decision_id,
                        "decision": decision,
                        "phase": staged_app["phase"],
                        "route": staged_app["route"],
                        "successor_work_item_id": successor_work_item_id,
                        "lifecycle_revision": staged_app[
                            "lifecycle_revision"
                        ],
                        "evidence_revision": staged_app["evidence_revision"],
                    }
                    if decision == "approved":
                        routing_context = self._business_exception_routing_context(
                            request, decision_record, staged_app
                        )
                        result["routing_context"] = routing_context
                        self._before_write("exception_decision.routing_job")
                        staged.jobs.append(
                            {
                                "job_id": self._stable_id(
                                    "job", f"exception_route:{request_id}:{decision_id}"
                                ),
                                "application_id": request["application_id"],
                                "kind": "business_exception_route",
                                "status": "queued",
                                "fingerprint": routing_context["current_context"],
                                "logical_operation_id": self._stable_id(
                                    "operation", f"exception_route:{request_id}"
                                ),
                                "request_id": request_id,
                                "decision_id": decision_id,
                                "routing_context": copy.deepcopy(routing_context),
                                "fence": 0,
                                "attempt_no": 0,
                            }
                        )
                    self._before_write("exception_decision.idempotency")
                    staged.idempotency[binding_key] = (
                        command_fingerprint,
                        copy.deepcopy(result),
                    )
                    self._before_write("exception_decision.publish")
                    staged.persist()
                except _StoreWriteFailure as error:
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "request_id": request_id,
                        "work_item_id": work_item_id,
                        "reason_code": (
                            "AUDIT_UNAVAILABLE"
                            if str(error) == "exception_decision.audit"
                            else "STORAGE_UNAVAILABLE"
                        ),
                    }
                except StaleStoreRevision:
                    if attempt == 0:
                        continue
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "request_id": request_id,
                        "work_item_id": work_item_id,
                        "reason_code": "STORAGE_UNAVAILABLE",
                    }
                self._store = staged
                return result
            raise RuntimeError("business exception decision retry exhausted")

    def expire_business_exception(
        self,
        *,
        principal: S01CommandPrincipal,
        request_id: str,
        expected_context: dict[str, Any],
        idempotency_key: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        event_time = int(self._clock() if now is None else now)
        return self._deactivate_business_exception(
            principal=principal,
            request_id=request_id,
            reason_code="BUSINESS_EXCEPTION_EXPIRED",
            expected_context=expected_context,
            idempotency_key=idempotency_key,
            event_time=event_time,
            require_expiry=True,
        )

    def invalidate_business_exception(
        self,
        *,
        principal: S01CommandPrincipal,
        request_id: str,
        reason_code: str,
        expected_context: dict[str, Any],
        idempotency_key: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        event_time = int(self._clock() if now is None else now)
        if reason_code not in self._EXCEPTION_INVALIDATION_REASONS:
            raise ValueError("business exception invalidation reason is invalid")
        return self._deactivate_business_exception(
            principal=principal,
            request_id=request_id,
            reason_code=reason_code,
            expected_context=expected_context,
            idempotency_key=idempotency_key,
            event_time=event_time,
            require_expiry=False,
        )

    def _deactivate_business_exception(
        self,
        *,
        principal: S01CommandPrincipal,
        request_id: str,
        reason_code: str,
        expected_context: dict[str, Any],
        idempotency_key: str,
        event_time: int,
        require_expiry: bool,
    ) -> dict[str, Any]:
        if not self._valid_exception_router_principal(principal, now=event_time):
            raise QueryNotFound(request_id)
        if not self._valid_idempotency_key(idempotency_key):
            raise ValueError("business exception deactivation key is invalid")
        mode = "expiry" if require_expiry else "invalidation"
        prefix = f"exception_{mode}"
        status_value = "expired" if require_expiry else "invalidated"
        record_type = f"business_exception_{status_value}"
        action = f"business_exception_{status_value}"
        schema = f"business-exception-{mode}/1"
        fingerprint_bytes = json.dumps(
            {
                "expected_context": expected_context,
                "reason_code": reason_code,
                "request_id": request_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        command_fingerprint = hashlib.sha256(fingerprint_bytes).hexdigest()
        binding_key = self._exception_idempotency_binding_key(
            principal,
            request_id,
            idempotency_key,
            action=action,
        )
        with self._lock:
            for attempt in range(2):
                self._reload_store()
                request = self._business_exception_request_authority(request_id)
                previous = self._store.idempotency.get(binding_key)
                if previous is not None:
                    if previous[0] == command_fingerprint:
                        return {**copy.deepcopy(previous[1]), "replayed": True}
                    return {
                        "status": "conflict",
                        "replayed": False,
                        "request_id": request_id,
                        "reason_code": "IDEMPOTENCY_KEY_CONFLICT",
                    }
                app, run, _, status, _, actual_context = (
                    self._business_exception_current_context(request, now=event_time)
                )
                if expected_context != actual_context:
                    return {
                        "status": "stale",
                        "replayed": False,
                        "request_id": request_id,
                        "reason_code": "STALE_EXCEPTION_CONTEXT",
                    }
                if require_expiry and event_time < request["expires_at"]:
                    return {
                        "status": "not_due",
                        "replayed": False,
                        "request_id": request_id,
                        "expires_at": request["expires_at"],
                        "reason_code": "BUSINESS_EXCEPTION_NOT_EXPIRED",
                    }
                if status in {"expired", "invalidated", "rejected"}:
                    return {
                        "status": "already_inactive",
                        "replayed": False,
                        "request_id": request_id,
                        "reason_code": "BUSINESS_EXCEPTION_ALREADY_INACTIVE",
                    }
                if app.get("phase") == "Verification Completed":
                    return {
                        "status": "sealed",
                        "replayed": False,
                        "request_id": request_id,
                        "reason_code": "PROCESSING_CYCLE_ALREADY_SEALED",
                    }
                if app.get("phase") not in {
                    "Pending Exception Approval",
                    "Routing Determination",
                    "Manual Review",
                }:
                    return {
                        "status": "stale",
                        "replayed": False,
                        "request_id": request_id,
                        "reason_code": "STALE_EXCEPTION_CONTEXT",
                    }
                gate = self._review_write_gate(app=app)
                if gate is not None:
                    failure_status, failure_reason = gate
                    return {
                        "status": failure_status,
                        "replayed": False,
                        "request_id": request_id,
                        "reason_code": failure_reason,
                    }
                staged = copy.deepcopy(self._store)
                staged_app = staged.applications[request["application_id"]]
                staged_run = next(
                    item
                    for item in staged.runs
                    if item.get("run_record_id") == run.get("run_record_id")
                )
                current_manual_work = [
                    item
                    for item in staged.work_items
                    if item.get("application_id") == request["application_id"]
                    and item.get("run_id") == request["run_id"]
                    and item.get("kind") == "manual_review"
                    and item.get("lifecycle_revision")
                    == staged_app.get("lifecycle_revision")
                ]
                try:
                    self._before_write(f"{prefix}.lifecycle")
                    staged_app["route"] = "manual_review"
                    self._transition_lifecycle(
                        staged_app,
                        "Manual Review",
                        (
                            "BUSINESS_EXCEPTION_EXPIRED"
                            if require_expiry
                            else "BUSINESS_EXCEPTION_INVALIDATED"
                        ),
                        store=staged,
                    )
                    staged.lifecycle_events[-1].update(
                        {
                            "run_id": request["run_id"],
                            "request_id": request_id,
                            "exception_id": request_id,
                            "expires_at": request["expires_at"],
                            "invalidation_reason_code": reason_code,
                        }
                    )
                    record_id = self._stable_id(
                        prefix,
                        f"{request_id}:{reason_code}:{event_time}",
                    )
                    self._before_write(f"{prefix}.record")
                    deactivation_record = {
                        "record_id": record_id,
                        "record_type": record_type,
                        "schema_version": schema,
                        "request_id": request_id,
                        "exception_id": request_id,
                        "work_item_id": request["work_item_id"],
                        "application_id": request["application_id"],
                        "run_id": request["run_id"],
                        "finding_id": request["finding_id"],
                        "status": status_value,
                        "reason_code": reason_code,
                        "expires_at": request["expires_at"],
                        "lifecycle_revision": staged_app["lifecycle_revision"],
                    }
                    deactivation_record[
                        "expired_at" if require_expiry else "invalidated_at"
                    ] = event_time
                    staged.review_records.append(deactivation_record)
                    sequence = 1 + sum(
                        record.get("work_item_id") == request["work_item_id"]
                        and str(record.get("record_type", "")).startswith(
                            "exception_work_item_"
                        )
                        for record in staged.review_records
                    )
                    self._before_write(f"{prefix}.work_item")
                    staged.review_records.append(
                        {
                            "record_id": self._stable_id(
                                "review_record",
                                f"{request['work_item_id']}:{mode}:{sequence}",
                            ),
                            "record_type": "exception_work_item_invalidated",
                            "sequence": sequence,
                            "work_item_id": request["work_item_id"],
                            "request_id": request_id,
                            "exception_id": request_id,
                            "application_id": request["application_id"],
                            "run_id": request["run_id"],
                            "invalidated_at": event_time,
                            "reason_code": reason_code,
                            "recorded_at": event_time,
                        }
                    )
                    for manual_work in current_manual_work:
                        manual_sequence = 1 + sum(
                            record.get("work_item_id")
                            == manual_work["work_item_id"]
                            and str(record.get("record_type", "")).startswith(
                                "work_item_"
                            )
                            for record in staged.review_records
                        )
                        staged.review_records.append(
                            {
                                "record_id": self._stable_id(
                                    "review_record",
                                    f"{manual_work['work_item_id']}:{mode}:"
                                    f"{manual_sequence}",
                                ),
                                "record_type": "work_item_invalidated",
                                "sequence": manual_sequence,
                                "work_item_id": manual_work["work_item_id"],
                                "application_id": request["application_id"],
                                "run_id": request["run_id"],
                                "request_id": request_id,
                                "exception_id": request_id,
                                "invalidated_at": event_time,
                                "reason_code": reason_code,
                                "recorded_at": event_time,
                            }
                        )
                    blocker_ids = [
                        item["finding_id"]
                        for item in staged.findings
                        if item.get("application_id") == request["application_id"]
                        and item.get("run_id") == request["run_id"]
                        and item.get("mandatory") is True
                        and item.get("verdict") != "consistent"
                    ]
                    self._before_write(f"{prefix}.review_successor")
                    successor_work_item_id = self._create_manual_review_successor(
                        staged,
                        staged_app,
                        staged_run,
                        finding_ids=blocker_ids,
                        predecessor_request_id=request_id,
                    )
                    self._before_write(f"{prefix}.audit")
                    staged.audit_events.append(
                        {
                            "event_id": self._stable_id("audit", f"{action}:{record_id}"),
                            "action": action,
                            "subject": principal.subject,
                            "role": principal.role,
                            "scope": self._application_visibility_scope(
                                request["application_id"]
                            ),
                            "source_id": principal.source_id,
                            "application_id": request["application_id"],
                            "run_id": request["run_id"],
                            "finding_id": request["finding_id"],
                            "request_id": request_id,
                            "exception_id": request_id,
                            "work_item_id": request["work_item_id"],
                            "successor_work_item_id": successor_work_item_id,
                            "expires_at": request["expires_at"],
                            "reason_code": reason_code,
                            "lifecycle_revision": staged_app[
                                "lifecycle_revision"
                            ],
                            "evidence_revision": staged_app["evidence_revision"],
                            "result": "accepted",
                            **self._audit_time_fields(staged, now=event_time),
                        }
                    )
                    result = {
                        "status": "accepted",
                        "replayed": False,
                        "application_id": request["application_id"],
                        "request_id": request_id,
                        "phase": "Manual Review",
                        "route": "manual_review",
                        "expires_at": request["expires_at"],
                        "reason_code": reason_code,
                        "successor_work_item_id": successor_work_item_id,
                        "lifecycle_revision": staged_app[
                            "lifecycle_revision"
                        ],
                        "evidence_revision": staged_app["evidence_revision"],
                    }
                    self._before_write(f"{prefix}.idempotency")
                    staged.idempotency[binding_key] = (
                        command_fingerprint,
                        copy.deepcopy(result),
                    )
                    self._before_write(f"{prefix}.publish")
                    staged.persist()
                except _StoreWriteFailure as error:
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "request_id": request_id,
                        "reason_code": (
                            "AUDIT_UNAVAILABLE"
                            if str(error) == f"{prefix}.audit"
                            else "STORAGE_UNAVAILABLE"
                        ),
                    }
                except StaleStoreRevision:
                    if attempt == 0:
                        continue
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "request_id": request_id,
                        "reason_code": "STORAGE_UNAVAILABLE",
                    }
                self._store = staged
                return result
            raise RuntimeError("business exception deactivation retry exhausted")

    def _record_s07_routing_failure(
        self,
        *,
        principal: S01CommandPrincipal,
        request: dict[str, Any],
        decision: dict[str, Any],
        run: dict[str, Any],
        job: dict[str, Any],
        routing_context: dict[str, Any],
        binding_key: str,
        command_fingerprint: str,
        now: int,
    ) -> dict[str, Any]:
        failure = self._S07_ROUTING_FAILURE
        retry_policy = {
            "id": self._S07_RETRY_POLICY_ID,
            "max_attempts": 3,
            "retry_offsets_seconds": [1, 2],
            "jitter": False,
        }
        retry_policy_digest = hashlib.sha256(
            json.dumps(
                retry_policy, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        condition = {
            "condition_id": failure["criterion_id"],
            "reason_code": failure["primary_reason_code"],
        }
        criterion_body = {
            "id": failure["criterion_id"],
            "version": "1",
            "operation": failure["operation"],
            "dependency": failure["dependency"],
            "required_conditions": [failure["primary_reason_code"]],
            "trusted_verifier": failure["responsible_party"],
            "evidence_kind": failure["evidence_kind"],
            "conditions": [condition],
        }
        criterion_digest = hashlib.sha256(
            json.dumps(
                criterion_body, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        application_id = str(request["application_id"])
        recovery_work_id = self._stable_id(
            "recovery_work",
            ":".join(
                (
                    application_id,
                    str(request["cycle"]),
                    str(job["job_id"]),
                    criterion_digest,
                )
            ),
        )
        staged = copy.deepcopy(self._store)
        staged_app = staged.applications[application_id]
        staged_job = next(
            item for item in staged.jobs if item.get("job_id") == job["job_id"]
        )
        attempt_no = int(staged_job.get("attempt_no", 0)) + 1
        fence = int(staged_job.get("fence", 0)) + 1
        attempt_id = self._stable_id(
            "attempt", f"{staged_job['job_id']}:{attempt_no}:{principal.subject}"
        )
        spec = run.get("spec")
        if not isinstance(spec, dict):
            return {
                "status": "unavailable",
                "replayed": False,
                "request_id": request["request_id"],
                "reason_code": "recovery.authority_unavailable",
            }
        pre_block_revision = int(staged_app["lifecycle_revision"])
        try:
            self._before_write("s07.routing_failure.attempt")
            staged_job.update(
                {
                    "status": "blocked",
                    "retryable": False,
                    "terminal_reason_code": failure["primary_reason_code"],
                    "worker_id": principal.subject,
                    "fence": fence,
                    "attempt_no": attempt_no,
                }
            )
            staged.attempts.append(
                {
                    "attempt_id": attempt_id,
                    "job_id": staged_job["job_id"],
                    "application_id": application_id,
                    "worker_id": principal.subject,
                    "fence": fence,
                    "attempt_no": attempt_no,
                    "started_at": now,
                    "status": "terminal_failure",
                    "failure_classification": "terminal",
                    "retry_not_before": None,
                }
            )
            self._before_write("s07.routing_failure.lifecycle")
            staged_app["evidence_ready"] = False
            staged_app["route"] = "unprocessable"
            staged_app["projection_visible"] = False
            staged_app["projection_pending"] = False
            self._transition_lifecycle(
                staged_app,
                "Unprocessable",
                failure["primary_reason_code"],
                store=staged,
            )
            staged.lifecycle_events[-1].update(
                {
                    "run_id": request["run_id"],
                    "request_id": request["request_id"],
                    "decision_id": decision["decision_id"],
                    "recovery_work_id": recovery_work_id,
                    "responsible_party": failure["responsible_party"],
                    "recovery_action": failure["recovery_action"],
                    "recovery_target": failure["recovery_target"],
                }
            )
            opened = {
                "event_id": self._stable_id(
                    "recovery_event", f"{recovery_work_id}:opened"
                ),
                "kind": "opened",
                "schema_version": "recovery-work/1",
                "recovery_work_id": recovery_work_id,
                "application_id": application_id,
                "visibility_scope": self._application_visibility_scope(application_id),
                "cycle": staged_app["cycle"],
                "evidence_revision": staged_app["evidence_revision"],
                "release_id": spec["release_id"],
                "release_digest": spec["release_digest"],
                "checker_build": spec["checker_build"],
                "pre_block_lifecycle_revision": pre_block_revision,
                "lifecycle_revision": staged_app["lifecycle_revision"],
                "failed_from_phase": "Routing Determination",
                "operation": failure["operation"],
                "logical_operation_id": staged_job["logical_operation_id"],
                "job_id": staged_job["job_id"],
                "attempt_ids": [attempt_id],
                "dependency": failure["dependency"],
                "safe_correlation_id": self._stable_id(
                    "correlation", f"{staged_job['job_id']}:{attempt_id}"
                ),
                "primary_reason_code": failure["primary_reason_code"],
                "related_reason_codes": list(failure["related_reason_codes"]),
                "retry_policy": retry_policy,
                "retry_policy_digest": retry_policy_digest,
                "outcome_known": True,
                "responsible_party": failure["responsible_party"],
                "recovery_action": failure["recovery_action"],
                "recovery_target": failure["recovery_target"],
                "criterion": {**criterion_body, "digest": criterion_digest},
                "conditions": [condition],
                "request_id": request["request_id"],
                "decision_id": decision["decision_id"],
                "routing_context": copy.deepcopy(routing_context),
                "opened_at": now,
                "idempotency_fingerprint": hashlib.sha256(
                    f"{staged_job['job_id']}:routing_dependency".encode("utf-8")
                ).hexdigest(),
            }
            self._before_write("s07.routing_failure.recovery_work")
            staged.recovery_events.append(opened)
            self._before_write("s07.routing_failure.audit")
            staged.audit_events.append(
                {
                    "event_id": self._stable_id(
                        "audit", f"s07_routing_failure:{recovery_work_id}"
                    ),
                    "action": "protected_operation_failed",
                    "subject": principal.subject,
                    "role": principal.role,
                    "scope": principal.scope,
                    "source_id": principal.source_id,
                    "application_id": application_id,
                    "job_id": staged_job["job_id"],
                    "attempt_id": attempt_id,
                    "request_id": request["request_id"],
                    "decision_id": decision["decision_id"],
                    "recovery_work_id": recovery_work_id,
                    "result": "blocked",
                    "reason_code": failure["primary_reason_code"],
                    "lifecycle_revision": staged_app["lifecycle_revision"],
                    **self._audit_time_fields(staged, now=now),
                }
            )
            result = {
                "status": "blocked",
                "replayed": False,
                "application_id": application_id,
                "request_id": request["request_id"],
                "decision_id": decision["decision_id"],
                "job_id": staged_job["job_id"],
                "attempt_id": attempt_id,
                "recovery_work_id": recovery_work_id,
                "phase": "Unprocessable",
                "route": "unprocessable",
                "lifecycle_revision": staged_app["lifecycle_revision"],
                "evidence_revision": staged_app["evidence_revision"],
            }
            self._before_write("s07.routing_failure.idempotency")
            staged.idempotency[binding_key] = (
                command_fingerprint,
                copy.deepcopy(result),
            )
            self._before_write("s07.routing_failure.outbox")
            staged.outbox.append(
                {
                    "event_id": self._stable_id(
                        "outbox", f"s07_recovery:{recovery_work_id}"
                    ),
                    "kind": "s07_recovery_work_opened",
                    "application_id": application_id,
                    "recovery_work_id": recovery_work_id,
                    "lifecycle_revision": staged_app["lifecycle_revision"],
                    "visibility_scope": opened["visibility_scope"],
                    "status": "pending",
                }
            )
            self._before_write("s07.routing_failure.publish")
            staged.persist()
        except (_StoreWriteFailure, StaleStoreRevision):
            return {
                "status": "unavailable",
                "replayed": False,
                "request_id": request["request_id"],
                "reason_code": "recovery.authority_unavailable",
            }
        self._store = staged
        return result

    def determine_business_exception_route(
        self,
        *,
        principal: S01CommandPrincipal,
        request_id: str,
        expected_context: dict[str, Any],
        idempotency_key: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        route_time = int(self._clock() if now is None else now)
        if not self._valid_exception_router_principal(principal, now=route_time):
            raise QueryNotFound(request_id)
        if not self._valid_idempotency_key(idempotency_key):
            raise ValueError("business exception route idempotency key is invalid")
        fingerprint_bytes = json.dumps(
            {
                "expected_context": expected_context,
                "request_id": request_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        command_fingerprint = hashlib.sha256(fingerprint_bytes).hexdigest()
        binding_key = self._exception_idempotency_binding_key(
            principal,
            request_id,
            idempotency_key,
            action="determine_business_exception_route",
        )
        with self._lock:
            for attempt in range(2):
                self._reload_store()
                request = self._business_exception_request_authority(request_id)
                previous = self._store.idempotency.get(binding_key)
                if previous is not None:
                    if previous[0] == command_fingerprint:
                        return {**copy.deepcopy(previous[1]), "replayed": True}
                    return {
                        "status": "conflict",
                        "replayed": False,
                        "request_id": request_id,
                        "reason_code": "IDEMPOTENCY_KEY_CONFLICT",
                    }
                if (
                    self._business_exception_operations_state()["operations"]
                    != "open"
                ):
                    return {
                        "status": "stopped",
                        "replayed": False,
                        "request_id": request_id,
                        "reason_code": self._EXCEPTION_OPERATIONS_CLOSED,
                    }
                decisions = [
                    record
                    for record in self._store.review_records
                    if record.get("record_type") == "business_exception_decision"
                    and record.get("request_id") == request_id
                ]
                if len(decisions) != 1 or decisions[0].get("decision") != "approved":
                    return {
                        "status": "stale",
                        "replayed": False,
                        "request_id": request_id,
                        "reason_code": "APPROVED_EXCEPTION_DECISION_REQUIRED",
                    }
                decision = decisions[0]
                app, run, finding, status, current, _ = (
                    self._business_exception_current_context(request, now=route_time)
                )
                actual_context = self._business_exception_routing_context(
                    request, decision, app
                )
                if expected_context != actual_context:
                    return {
                        "status": "stale",
                        "replayed": False,
                        "request_id": request_id,
                        "reason_code": "STALE_ROUTING_CONTEXT",
                    }
                rule = self._exception_policy_rule(app, run, finding)
                if (
                    status != "approved"
                    or not current
                    or route_time >= request["expires_at"]
                    or app.get("phase") != "Routing Determination"
                    or app.get("route") != "routing_determination"
                    or finding.get("rule_id") in self._PROTECTED_EXCEPTION_CHECKS
                    or rule is None
                    or rule.waivable is not True
                    or rule.waiver_policy_digest != request["waiver_policy_digest"]
                ):
                    return {
                        "status": "stale",
                        "replayed": False,
                        "request_id": request_id,
                        "reason_code": (
                            "BUSINESS_EXCEPTION_EXPIRED"
                            if route_time >= request["expires_at"]
                            else "STALE_ROUTING_CONTEXT"
                        ),
                    }
                gate = self._review_write_gate(app=app)
                if gate is not None:
                    failure_status, failure_reason = gate
                    return {
                        "status": failure_status,
                        "replayed": False,
                        "request_id": request_id,
                        "reason_code": failure_reason,
                    }
                route_jobs = [
                    item
                    for item in self._store.jobs
                    if item.get("application_id") == request["application_id"]
                    and item.get("request_id") == request_id
                    and item.get("decision_id") == decision["decision_id"]
                    and item.get("kind")
                    in {"business_exception_route", "recovery_route"}
                ]
                queued_route_jobs = [
                    item for item in route_jobs if item.get("status") == "queued"
                ]
                if len(queued_route_jobs) != 1:
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "request_id": request_id,
                        "reason_code": "routing.job_authority_unavailable",
                    }
                route_job = queued_route_jobs[0]
                if route_job.get("routing_context") != actual_context:
                    return {
                        "status": "stale",
                        "replayed": False,
                        "request_id": request_id,
                        "reason_code": "STALE_ROUTING_CONTEXT",
                    }
                try:
                    self._before_write("exception_route.operation")
                except _StoreWriteFailure:
                    return self._record_s07_routing_failure(
                        principal=principal,
                        request=request,
                        decision=decision,
                        run=run,
                        job=route_job,
                        routing_context=actual_context,
                        binding_key=binding_key,
                        command_fingerprint=command_fingerprint,
                        now=route_time,
                    )
                outstanding = [
                    item
                    for item in self._store.findings
                    if item.get("application_id") == request["application_id"]
                    and item.get("run_id") == request["run_id"]
                    and item.get("mandatory") is True
                    and item.get("verdict") != "consistent"
                    and item.get("finding_id") != request["finding_id"]
                ]
                staged = copy.deepcopy(self._store)
                staged_app = staged.applications[request["application_id"]]
                staged_run = next(
                    item
                    for item in staged.runs
                    if item.get("run_record_id") == run.get("run_record_id")
                )
                staged_route_job = next(
                    item
                    for item in staged.jobs
                    if item.get("job_id") == route_job["job_id"]
                )
                route_attempt_no = int(staged_route_job.get("attempt_no", 0)) + 1
                route_fence = int(staged_route_job.get("fence", 0)) + 1
                route_attempt_id = self._stable_id(
                    "attempt",
                    f"{staged_route_job['job_id']}:{route_attempt_no}:{principal.subject}",
                )
                try:
                    staged_route_job.update(
                        {
                            "status": "complete",
                            "worker_id": principal.subject,
                            "fence": route_fence,
                            "attempt_no": route_attempt_no,
                            "completed_at": route_time,
                        }
                    )
                    staged.attempts.append(
                        {
                            "attempt_id": route_attempt_id,
                            "job_id": staged_route_job["job_id"],
                            "application_id": request["application_id"],
                            "worker_id": principal.subject,
                            "fence": route_fence,
                            "attempt_no": route_attempt_no,
                            "started_at": route_time,
                            "status": "complete",
                            "completed_at": route_time,
                            "run_spec": copy.deepcopy(run["spec"]),
                        }
                    )
                    self._before_write("exception_route.lifecycle")
                    if outstanding:
                        staged_app["route"] = "manual_review"
                        self._transition_lifecycle(
                            staged_app,
                            "Manual Review",
                            "MANDATORY_BLOCKER_REMAINS_AFTER_EXCEPTION",
                            store=staged,
                        )
                    else:
                        staged_app["route"] = "human_complete"
                        self._transition_lifecycle(
                            staged_app,
                            "Verification Completed",
                            "BUSINESS_EXCEPTION_COMPLETED",
                            store=staged,
                        )
                    staged.lifecycle_events[-1].update(
                        {
                            "run_id": request["run_id"],
                            "request_id": request_id,
                            "exception_id": request_id,
                            "decision_id": decision["decision_id"],
                            "expires_at": request["expires_at"],
                            "completion_basis": "business_exception",
                        }
                    )
                    successor_work_item_id = None
                    if outstanding:
                        self._before_write("exception_route.review_successor")
                        successor_work_item_id = self._create_manual_review_successor(
                            staged,
                            staged_app,
                            staged_run,
                            finding_ids=[item["finding_id"] for item in outstanding],
                            predecessor_request_id=request_id,
                        )
                    route_record_id = self._stable_id(
                        "exception_route",
                        f"{request_id}:{decision['decision_id']}:{staged_app['lifecycle_revision']}",
                    )
                    self._before_write("exception_route.record")
                    staged.review_records.append(
                        {
                            "record_id": route_record_id,
                            "record_type": "business_exception_route",
                            "schema_version": "business-exception-route/1",
                            "request_id": request_id,
                            "exception_id": request_id,
                            "decision_id": decision["decision_id"],
                            "application_id": request["application_id"],
                            "run_id": request["run_id"],
                            "finding_id": request["finding_id"],
                            "route": staged_app["route"],
                            "completion_basis": "business_exception",
                            "expires_at": request["expires_at"],
                            "lifecycle_revision": staged_app[
                                "lifecycle_revision"
                            ],
                            "routed_at": route_time,
                        }
                    )
                    self._before_write("exception_route.audit")
                    staged.audit_events.append(
                        {
                            "event_id": self._stable_id(
                                "audit", f"business_exception_routed:{route_record_id}"
                            ),
                            "action": "business_exception_routed",
                            "subject": principal.subject,
                            "role": principal.role,
                            "scope": self._application_visibility_scope(
                                request["application_id"]
                            ),
                            "source_id": principal.source_id,
                            "application_id": request["application_id"],
                            "run_id": request["run_id"],
                            "finding_id": request["finding_id"],
                            "request_id": request_id,
                            "exception_id": request_id,
                            "decision_id": decision["decision_id"],
                            "route": staged_app["route"],
                            "completion_basis": "business_exception",
                            "expires_at": request["expires_at"],
                            "successor_work_item_id": successor_work_item_id,
                            "mandatory_blocker_count": len(outstanding),
                            "lifecycle_revision": staged_app[
                                "lifecycle_revision"
                            ],
                            "evidence_revision": staged_app["evidence_revision"],
                            "result": "accepted",
                            **self._audit_time_fields(staged, now=route_time),
                        }
                    )
                    result = {
                        "status": "accepted",
                        "replayed": False,
                        "application_id": request["application_id"],
                        "request_id": request_id,
                        "decision_id": decision["decision_id"],
                        "phase": staged_app["phase"],
                        "route": staged_app["route"],
                        "completion_basis": "business_exception",
                        "successor_work_item_id": successor_work_item_id,
                        "lifecycle_revision": staged_app[
                            "lifecycle_revision"
                        ],
                        "evidence_revision": staged_app["evidence_revision"],
                    }
                    self._before_write("exception_route.idempotency")
                    staged.idempotency[binding_key] = (
                        command_fingerprint,
                        copy.deepcopy(result),
                    )
                    self._before_write("exception_route.publish")
                    staged.persist()
                except _StoreWriteFailure as error:
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "request_id": request_id,
                        "reason_code": (
                            "AUDIT_UNAVAILABLE"
                            if str(error) == "exception_route.audit"
                            else "STORAGE_UNAVAILABLE"
                        ),
                    }
                except StaleStoreRevision:
                    if attempt == 0:
                        continue
                    return {
                        "status": "unavailable",
                        "replayed": False,
                        "request_id": request_id,
                        "reason_code": "STORAGE_UNAVAILABLE",
                    }
                self._store = staged
                return result
            raise RuntimeError("business exception routing retry exhausted")

    def submit_review_work_item(
        self,
        *,
        principal: S01CommandPrincipal,
        work_item_id: str,
        expected_fence: int,
        expected_context: dict[str, Any],
        idempotency_key: str,
        verification: dict[str, Any],
        now: float | None = None,
    ) -> dict[str, Any]:
        """Atomically complete one claimed work item with an immutable decision."""
        submit_time = int(self._clock() if now is None else now)
        if not self._valid_reviewer_principal(principal, now=submit_time):
            raise QueryNotFound(work_item_id)
        if not self._valid_idempotency_key(idempotency_key):
            raise ValueError("review idempotency key is invalid")
        normalized = self._canonical_review_verification(verification)

        with self._lock:
            self._reload_store()
            work_item, state = self._review_work_item_authority(
                principal=principal,
                work_item_id=work_item_id,
                now=submit_time,
            )
            app, run, actual_context = self._review_current_context(work_item)
            try:
                fingerprint_bytes = json.dumps(
                    {
                        "expected_context": expected_context,
                        "expected_fence": expected_fence,
                        "verification": normalized,
                        "work_item_id": work_item_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError):
                return {
                    "status": "stale",
                    "replayed": False,
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "reason_code": "STALE_REVIEW_CONTEXT",
                }
            command_fingerprint = hashlib.sha256(fingerprint_bytes).hexdigest()
            binding_key = self._review_idempotency_binding_key(
                principal, work_item_id, idempotency_key
            )
            previous = self._store.idempotency.get(binding_key)
            if previous is not None:
                previous_fingerprint, previous_result = previous
                if previous_fingerprint == command_fingerprint:
                    return {**copy.deepcopy(previous_result), "replayed": True}
                return {
                    "status": "conflict",
                    "replayed": False,
                    "application_id": previous_result["application_id"],
                    "work_item_id": previous_result["work_item_id"],
                    "reason_code": "IDEMPOTENCY_KEY_CONFLICT",
                }
            if not self._review_context_matches(expected_context, actual_context):
                return {
                    "status": "stale",
                    "replayed": False,
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "reason_code": "STALE_REVIEW_CONTEXT",
                }
            if (
                state["status"] != "claimed"
                or state["claim_subject"] != principal.subject
                or state["claim_fence"] != expected_fence
                or float(state["claim_expires_at"]) <= submit_time
            ):
                return {
                    "status": "stale",
                    "replayed": False,
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "reason_code": "STALE_WORK_ITEM_CLAIM",
                }
            if {
                decision["finding_id"]
                for decision in normalized["finding_decisions"]
            } != set(work_item["finding_ids"]):
                raise ValueError("review verification must decide every work-item finding")
            if (
                app.get("phase") != "Manual Review"
                or app.get("current_run_id") != work_item["run_id"]
                or app.get("lifecycle_revision") != work_item["lifecycle_revision"]
                or app.get("evidence_revision") != work_item["evidence_revision"]
            ):
                return {
                    "status": "stale",
                    "replayed": False,
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "reason_code": "STALE_REVIEW_CONTEXT",
                }

            gate = self._review_write_gate(app=app)
            if gate is not None:
                status, reason_code = gate
                return {
                    "status": status,
                    "replayed": False,
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "reason_code": reason_code,
                }

            staged = copy.deepcopy(self._store)
            staged_app = staged.applications[work_item["application_id"]]
            compatibility = (
                self._review_compatibility_summary(
                    app,
                    run,
                    work_item,
                    reason_code=normalized["reason_code"],
                )
                if app.get("legacy_oracle_outcomes") or (
                            run.get("semantic_differential", {}).get("status")
                            == "bundle_bound"
                        )
                else None
            )
            decision_id = self._stable_id(
                "decision", f"{binding_key}:{command_fingerprint}"
            )
            sequence = 1 + sum(
                record.get("work_item_id") == work_item_id
                and str(record.get("record_type", "")).startswith("work_item_")
                for record in staged.review_records
            )
            try:
                self._before_write("review.lifecycle")
                self._transition_lifecycle(
                    staged_app,
                    "Verification Completed",
                    "HUMAN_REVIEW_COMPLETED",
                    store=staged,
                )
                staged.lifecycle_events[-1]["run_id"] = work_item["run_id"]
                staged_app["route"] = "human_complete"
                self._before_write("review.decision")
                staged.review_records.append(
                    {
                        "record_id": decision_id,
                        "record_type": "human_decision",
                        "decision_id": decision_id,
                        "work_item_id": work_item_id,
                        "application_id": work_item["application_id"],
                        "run_id": work_item["run_id"],
                        "reviewer_subject": principal.subject,
                        "reviewer_role": principal.role,
                        "reviewer_source_id": principal.source_id,
                        "assigned_subject": work_item["assigned_subject"],
                        "cycle": work_item["cycle"],
                        "finding_ids": copy.deepcopy(work_item["finding_ids"]),
                        "evidence_snapshot_id": work_item["evidence_snapshot_id"],
                        "release_id": work_item["release_id"],
                        "fixed_context": copy.deepcopy(actual_context),
                        "claim_fence": expected_fence,
                        "lifecycle_revision": staged_app["lifecycle_revision"],
                        "evidence_revision": staged_app["evidence_revision"],
                        "submitted_at": submit_time,
                        **(
                            {"compatibility": compatibility}
                            if compatibility is not None
                            else {}
                        ),
                        **copy.deepcopy(normalized),
                    }
                )
                self._before_write("review.work_item")
                staged.review_records.append(
                    {
                        "record_id": self._stable_id(
                            "review_record", f"{work_item_id}:complete:{sequence}"
                        ),
                        "record_type": "work_item_completed",
                        "sequence": sequence,
                        "work_item_id": work_item_id,
                        "application_id": work_item["application_id"],
                        "run_id": work_item["run_id"],
                        "claim_subject": principal.subject,
                        "claim_fence": expected_fence,
                        "decision_id": decision_id,
                        "completed_at": submit_time,
                        "recorded_at": submit_time,
                    }
                )
                self._before_write("review.audit")
                staged.audit_events.append(
                    {
                        "event_id": self._stable_id(
                            "audit", f"human_decision_submitted:{decision_id}"
                        ),
                        "action": "human_decision_submitted",
                        "subject": principal.subject,
                        "role": principal.role,
                        "scope": work_item["visibility_scope"],
                        "source_id": principal.source_id,
                        "application_id": work_item["application_id"],
                        "run_id": work_item["run_id"],
                        "route": "human_complete",
                        "lifecycle_revision": staged_app["lifecycle_revision"],
                        "evidence_revision": staged_app["evidence_revision"],
                        "work_item_id": work_item_id,
                        "decision_id": decision_id,
                        "outcome": normalized["outcome"],
                        "reason_code": normalized["reason_code"],
                        "claim_fence": expected_fence,
                        "result": "accepted",
                        **self._audit_time_fields(staged, now=submit_time),
                    }
                )
                result = {
                    "status": "accepted",
                    "replayed": False,
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "decision_id": decision_id,
                    "claim_fence": expected_fence,
                    "lifecycle_revision": staged_app["lifecycle_revision"],
                    "evidence_revision": staged_app["evidence_revision"],
                    "route": "human_complete",
                }
                self._before_write("review.idempotency")
                staged.idempotency[binding_key] = (
                    command_fingerprint,
                    copy.deepcopy(result),
                )
                staged.persist()
            except (StaleStoreRevision, _StoreWriteFailure) as error:
                return {
                    "status": "unavailable",
                    "replayed": False,
                    "application_id": work_item["application_id"],
                    "work_item_id": work_item_id,
                    "reason_code": (
                        "AUDIT_UNAVAILABLE"
                        if str(error) == "review.audit"
                        else "STORAGE_UNAVAILABLE"
                    ),
                }
            self._store = staged
            return result

    def review_work_item_view(
        self,
        *,
        principal: S01CommandPrincipal,
        work_item_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Return the immutable-authority view of one visible review work item."""
        query_time = float(self._clock() if now is None else now)
        with self._lock:
            self._reload_store()
            work_item, state = self._review_work_item_authority(
                principal=principal,
                work_item_id=work_item_id,
                now=query_time,
            )
            status = state["status"]
            if status == "claimed" and float(state["claim_expires_at"]) <= query_time:
                status = "expired"
            app, run, command_context = self._review_current_context(work_item)
            run_bytes = json.dumps(
                run,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            findings_by_id = {
                finding["finding_id"]: finding
                for finding in self._store.findings
                if finding.get("application_id") == work_item["application_id"]
                and finding.get("run_id") == work_item["run_id"]
            }
            if any(
                finding_id not in findings_by_id
                for finding_id in work_item["finding_ids"]
            ):
                raise RuntimeError("review work-item finding authority is unavailable")
            automatic_findings = []
            for finding_id in work_item["finding_ids"]:
                finding = findings_by_id[finding_id]
                projected_finding = {
                    key: finding[key]
                    for key in (
                        "finding_id",
                        "rule_id",
                        "verdict",
                        "severity",
                        "reason_code",
                    )
                }
                if isinstance(finding.get("membership"), dict):
                    projected_finding["membership"] = copy.deepcopy(
                        finding["membership"]
                    )
                automatic_findings.append(projected_finding)
            decision_records = {
                record["decision_id"]: record
                for record in self._store.review_records
                if record.get("record_type") == "human_decision"
                and record.get("work_item_id") == work_item_id
                and record.get("decision_id") in state["decision_ids"]
            }
            if len(decision_records) != len(state["decision_ids"]):
                raise RuntimeError("human decision authority is unavailable")
            decisions = [
                {
                    key: copy.deepcopy(decision_records[decision_id][key])
                    for key in (
                        "decision_id",
                        "schema_version",
                        "outcome",
                        "reason_code",
                        "finding_decisions",
                        "reviewer_subject",
                        "reviewer_role",
                        "reviewer_source_id",
                        "assigned_subject",
                        "cycle",
                        "finding_ids",
                        "evidence_snapshot_id",
                        "release_id",
                        "compatibility",
                        "note_metadata",
                        "fixed_context",
                        "claim_fence",
                        "submitted_at",
                    )
                    if key not in {"compatibility", "note_metadata"}
                    or key in decision_records[decision_id]
                }
                for decision_id in state["decision_ids"]
            ]
            decision = decisions[0] if len(decisions) == 1 else None
            return {
                "status": status,
                "application_id": work_item["application_id"],
                "work_item_id": work_item_id,
                "claim_subject": state["claim_subject"],
                "claim_fence": state["claim_fence"],
                "claim_expires_at": state["claim_expires_at"],
                "phase": app["phase"],
                "route": app["route"],
                "lifecycle_revision": app["lifecycle_revision"],
                "evidence_revision": app["evidence_revision"],
                "command_context": command_context,
                "automatic_findings": automatic_findings,
                "run_authority": {
                    "run_id": run["run_id"],
                    "status": run["status"],
                    "authority_digest": hashlib.sha256(run_bytes).hexdigest(),
                },
                "decision": decision,
                "decisions": decisions,
                "completed_finding_ids": copy.deepcopy(
                    state["completed_finding_ids"]
                ),
            }

    def queue_view(
        self,
        *,
        role: str = "",
        scope: str = "",
        subject: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        """Return a minimized Reviewer queue projection, hiding unauthorized scope.

        The response is backward compatible: ``items`` keeps its exact meaning
        and fields.  The additive ``recovery_items`` collection is produced by
        the Lifecycle authority from open Recovery Work for applications still
        blocked in ``Unprocessable``; the requesting Reviewer can only see work
        in an authorized scope that is assigned to them.  It contains no raw
        evidence, verifier result, object reference, run specification,
        credential, or client-computed transition.
        """
        if role != "reviewer" or not self.is_controlled_scope(scope):
            return {"items": [], "recovery_items": [], "projection_watermark": 0}
        with self._lock:
            self._reload_store()
            self._repair_published_projections()
            query_subject = subject
            query_time = float(self._clock()) if now is None else float(now)
            visible_scopes = {scope}
            if scope.startswith(self._SESSION_SCOPE_PREFIX):
                visible_scopes.add("C-DEMO")
            queue_principal = S01CommandPrincipal(
                subject=query_subject,
                role=role,
                scope=scope,
                source_id="review-queue",
            )
            items: list[dict[str, Any]] = []
            recovery_items: list[dict[str, Any]] = []
            visible_watermark = 0
            for projection in self._store.projections.values():
                if projection.get("visibility_scope") not in visible_scopes:
                    continue
                if projection["phase"] != "Manual Review":
                    continue
                try:
                    _, review_state = self._review_work_item_authority(
                        principal=queue_principal,
                        work_item_id=projection.get("work_item_id", ""),
                        now=query_time,
                    )
                except QueryNotFound:
                    continue
                if review_state["status"] == "completed":
                    continue
                if (
                    review_state["status"] == "claimed"
                    and float(review_state["claim_expires_at"]) > query_time
                    and review_state["claim_subject"] != query_subject
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
                        "claim_fence": review_state["claim_fence"],
                        "claim_expires_at": review_state["claim_expires_at"],
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
            recovery_events_by_work: dict[str, list[dict[str, Any]]] = {}
            for event in self._store.recovery_events:
                recovery_events_by_work.setdefault(
                    str(event.get("recovery_work_id") or ""), []
                ).append(event)
            # The shared accepted-admission authority is validated once up
            # front: a broken shared authority must surface as an explicit
            # unavailable error at the web boundary, never as an
            # authoritative-looking empty queue.  With the shared authority
            # proven consistent (the store is immutable within the lock),
            # every later authority failure in this loop is item-local.
            self._accepted_admission_authorities()
            for opened in self._store.recovery_events:
                if opened.get("kind") != "opened":
                    continue
                if not self._recovery_scope_visible(
                    queue_principal, opened.get("visibility_scope")
                ):
                    continue
                recovery_work_id = str(opened["recovery_work_id"])
                work_events = recovery_events_by_work.get(recovery_work_id, [])
                if not self._recovery_work_is_open(work_events):
                    continue
                application_id = str(opened["application_id"])
                recovery_app = self._store.applications.get(application_id)
                if not isinstance(recovery_app, dict):
                    continue
                try:
                    self._require_application_state_authority(recovery_app)
                    if (
                        recovery_app.get("phase") != "Unprocessable"
                        or self._application_review_assignee(application_id)
                        != query_subject
                    ):
                        continue
                except _ApplicationStateAuthorityUnavailable:
                    # Item-local corruption (this application's envelope,
                    # lifecycle, or evidence disagrees with its own
                    # authority): fail closed per item and publish nothing
                    # for it.  Shared authority failures were already
                    # surfaced above and never reach this boundary.
                    continue
                recovery_items.append(
                    {
                        "recovery_work_id": recovery_work_id,
                        "application_id": application_id,
                        "status": "open",
                        "phase": "Unprocessable",
                        "primary_reason_code": str(
                            opened["primary_reason_code"]
                        ),
                        "responsible_party": str(opened["responsible_party"]),
                        "lifecycle_revision": int(
                            recovery_app["lifecycle_revision"]
                        ),
                        "projection_watermark": int(
                            self._store.projection_watermark
                        ),
                    }
                )
            if recovery_items:
                visible_watermark = max(
                    visible_watermark,
                    int(self._store.projection_watermark),
                )
            return {
                "items": items,
                "recovery_items": recovery_items,
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
            or not self.is_controlled_scope(principal.scope)
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
            "finding_id",
            "decision_id",
            "correction_id",
            "observation_id",
            "old_observation_id",
            "new_observation_id",
            "invalidated_run_id",
            "outcome",
            "claim_fence",
            "assigned_subject",
            "request_id",
            "exception_id",
            "successor_work_item_id",
            "expires_at",
            "completion_basis",
            "waiver_policy_id",
            "waiver_policy_digest",
            "invalidated_exception_ids",
            "admission_after_stop",
            "requeued_jobs",
            "admission_after_recovery",
        )
        with self._lock:
            self._reload_store()
            if application_id not in self._store.applications:
                raise QueryNotFound(application_id)
            application_scope = self._application_visibility_scope(application_id)
            visible = application_scope == principal.scope
            if principal.scope == "C-DEMO":
                visible = visible or application_scope.startswith(
                    self._SESSION_SCOPE_PREFIX
                )
            elif principal.scope.startswith(self._SESSION_SCOPE_PREFIX):
                visible = visible or application_scope == "C-DEMO"
            if not visible:
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
        if role != "reviewer" or not self.is_controlled_scope(scope):
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
            if projection is None or projection.get("visibility_scope") not in visible_scopes:
                raise QueryNotFound(application_id)
            # Shared currentness guard: an affected old run is never shown
            # as a live review workspace before Governance facts settle.
            if self._s09_currentness_block_reasons(
                self._store, self._store.applications.get(application_id) or {}
            ):
                raise QueryNotFound(application_id)
            workspace_principal = S01CommandPrincipal(
                subject=query_subject,
                role="reviewer",
                scope=scope,
                source_id="review-workspace",
            )
            try:
                work_item, review_state = self._review_work_item_authority(
                    principal=workspace_principal,
                    work_item_id=str(projection.get("work_item_id") or ""),
                    now=query_time,
                )
            except QueryNotFound:
                raise QueryNotFound(application_id) from None
            if projection["phase"] != "Manual Review" or review_state["status"] == "completed":
                raise QueryNotFound(application_id)
            findings = copy.deepcopy(projection["mandatory_blockers"])
            selected = next((f for f in findings if f["finding_id"] == finding_id), None)
            if selected is None and findings:
                selected = findings[0]
            result = {
                "application_id": application_id,
                "work_item_id": projection["work_item_id"],
                "assigned_subject": projection["assigned_subject"],
                "claim_fence": review_state["claim_fence"],
                "claim_expires_at": review_state["claim_expires_at"],
                "track": projection["track"],
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
            if projection["track"] == "R-OBSERVED":
                result.update(
                    {
                        "claim_label": "R-OBSERVED",
                        "real_cross_document_opportunities": 0,
                        "performance_status": "not_estimable",
                    }
                )
            if selected is not None:
                result["business_exception_eligibility"] = (
                    self._business_exception_eligibility(
                        work_item=work_item,
                        review_state=review_state,
                        finding=selected,
                        now=query_time,
                        subject=query_subject,
                    )
                )
            return result

    def _current_run_authority(
        self, app: dict[str, Any]
    ) -> dict[str, Any] | None:
        current_run_id = app.get("current_run_id")
        if current_run_id is None:
            phase_route = (app.get("phase"), app.get("route"))
            if phase_route in {
                ("Intake", "pending_check"),
                ("Assembly", "pending_check"),
                ("Evidence Ready", "pending_check"),
                ("Checking", "pending_check"),
                ("Awaiting Evidence", "awaiting_evidence"),
                ("Unprocessable", "unprocessable"),
            }:
                return None
            raise _ApplicationStateAuthorityUnavailable(self._APPLICATION_STATE_FAILURE)
        lifecycle = [
            event
            for event in self._store.lifecycle_events
            if event.get("application_id") == app.get("application_id")
            and event.get("revision") == app.get("lifecycle_revision")
            and event.get("run_id") == current_run_id
        ]
        matching = []
        for run in self._store.runs:
            spec = run.get("spec")
            if not isinstance(spec, dict):
                continue
            try:
                context_matches = run.get("completion_context") == self._completion_context(
                    spec
                )
            except KeyError:
                context_matches = False
            if (
                run.get("application_id") == app.get("application_id")
                and run.get("run_id") == current_run_id
                and run.get("status") == "complete"
                and spec.get("cycle") == app.get("cycle")
                and spec.get("evidence_revision") == app.get("evidence_revision")
                and spec.get("evidence_snapshot_id")
                == app.get("current_evidence_snapshot_id")
                and spec.get("evidence_snapshot_digest")
                == app.get("current_evidence_snapshot_digest")
                and context_matches
            ):
                matching.append(run)
        if len(lifecycle) != 1 or len(matching) != 1:
            raise _ApplicationStateAuthorityUnavailable(self._APPLICATION_STATE_FAILURE)
        return matching[0]

    def current_route_view(
        self,
        *,
        principal: S01CommandPrincipal,
        application_id: str,
    ) -> dict[str, Any]:
        """Return the Lifecycle-owned route and its current immutable run."""
        with self._lock:
            self._reload_store()
            app = self._reviewer_application_authority(principal, application_id)
            current_run = self._current_run_authority(app)
            # Shared currentness guard: authoritative Governance generation,
            # hold union and final-impact disposition receipts may mark the
            # reconstructed run non-current before Lifecycle consumption.
            guard_reasons = (
                self._s09_currentness_block_reasons(self._store, app)
                if current_run is not None
                else ()
            )
            spec = current_run.get("spec", {}) if current_run is not None else {}
            lifecycle_events = [
                event
                for event in self._store.lifecycle_events
                if event.get("application_id") == application_id
                and event.get("revision") == app["lifecycle_revision"]
                and not event.get("auxiliary")
            ]
            if len(lifecycle_events) != 1:
                raise RuntimeError("current lifecycle route authority is unavailable")
            lifecycle = lifecycle_events[0]
            result = {
                "schema_version": "s04-current-route/1",
                "application_id": application_id,
                "phase": app["phase"],
                "route": app["route"],
                "current_run_id": app.get("current_run_id"),
                "cycle": app["cycle"],
                "lifecycle_revision": app["lifecycle_revision"],
                "evidence_revision": app["evidence_revision"],
                "evidence_snapshot_id": app.get("current_evidence_snapshot_id"),
                "evidence_snapshot_digest": app.get(
                    "current_evidence_snapshot_digest"
                ),
                "release_id": spec.get("release_id"),
                "release_digest": spec.get("release_digest"),
                "checker_build": spec.get("checker_build"),
                "currentness_reason": (
                    guard_reasons[0]
                    if guard_reasons
                    else (
                        "CURRENT_CONTEXT_MATCH"
                        if current_run is not None
                        else "NO_CURRENT_RUN"
                    )
                ),
            }
            if lifecycle.get("exception_id") is not None:
                result.update(
                    {
                        "completion_basis": lifecycle.get("completion_basis"),
                        "exception_id": lifecycle["exception_id"],
                        "exception_decision_id": lifecycle.get("decision_id"),
                        "exception_expires_at": lifecycle.get("expires_at"),
                    }
                )
            failure_keys = (
                "reason_code",
                "responsible_party",
                "recovery_action",
                "recovery_target",
            )
            if all(key in lifecycle for key in failure_keys):
                result["failure"] = {
                    key: copy.deepcopy(lifecycle[key])
                    for key in failure_keys
                }
            return result

    def application_history_view(
        self,
        *,
        principal: S01CommandPrincipal,
        application_id: str,
    ) -> dict[str, Any]:
        """Return minimized immutable correction and run history for a Reviewer."""
        with self._lock:
            self._reload_store()
            app = self._reviewer_application_authority(principal, application_id)
            current_run = self._current_run_authority(app)
            # Shared currentness guard: when authoritative Governance facts
            # invalidate the reconstructed run, the history frame reports it
            # as non-current with the stable guard reason.
            guard_reasons = (
                self._s09_currentness_block_reasons(self._store, app)
                if current_run is not None
                else ()
            )
            guard_current_run = current_run if guard_reasons else None
            if guard_reasons:
                current_run = None
            exception_requests = [
                record
                for record in self._store.review_records
                if record.get("record_type") == "business_exception_request"
                and record.get("application_id") == application_id
            ]
            business_exceptions = []
            applicable_exception_ids: set[str] = set()
            for request in exception_requests:
                _, _, _, status, current, _ = self._business_exception_current_context(
                    request, now=float(self._clock())
                )
                decisions = [
                    record
                    for record in self._store.review_records
                    if record.get("record_type") == "business_exception_decision"
                    and record.get("request_id") == request["request_id"]
                ]
                routes = [
                    record
                    for record in self._store.review_records
                    if record.get("record_type") == "business_exception_route"
                    and record.get("request_id") == request["request_id"]
                ]
                if len(decisions) > 1 or len(routes) > 1:
                    raise RuntimeError("business exception history is not unique")
                decision = decisions[0] if decisions else None
                route = routes[0] if routes else None
                if status == "approved" and current:
                    applicable_exception_ids.add(request["request_id"])
                business_exceptions.append(
                    {
                        "request_id": request["request_id"],
                        "run_id": request["run_id"],
                        "finding_id": request["finding_id"],
                        "rule_id": request["rule_id"],
                        "machine_verdict": request["verdict"],
                        "status": status,
                        "current": current,
                        "request_reason": request["reason_code"],
                        "scope": request["scope"],
                        "requested_at": request["requested_at"],
                        "expires_at": request["expires_at"],
                        "decision_id": (
                            decision.get("decision_id") if decision is not None else None
                        ),
                        "decision": (
                            decision.get("decision") if decision is not None else None
                        ),
                        "routed": route is not None,
                        "route": route.get("route") if route is not None else None,
                        "completion_basis": (
                            route.get("completion_basis") if route is not None else None
                        ),
                    }
                )
            invalidations = {
                event.get("invalidated_run_id"): event
                for event in self._store.lifecycle_events
                if event.get("application_id") == application_id
                and event.get("invalidated_run_id")
            }
            runs: list[dict[str, Any]] = []
            for run in self._store.runs:
                if run.get("application_id") != application_id:
                    continue
                spec = run.get("spec")
                if not isinstance(spec, dict):
                    raise RuntimeError("run history spec is unavailable")
                is_current = run is current_run
                run_id = str(run["run_id"])
                invalidation = invalidations.get(run_id, {})
                decision_ids = [
                    record["decision_id"]
                    for record in self._store.review_records
                    if record.get("record_type") == "human_decision"
                    and record.get("application_id") == application_id
                    and record.get("run_id") == run_id
                ]
                exception_ids = sorted(
                    {
                        record["exception_id"]
                        for record in self._store.review_records
                        if record.get("application_id") == application_id
                        and record.get("run_id") == run_id
                        and isinstance(record.get("exception_id"), str)
                    }
                )
                run_bytes = json.dumps(
                    run,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                runs.append(
                    {
                        "run_id": run_id,
                        "status": run["status"],
                        "authority_digest": hashlib.sha256(run_bytes).hexdigest(),
                        "current": is_current,
                        "currentness_reason": (
                            "CURRENT_CONTEXT_MATCH"
                            if is_current
                            else (
                                guard_reasons[0]
                                if run is guard_current_run
                                else invalidation.get("reason_code")
                                or (
                                    "STALE_COMPLETION_CONTEXT"
                                    if run.get("status") == "stale"
                                    else "CONTEXT_NOT_CURRENT"
                                )
                            )
                        ),
                        "cycle": spec["cycle"],
                        "lifecycle_revision": spec["lifecycle_revision"],
                        "evidence_revision": spec["evidence_revision"],
                        "evidence_snapshot_id": spec["evidence_snapshot_id"],
                        "evidence_snapshot_digest": spec["evidence_snapshot_digest"],
                        "release_id": spec["release_id"],
                        "release_digest": spec["release_digest"],
                        "checker_build": spec["checker_build"],
                        "policy_scope": spec.get("policy_scope"),
                        "activation_event_id": spec.get("activation_event_id"),
                        "active_generation": spec.get("active_generation"),
                        "candidate_id": spec.get("candidate_id"),
                        "manifest_id": spec.get("manifest_id"),
                        "manifest_digest": spec.get("manifest_digest"),
                        "validation_bundle_id": spec.get("validation_bundle_id"),
                        "validation_bundle_digest": spec.get(
                            "validation_bundle_digest"
                        ),
                        "approval_binding_id": spec.get("approval_binding_id"),
                        "approval_binding_digest": spec.get(
                            "approval_binding_digest"
                        ),
                        "components": copy.deepcopy(spec.get("components", [])),
                        "finding_ids": copy.deepcopy(run.get("finding_ids", [])),
                        "cas_mismatches": list(run.get("cas_mismatches", ())),
                        **(
                            {
                                "reconciliation": copy.deepcopy(
                                    run["reconciliation"]
                                )
                            }
                            if run.get("reconciliation") is not None
                            else {}
                        ),
                        "selected_observation_ids": sorted(
                            {
                                outcome["observation_id"]
                                for outcome in run.get("selection_outcomes", [])
                                if outcome.get("selected") is True
                                and isinstance(outcome.get("observation_id"), str)
                            }
                        ),
                        "decision_ids": decision_ids,
                        "exception_ids": exception_ids,
                        "applicable_decision_ids": decision_ids if is_current else [],
                        "applicable_exception_ids": (
                            sorted(set(exception_ids) & applicable_exception_ids)
                            if is_current
                            else []
                        ),
                        "invalidated_decision_ids": copy.deepcopy(
                            invalidation.get("invalidated_decision_ids", [])
                        ),
                        "invalidated_exception_ids": copy.deepcopy(
                            invalidation.get("invalidated_exception_ids", [])
                        ),
                    }
                )
            corrections = []
            for event in self._store.evidence_events:
                if (
                    event.get("application_id") != application_id
                    or event.get("kind") != "field_correction"
                ):
                    continue
                payload = event.get("payload")
                correction = payload.get("correction") if isinstance(payload, dict) else None
                if not isinstance(correction, dict):
                    raise RuntimeError("correction history is unavailable")
                lifecycle = [
                    item
                    for item in self._store.lifecycle_events
                    if item.get("application_id") == application_id
                    and item.get("correction_id") == correction.get("correction_id")
                ]
                if len(lifecycle) != 1:
                    raise RuntimeError("correction lifecycle history is unavailable")
                invalidation = lifecycle[0]
                corrections.append(
                    {
                        "correction_id": correction["correction_id"],
                        "superseded_observation_id": correction[
                            "supersedes_observation_id"
                        ],
                        "successor_observation_id": correction["observation_id"],
                        "document_id": correction["document_id"],
                        "document_role": correction["document_role"],
                        "field": correction["field"],
                        "source_location": copy.deepcopy(
                            correction["source_location"]
                        ),
                        "reason_code": correction["reason_code"],
                        "actor": correction["actor"],
                        "recorded_at": correction["recorded_at"],
                        "invalidated_decision_ids": copy.deepcopy(
                            invalidation.get("invalidated_decision_ids", [])
                        ),
                        "invalidated_exception_ids": copy.deepcopy(
                            invalidation.get("invalidated_exception_ids", [])
                        ),
                        "evidence_revision": event["revision"],
                    }
                )
            attachment_versions_by_id: dict[str, dict[str, Any]] = {}
            for event in self._store.evidence_events:
                if event.get("application_id") != application_id:
                    continue
                payload = event.get("payload")
                event_evidence = payload.get("evidence") if isinstance(payload, dict) else None
                if not isinstance(event_evidence, list):
                    continue
                for document in event_evidence:
                    attachment = (
                        document.get("attachment")
                        if isinstance(document, dict)
                        and document.get("document_role")
                        == self._SUPPLEMENT_TARGET_DOCUMENT_ROLE
                        else None
                    )
                    if not isinstance(attachment, dict):
                        continue
                    attachment_id = attachment.get("attachment_id")
                    if not isinstance(attachment_id, str) or not attachment_id:
                        raise RuntimeError("attachment history identity is unavailable")
                    item = {
                        "attachment_id": attachment_id,
                        "version": attachment["version"],
                        "document_id": document["document_id"],
                        "document_role": document["document_role"],
                        "supersedes_attachment_id": attachment.get(
                            "supersedes_attachment_id"
                        ),
                        "page_ids": copy.deepcopy(attachment.get("page_ids", [])),
                        "producer_result_id": attachment.get("producer_result_id"),
                        "evidence_revision": event["revision"],
                    }
                    previous = attachment_versions_by_id.get(attachment_id)
                    if previous is None:
                        attachment_versions_by_id[attachment_id] = item
                    elif {
                        key: value
                        for key, value in previous.items()
                        if key != "evidence_revision"
                    } != {
                        key: value
                        for key, value in item.items()
                        if key != "evidence_revision"
                    }:
                        raise RuntimeError("attachment history is inconsistent")
            superseded_attachment_ids = {
                item["supersedes_attachment_id"]
                for item in attachment_versions_by_id.values()
                if item["supersedes_attachment_id"] is not None
            }
            attachment_versions = sorted(
                (
                    {
                        **item,
                        "current": item["attachment_id"]
                        not in superseded_attachment_ids,
                    }
                    for item in attachment_versions_by_id.values()
                ),
                key=lambda item: (int(item["version"]), item["attachment_id"]),
            )
            # S10: preserved page-membership history.  The current ledger is a
            # projection of the current Evidence graph holding every candidate
            # claim and every accepted/unassigned decision with its explicit
            # status; correction events add the chronological successor facts.
            current_ledger_graph = self._admitted_graph(app)
            membership_ledger = (
                current_ledger_graph.get("page_memberships")
                if isinstance(current_ledger_graph, dict)
                else None
            )
            memberships = []
            if isinstance(membership_ledger, list):
                for record in membership_ledger:
                    if not isinstance(record, dict):
                        continue
                    kind = record.get("record_kind")
                    if kind == "candidate":
                        memberships.append(
                            {
                                "record_kind": kind,
                                "claim_id": record["claim_id"],
                                "page": copy.deepcopy(record["page"]),
                                "candidate_document": copy.deepcopy(
                                    record["candidate_document"]
                                ),
                                "provenance": copy.deepcopy(
                                    record.get("provenance", {})
                                ),
                            }
                        )
                    elif kind in {"accepted", "unassigned"}:
                        item: dict[str, Any] = {
                            "record_kind": kind,
                            "decision_id": record["decision_id"],
                            "membership_id": record.get("membership_id"),
                            "page": copy.deepcopy(record["page"]),
                            "actor": record["actor"],
                            "reason_code": record["reason_code"],
                            "time": record["time"],
                            "source_evidence": copy.deepcopy(
                                record.get("source_evidence", {})
                            ),
                            "supersedes": copy.deepcopy(
                                record.get("supersedes", [])
                            ),
                            "status": record["status"],
                        }
                        if kind == "accepted":
                            item["document_instance_id"] = record.get(
                                "document_instance_id"
                            )
                            item["document_role"] = record.get("document_role")
                        memberships.append(item)
            membership_history = []
            for event in sorted(
                self._store.evidence_events,
                key=lambda item: int(item.get("revision") or 0),
            ):
                if (
                    event.get("application_id") != application_id
                    or event.get("kind") != "membership_correction"
                ):
                    continue
                payload = event.get("payload")
                correction = payload.get("correction") if isinstance(payload, dict) else None
                if not isinstance(correction, dict):
                    raise RuntimeError("membership history is unavailable")
                membership_history.append(
                    {
                        "evidence_revision": event.get("revision"),
                        "event_id": event.get("event_id"),
                        **{
                            key: copy.deepcopy(correction[key])
                            for key in (
                                "correction_id",
                                "decision_id",
                                "page_source_sha256",
                                "page_ordinal",
                                "decision",
                                "document_instance_id",
                                "document_role",
                                "reason_code",
                                "actor",
                                "recorded_at",
                                "supersedes",
                            )
                            if correction.get(key) is not None
                        },
                    }
                )
            return {
                "schema_version": "s04-application-history/1",
                "application_id": application_id,
                "current_run_id": (
                    None if guard_reasons else app.get("current_run_id")
                ),
                "runs": runs,
                "corrections": corrections,
                "business_exceptions": business_exceptions,
                "attachment_versions": attachment_versions,
                "memberships": memberships,
                "membership_history": membership_history,
            }

    def _reviewer_application_authority(
        self,
        principal: S01CommandPrincipal,
        application_id: str,
    ) -> dict[str, Any]:
        if not self._valid_reviewer_principal(principal, now=float(self._clock())):
            raise QueryNotFound(application_id)
        app = self._store.applications.get(application_id)
        if app is None:
            raise QueryNotFound(application_id)
        visible_scopes = {principal.scope}
        if principal.scope.startswith(self._SESSION_SCOPE_PREFIX):
            visible_scopes.add("C-DEMO")
        if (
            self._application_visibility_scope(application_id) not in visible_scopes
            or self._application_review_assignee(application_id) != principal.subject
        ):
            raise QueryNotFound(application_id)
        self._require_application_state_authority(app)
        return app

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
        oracle_outcomes: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
        governed_active = (
            self._policy_governance is not None
            and self._policy_governance.has_governed_activation("C-DEMO/demo")
        )
        if not governed_active:
            # Legacy mode keeps the offline differential oracle for its
            # pre-cutover seam; governed runs reference the immutable
            # validation/migration bundle instead and never execute the
            # legacy engine at admission.
            try:
                oracle_report = (
                    self._legacy_oracle_runner(oracle_application)
                    if self._legacy_oracle_runner is not None
                    else self._legacy_release()["legacy_oracle"].run(oracle_application)
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
        # S10: a C-DEMO fixture may declare an application-local page-membership
        # ledger on its graph.  It is admitted verbatim (immutable candidate
        # claims plus any explicitly accepted/unassigned disposition facts) and
        # travels with the Evidence payload and revisions.
        admitted_graph = envelope.payload["application"].get("graph")
        if isinstance(admitted_graph, dict) and isinstance(
            admitted_graph.get("page_memberships"), list
        ):
            admitted_evidence["graph"] = copy.deepcopy(admitted_graph)
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
        source_path = (self.fixture_root / self._scenario_id).resolve()
        source_sha256 = (
            self._C_DEMO_PROVENANCE_SOURCE_SHA256
            if self._scenario_id == "app_r53_bad_engine.json"
            else hashlib.sha256(source_path.read_bytes()).hexdigest()
        )
        source_object_ref = (
            "c-demo-object:sha256:" + source_sha256
        )
        entries: list[dict[str, Any]] = []
        provenance_entries = (
            self._C_DEMO_MISSING_VIN_PROVENANCE_ENTRIES
            if self._scenario_id == "app_missing_vin_docs.json"
            else self._C_DEMO_PROVENANCE_ENTRIES
        )
        for item in copy.deepcopy(provenance_entries):
            if isinstance(item, (list, tuple)) and len(item) == 4:
                document_id, field, source_page, source_region = item
                entries.append(
                    {
                        "document_id": document_id,
                        "field": field,
                        "source_object_ref": source_object_ref,
                        "source_sha256": source_sha256,
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
            "scenario_id": self._scenario_id,
            "source_kind": "synthetic-json-pages/1",
            "bound_source_sha256": source_sha256,
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
        scenario_id = self._scenario_id
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
    def is_controlled_scope(cls, scope: object) -> bool:
        return cls.is_c_demo_scope(scope) or cls.is_registered_scope(scope)

    @classmethod
    def _recovery_work_is_open(
        cls, work_events: list[dict[str, Any]]
    ) -> bool:
        """True when the append-only events describe exactly one open work item."""
        return (
            sum(event.get("kind") == "opened" for event in work_events) == 1
            and not any(
                event.get("kind") in {"resolved", "superseded", "terminated"}
                for event in work_events
            )
        )

    @classmethod
    def _recovery_scope_visible(
        cls,
        principal: S01CommandPrincipal,
        visibility_scope: object,
    ) -> bool:
        return visibility_scope == principal.scope or (
            principal.role == "operator"
            and principal.scope == "C-DEMO"
            and isinstance(visibility_scope, str)
            and visibility_scope.startswith(cls._SESSION_SCOPE_PREFIX)
            and cls.is_c_demo_scope(visibility_scope)
        )

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

    @staticmethod
    def _registered_fact_counts(*, accepted: bool, attachments: int = 0, observations: int = 0) -> dict[str, int]:
        return {
            "applications": int(accepted),
            "receipts": 1,
            "idempotency_bindings": 1,
            "lifecycle_events": int(accepted),
            "evidence_events": int(accepted),
            "audit_events": 1,
            "jobs": int(accepted),
            "outbox_events": int(accepted),
            "attachments": attachments if accepted else 0,
            "pages": attachments if accepted else 0,
            "producer_results": int(accepted),
            "observations": observations if accepted else 0,
        }

    @staticmethod
    def _registered_rejected(reason_code: str) -> AdmissionResult:
        return AdmissionResult(
            disposition=AdmissionDisposition.REJECTED,
            reason_code=reason_code,
            lifecycle_revision=None,
            evidence_revision=None,
            retryable=False,
            responsible_party="integrator",
            recovery_action="repair_and_resubmit",
            fact_counts={
                "applications": 0,
                "receipts": 0,
                "idempotency_bindings": 0,
                "lifecycle_events": 0,
                "evidence_events": 0,
                "audit_events": 0,
                "jobs": 0,
                "outbox_events": 0,
                "attachments": 0,
                "pages": 0,
                "producer_results": 0,
                "observations": 0,
            },
            real_cross_document_opportunities=0,
            performance_status="not_estimable",
        )

    def _registered_failure_result(self, error: S02IntakeError) -> AdmissionResult:
        return AdmissionResult(
            disposition=AdmissionDisposition(error.disposition),
            reason_code=error.reason_code,
            lifecycle_revision=None,
            evidence_revision=None,
            retryable=error.retryable,
            responsible_party=error.responsible_party,
            recovery_action=error.recovery_action,
            gate_results=error.gate_results,
            adapter_id=error.adapter_id,
            adapter_version=error.adapter_version,
            source_registration_digest=error.registration_digest,
            fact_counts=self._registered_rejected(error.reason_code).fact_counts,
            real_cross_document_opportunities=0,
            performance_status="not_estimable",
        )

    @classmethod
    def is_registered_scope(cls, scope: object) -> bool:
        del cls
        return is_registered_scope(scope)

    @classmethod
    def _valid_registered_principal(cls, principal: S01CommandPrincipal) -> bool:
        return (
            isinstance(principal.subject, str)
            and bool(principal.subject)
            and principal.subject.strip() == principal.subject
            and principal.role == "integrator"
            and cls.is_registered_scope(principal.scope)
            and isinstance(principal.source_id, str)
            and bool(principal.source_id)
            and principal.source_id.strip() == principal.source_id
        )

    @classmethod
    def _registered_principal_live(
        cls,
        principal: S01CommandPrincipal,
        *,
        now: float,
    ) -> bool:
        """The registered Integrator identity seam for the read-only request
        projection: the principal must be a registered integrator whose
        expiry (when present) is a finite numeric timestamp strictly after
        ``now``.  Every other expiry form (boolean, string, NaN, infinity,
        expired) fails closed with the same sanitized query-not-found."""
        if not cls._valid_registered_principal(principal):
            return False
        expires_at = principal.expires_at
        if expires_at is None:
            return True
        return (
            not isinstance(expires_at, bool)
            and isinstance(expires_at, (int, float))
            and math.isfinite(float(expires_at))
            and float(expires_at) > now
        )

    @staticmethod
    def _registered_idempotency_binding_key(
        principal: S01CommandPrincipal,
        idempotency_key: str,
        workload_identity_id: str,
        *,
        action: str = "submit_observation_result",
    ) -> str:
        material = json.dumps(
            {
                "action": action,
                "key": idempotency_key,
                "role": principal.role,
                "scope": principal.scope,
                "source_id": principal.source_id,
                "subject": principal.subject,
                "workload_identity_id": workload_identity_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "s02_idempotency_" + hashlib.sha256(material).hexdigest()

    def _accepted_source_stream_receipts(
        self, envelope: S02CanonicalEnvelope
    ) -> list[AdmissionResult]:
        return [
            receipt
            for receipt in self._store.receipts.values()
            if isinstance(receipt, AdmissionResult)
            and receipt.disposition is AdmissionDisposition.ACCEPTED
            and receipt.source_registration_digest == envelope.registration_digest
            and receipt.stream_id == envelope.stream_id
            and type(receipt.source_revision) is int
        ]

    def _registered_idempotency_conflict(
        self, previous: AdmissionResult
    ) -> AdmissionResult:
        return AdmissionResult(
            disposition=AdmissionDisposition.REJECTED,
            receipt_id=previous.receipt_id,
            reason_code="intake.idempotency_conflict",
            lifecycle_revision=previous.lifecycle_revision,
            evidence_revision=previous.evidence_revision,
            envelope_version=previous.envelope_version,
            schema_version=previous.schema_version,
            semantic_version=previous.semantic_version,
            envelope_id=previous.envelope_id,
            stream_id=previous.stream_id,
            source_revision_id=previous.source_revision_id,
            envelope_fingerprint=previous.envelope_fingerprint,
            idempotency_identity=previous.idempotency_identity,
            idempotency_key_digest=previous.idempotency_key_digest,
            adapter_id=previous.adapter_id,
            adapter_version=previous.adapter_version,
            artifact_manifest_digest=previous.artifact_manifest_digest,
            responsible_party="integrator",
            recovery_action="reconcile_the_existing_idempotent_command",
            fact_counts={key: 0 for key in previous.fact_counts},
            gate_results=("identity:verified", "idempotency:conflict"),
            real_cross_document_opportunities=0,
            performance_status="not_estimable",
            source_registration_digest=previous.source_registration_digest,
            source_revision=previous.source_revision,
        )

    @staticmethod
    def _registered_replay(previous: AdmissionResult) -> AdmissionResult:
        return AdmissionResult(
            **{
                **previous.__dict__,
                "replayed": True,
                "fact_counts": {key: 0 for key in previous.fact_counts},
            }
        )

    def _record_registered_disposition(
        self,
        *,
        error: S02IntakeError,
        command_fingerprint: str,
        binding_key: str,
        principal: S01CommandPrincipal,
        submission: dict[str, Any],
        envelope: S02CanonicalEnvelope | None = None,
        command_type: str = "submit_observation_result",
        application_id: str | None = None,
        request_id: str | None = None,
    ) -> AdmissionResult:
        if not self.audit_available:
            return self._registered_rejected("intake.audit_unavailable")
        if not self.storage_available:
            return self._registered_rejected("intake.storage_unavailable")
        for _ in range(2):
            previous = self._store.idempotency.get(binding_key)
            if previous is not None:
                previous_fingerprint, previous_result = previous
                if previous_fingerprint == command_fingerprint:
                    return self._registered_replay(previous_result)
                return self._registered_idempotency_conflict(previous_result)
            receipt_id = self._stable_id(
                "receipt",
                f"registered:{error.disposition}:{binding_key}:{command_fingerprint}",
            )
            envelope_id = (
                envelope.envelope_id
                if envelope is not None
                else submission.get("envelope_id")
                if isinstance(submission.get("envelope_id"), str)
                else None
            )
            stream_id = (
                envelope.stream_id
                if envelope is not None
                else submission.get("stream_id")
                if isinstance(submission.get("stream_id"), str)
                else None
            )
            source_revision = (
                envelope.source_revision
                if envelope is not None
                else submission.get("source_revision")
                if type(submission.get("source_revision")) is int
                else None
            )
            result = AdmissionResult(
                disposition=AdmissionDisposition(error.disposition),
                receipt_id=receipt_id,
                reason_code=error.reason_code,
                lifecycle_revision=None,
                evidence_revision=None,
                audit_recorded=True,
                envelope_version=(envelope.envelope_version if envelope else None),
                schema_version=(
                    envelope.schema_version
                    if envelope
                    else str(submission.get("schema_version") or "") or None
                ),
                semantic_version=(
                    envelope.semantic_version
                    if envelope
                    else str(submission.get("semantic_version") or "") or None
                ),
                envelope_id=envelope_id,
                stream_id=stream_id,
                source_revision_id=(
                    self._stable_id(
                        "source_revision",
                        f"{stream_id}:{source_revision}:{command_fingerprint}",
                    )
                    if stream_id and source_revision is not None
                    else None
                ),
                envelope_fingerprint=(
                    envelope.fingerprint if envelope else command_fingerprint
                ),
                idempotency_identity=binding_key,
                idempotency_key_digest=hashlib.sha256(
                    binding_key.encode("utf-8")
                ).hexdigest(),
                adapter_id=(envelope.adapter_id if envelope else error.adapter_id),
                adapter_version=(
                    envelope.adapter_version if envelope else error.adapter_version
                ),
                artifact_manifest_digest=self._manifest.digest,
                retryable=error.retryable,
                responsible_party=error.responsible_party,
                recovery_action=error.recovery_action,
                fact_counts=self._registered_fact_counts(accepted=False),
                gate_results=tuple(dict.fromkeys((*error.gate_results, "idempotency:bound"))),
                real_cross_document_opportunities=0,
                performance_status="not_estimable",
                source_registration_digest=(
                    envelope.registration_digest
                    if envelope
                    else error.registration_digest
                ),
                source_revision=source_revision,
                application_id=application_id,
                request_id=request_id,
            )
            staged = copy.deepcopy(self._store)
            staged.audit_events.append(
                {
                    "event_id": self._stable_id(
                        "audit", f"registered_intake:{receipt_id}"
                    ),
                    "action": "registered_intake",
                    "subject": principal.subject,
                    "role": principal.role,
                    "scope": principal.scope,
                    "source_id": principal.source_id,
                    "application_id": application_id,
                    "request_id": request_id,
                    "receipt_id": receipt_id,
                    "result": error.disposition,
                    "reason_code": error.reason_code,
                    "command_type": command_type,
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
                return self._registered_rejected("intake.storage_unavailable")
            self._store = staged
            return result
        return self._registered_rejected("intake.storage_unavailable")

    def _admit_registered(
        self,
        envelope: S02CanonicalEnvelope,
        *,
        principal: S01CommandPrincipal,
        binding_key: str,
        command_fingerprint: str,
    ) -> AdmissionResult:
        if any(
            event.get("action") == "controlled_admission"
            and event.get("result") == "accepted"
            and event.get("scope") == principal.scope
            and isinstance(event.get("envelope"), dict)
            and event["envelope"].get("upstream_application_reference")
            == envelope.upstream_application_reference
            for event in self._store.audit_events
        ):
            error = S02IntakeError(
                "rejected",
                "intake.source_revision_conflict",
                responsible_party="integrator",
                recovery_action="use_the_existing_application_stream",
                gate_results=(
                    "identity:verified",
                    "contract:verified",
                    "object:verified",
                    "causality:failed",
                ),
                adapter_id=envelope.adapter_id,
                adapter_version=envelope.adapter_version,
                registration_digest=envelope.registration_digest,
            )
            return self._record_registered_disposition(
                error=error,
                command_fingerprint=command_fingerprint,
                binding_key=binding_key,
                principal=principal,
                submission={},
                envelope=envelope,
            )
        try:
            app_id = self._application_id_allocator()
            if (
                not isinstance(app_id, str)
                or not app_id.startswith("app_")
                or not 8 <= len(app_id) <= 100
                or app_id.strip() != app_id
                or app_id in self._store.applications
            ):
                return self._registered_rejected("intake.storage_unavailable")
        except Exception:
            return self._registered_rejected("intake.storage_unavailable")

        receipt_id = self._stable_id("receipt", envelope.fingerprint)
        job_id = self._stable_id("job", envelope.fingerprint)
        source_revision_id = self._stable_id(
            "source_revision",
            f"{envelope.stream_id}:{envelope.source_revision}:{envelope.fingerprint}",
        )
        evidence_revision = 1
        lifecycle_revision = 1
        accepted_envelope = copy.deepcopy(envelope.payload["envelope"])
        accepted_envelope.update(
            {
                "track": "R-OBSERVED",
                "fingerprint": envelope.fingerprint,
                "disposition": AdmissionDisposition.ACCEPTED.value,
            }
        )
        fact_counts = self._registered_fact_counts(
            accepted=True,
            attachments=envelope.attachment_count,
            observations=envelope.observation_count,
        )
        result = AdmissionResult(
            disposition=AdmissionDisposition.ACCEPTED,
            application_id=app_id,
            receipt_id=receipt_id,
            job_id=job_id,
            reason_code="intake.accepted",
            lifecycle_revision=lifecycle_revision,
            evidence_revision=evidence_revision,
            audit_recorded=True,
            envelope_version=envelope.envelope_version,
            schema_version=envelope.schema_version,
            semantic_version=envelope.semantic_version,
            envelope_id=envelope.envelope_id,
            stream_id=envelope.stream_id,
            source_revision_id=source_revision_id,
            envelope_fingerprint=envelope.fingerprint,
            idempotency_identity=binding_key,
            idempotency_key_digest=hashlib.sha256(
                binding_key.encode("utf-8")
            ).hexdigest(),
            received_at=str(int(self._clock())),
            received_at_status="server_observed",
            adapter_id=envelope.adapter_id,
            adapter_version=envelope.adapter_version,
            source_sha256=envelope.payload["source"]["source_result_sha256"],
            artifact_manifest_digest=self._manifest.digest,
            responsible_party="none",
            recovery_action="none",
            fact_counts=fact_counts,
            gate_results=(
                "identity:verified",
                "contract:verified",
                "object:verified",
                "causality:verified",
                "tenant_source_binding:verified",
                "idempotency:bound",
                (
                    "provenance:eligible"
                    if envelope.provenance_eligible
                    else "provenance:ineligible"
                ),
            ),
            real_cross_document_opportunities=0,
            performance_status="not_estimable",
            source_registration_digest=envelope.registration_digest,
            source_revision=envelope.source_revision,
        )
        evidence = copy.deepcopy(envelope.payload["application"]["evidence"])
        for document in evidence:
            for observation in document.get("observations", []):
                observation["source_receipt_id"] = receipt_id
        admitted_evidence = {
            "schema_version": "s02-admitted-evidence/1",
            "evidence": evidence,
            "graph": copy.deepcopy(envelope.payload["application"]["graph"]),
        }
        admitted_bytes = json.dumps(
            admitted_evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        admitted_digest = hashlib.sha256(admitted_bytes).hexdigest()
        evidence_event_id = self._stable_id(
            "evidence", f"{app_id}:admitted:{envelope.fingerprint}"
        )
        application = {
            "application_id": app_id,
            "upstream_application_reference": envelope.upstream_application_reference,
            "track": "R-OBSERVED",
            "phase": "Intake",
            "cycle": 1,
            "lifecycle_revision": lifecycle_revision,
            "evidence_revision": evidence_revision,
            "envelope": copy.deepcopy(accepted_envelope),
            "source": copy.deepcopy(envelope.payload["source"]),
            "artifact_manifest": {
                "digest": self._manifest.digest,
                "source_registration_digest": envelope.registration_digest,
            },
            "admitted_evidence_event_id": evidence_event_id,
            "legacy_oracle_outcomes": (),
            "evidence_ready": False,
            "route": "pending_check",
            "phase_history": ["Intake"],
            "current_run_id": None,
            "projection_visible": False,
            "projection_pending": False,
        }
        staged = copy.deepcopy(self._store)
        try:
            self._before_write("registered_admission.application")
            staged.applications[app_id] = application
            self._before_write("registered_admission.lifecycle_event")
            staged.lifecycle_events.append(
                {
                    "event_id": self._stable_id(
                        "lifecycle", f"{app_id}:1:{lifecycle_revision}"
                    ),
                    "application_id": app_id,
                    "revision": lifecycle_revision,
                    "phase": "Intake",
                    "cycle": 1,
                    "reason_code": "intake.accepted",
                }
            )
            self._before_write("registered_admission.evidence_event")
            staged.evidence_events.append(
                {
                    "event_id": evidence_event_id,
                    "application_id": app_id,
                    "revision": evidence_revision,
                    "kind": "admitted_snapshot",
                    "fingerprint": envelope.fingerprint,
                    "content_sha256": admitted_digest,
                    "content_bytes": len(admitted_bytes),
                    "payload": admitted_evidence,
                }
            )
            self._before_write("registered_admission.audit_event")
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
                    "reason_code": "intake.accepted",
                    "command_type": envelope.command_type,
                    "envelope_fingerprint": envelope.fingerprint,
                    "idempotency_scope": binding_key,
                    "envelope": copy.deepcopy(accepted_envelope),
                    **self._audit_time_fields(staged),
                }
            )
            self._before_write("registered_admission.job")
            staged.jobs.append(
                self._admission_job_record(job_id, app_id, envelope.fingerprint)
            )
            self._before_write("registered_admission.outbox")
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
            self._before_write("registered_admission.idempotency_binding")
            staged.idempotency[binding_key] = (command_fingerprint, result)
            self._before_write("registered_admission.receipt")
            staged.receipts[receipt_id] = result
            self._before_write("registered_admission.publish")
        except _StoreWriteFailure:
            return self._registered_rejected("intake.storage_unavailable")
        try:
            staged.persist()
        except StaleStoreRevision:
            self._reload_store()
            previous = self._store.idempotency.get(binding_key)
            if previous is not None and previous[0] == command_fingerprint:
                return self._registered_replay(previous[1])
            return self._registered_rejected("intake.storage_unavailable")
        except Exception:
            return self._registered_rejected("intake.storage_unavailable")
        self._store = staged
        return result

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
            "waiver_policy_id": manifest["waiver_policy_id"],
            "waiver_policy_digest": manifest["waiver_policy_digest"],
            "checker_build": manifest["checker_build"],
            "limits": manifest["limits"],
            "applicable_check_ids": manifest["applicable_check_ids"],
            "applicable_check_count": manifest["applicable_check_count"],
            "target_release": target_release,
            "legacy_oracle": RuleEngine(cfg),
        }

    @staticmethod
    def _select_checker_release(
        baseline: dict[str, Any], checker_build: str
    ) -> dict[str, Any]:
        if (
            not isinstance(checker_build, str)
            or not checker_build
            or len(checker_build) > 200
            or checker_build.strip() != checker_build
        ):
            raise ValueError("checker build must be a non-empty canonical value")
        target = baseline["target_release"]
        release_bytes = json.dumps(
            {
                "schema_version": "s01-target-release/6",
                "release_id": target.release_id,
                "rules_digest": target.rules_digest,
                "knowledge_digest": target.knowledge_digest,
                "normalizer_digest": target.normalizer_digest,
                "waiver_policy_id": target.waiver_policy_id,
                "waiver_policy_digest": target.waiver_policy_digest,
                "checker_build": checker_build,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        selected = replace(
            target,
            release_digest=hashlib.sha256(release_bytes).hexdigest(),
            checker_build=checker_build,
        )
        manifest = selected.public_manifest()
        return {
            **baseline,
            "digest": manifest["digest"],
            "checker_build": manifest["checker_build"],
            "target_release": selected,
        }

    def _admitted_evidence(self, app: dict[str, Any]) -> list[dict[str, Any]]:
        matching = [
            event
            for event in self._store.evidence_events
            if event.get("application_id") == app["application_id"]
            and event.get("revision") == app["evidence_revision"]
            and event.get("kind")
            in {
                "admitted_snapshot",
                "field_correction",
                "membership_correction",
                "supplement_attachment_version",
            }
        ]
        if len(matching) != 1:
            raise RuntimeError("admitted evidence authority is unavailable")
        event = matching[0]
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("schema_version") not in {
            "s01-admitted-evidence/1",
            "s02-admitted-evidence/1",
            "s04-corrected-evidence/1",
            "s06-supplement-evidence/1",
            "s10-corrected-evidence/1",
        }:
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

    def _admitted_graph(self, app: dict[str, Any]) -> dict[str, Any]:
        """Return the current admitted graph for the application's evidence
        revision.  The graph travels inside the same immutable Evidence event
        payload as the ``evidence`` list, so it shares the same append-only
        revision and snapshot machinery (S10 hard decision)."""
        matching = [
            event
            for event in self._store.evidence_events
            if event.get("application_id") == app["application_id"]
            and event.get("revision") == app["evidence_revision"]
            and event.get("kind")
            in {
                "admitted_snapshot",
                "field_correction",
                "membership_correction",
                "supplement_attachment_version",
            }
        ]
        if len(matching) != 1:
            raise RuntimeError("admitted graph authority is unavailable")
        payload = matching[0].get("payload")
        graph = payload.get("graph") if isinstance(payload, dict) else None
        if not isinstance(graph, dict):
            graph = {}
        return copy.deepcopy(graph)

    @staticmethod
    def _registrable_candidate_membership(
        *,
        record: dict[str, Any],
        application_id: str,
    ) -> dict[str, Any] | None:
        """Validate and normalize one admitted candidate page-membership claim."""
        if not isinstance(record, dict):
            return None
        page = record.get("page")
        candidate_document = record.get("candidate_document")
        provenance = record.get("provenance")
        if not isinstance(page, dict) or not isinstance(candidate_document, dict):
            return None
        source_sha256 = page.get("source_sha256")
        page_ordinal = page.get("page_ordinal")
        claim_id = record.get("claim_id")
        candidate_instance = candidate_document.get("document_instance_id")
        candidate_role = candidate_document.get("document_role")
        if (
            not isinstance(claim_id, str)
            or not claim_id
            or not isinstance(source_sha256, str)
            or len(source_sha256) != 64
            or any(character not in "0123456789abcdef" for character in source_sha256)
            or isinstance(page_ordinal, bool)
            or not isinstance(page_ordinal, int)
            or page_ordinal < 1
            or not isinstance(candidate_instance, str)
            or not candidate_instance
            or not isinstance(candidate_role, str)
            or not candidate_role
        ):
            return None
        return {
            "record_kind": "candidate",
            "claim_id": claim_id,
            "application_id": application_id,
            "page": {
                "source_sha256": source_sha256,
                "page_ordinal": page_ordinal,
            },
            "candidate_document": {
                "document_instance_id": candidate_instance,
                "document_role": candidate_role,
            },
            "provenance": copy.deepcopy(provenance or {}),
        }

    def _registrable_membership_ledger(
        self,
        graph: Any,
        *,
        application_id: str,
    ) -> list[dict[str, Any]]:
        """Build the normalized append-only ledger admitted on the graph.

        Only well-formed candidate claims are admitted; accepted/unassigned
        dispositions inside a fixture are trusted only when they point at a
        declared candidate claim of the same application boundary.  Nothing is
        inferred from candidate confidence, order or count."""
        if not isinstance(graph, dict):
            return []
        registrations = graph.get("page_memberships")
        if not isinstance(registrations, list):
            return []
        if not registrations:
            return []
        ledger: list[dict[str, Any]] = []
        candidate_pages: dict[str, int] = {}
        for record in registrations:
            if not isinstance(record, dict):
                continue
            kind = record.get("record_kind")
            if kind == "candidate":
                normalized = self._registrable_candidate_membership(
                    record=record, application_id=application_id
                )
                if normalized is None:
                    continue
                source_sha256 = normalized["page"]["source_sha256"]
                if source_sha256 in candidate_pages:
                    # One ledger page may carry many coexisting candidate
                    # claims; the denied check below is only for identical
                    # claim identity of the same page.
                    pass
                ledger.append(normalized)
            elif kind in {"accepted", "unassigned"}:
                page = record.get("page")
                source_sha256 = page.get("source_sha256") if isinstance(page, dict) else None
                if not isinstance(source_sha256, str):
                    continue
                if any(
                    item.get("record_kind") == "candidate"
                    and isinstance(item.get("page"), dict)
                    and item["page"].get("source_sha256") == source_sha256
                    for item in ledger
                ):
                    decision_id = record.get("decision_id")
                    decision_kind = kind
                    successor = {
                        "record_kind": decision_kind,
                        "decision_id": (
                            decision_id
                            if isinstance(decision_id, str) and decision_id
                            else self._stable_id(
                                "decision", f"{application_id}:{source_sha256}:{kind}"
                            )
                        ),
                        "application_id": application_id,
                        "page": copy.deepcopy(page),
                        "actor": str(record.get("actor") or "fixture-admitted"),
                        "reason_code": str(record.get("reason_code") or "MEMBERSHIP_SOURCE_VERIFIED"),
                        "time": int(record.get("time") or 0),
                        "source_evidence": copy.deepcopy(
                            record.get("source_evidence") or {}
                        ),
                        "supersedes": sorted(
                            str(item)
                            for item in (record.get("supersedes") or [])
                            if isinstance(item, str) and item
                        ),
                        "status": "active",
                    }
                    if decision_kind == "accepted":
                        successor["document_instance_id"] = str(
                            record.get("document_instance_id") or ""
                        )
                        successor["document_role"] = str(
                            record.get("document_role") or ""
                        )
                        if not successor["document_instance_id"] or not successor["document_role"]:
                            continue
                    ledger.append(successor)
        return ledger

    @staticmethod
    def _assemble_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        assembled = copy.deepcopy(evidence)
        for document in assembled:
            observations = document.get("observations")
            fields = document.get("fields")
            if not isinstance(observations, list) or not isinstance(fields, dict):
                continue
            corrected_fields = {
                observation.get("field")
                for observation in observations
                if isinstance(observation, dict)
                and observation.get("supersedes_observation_id") is not None
            }
            for field in corrected_fields:
                candidates = [
                    observation
                    for observation in observations
                    if isinstance(observation, dict)
                    and observation.get("field") == field
                    and isinstance(observation.get("observation_id"), str)
                    and observation.get("observation_id")
                ]
                identities = {item["observation_id"] for item in candidates}
                superseded = {
                    item["supersedes_observation_id"]
                    for item in candidates
                    if item.get("supersedes_observation_id") is not None
                }
                successors = [
                    item for item in candidates if item["observation_id"] not in superseded
                ]
                if (
                    len(identities) != len(candidates)
                    or not superseded.issubset(identities)
                    or len(successors) != 1
                ):
                    raise RuntimeError("evidence supersession authority is unavailable")
                fields[field] = copy.deepcopy(successors[0])
        attachment_documents: dict[str, dict[str, Any]] = {}
        superseded_attachment_ids: set[str] = set()
        for document in assembled:
            attachment = document.get("attachment")
            if not isinstance(attachment, dict):
                continue
            attachment_id = attachment.get("attachment_id")
            if (
                not isinstance(attachment_id, str)
                or not attachment_id
                or attachment_id in attachment_documents
            ):
                raise RuntimeError("attachment version authority is unavailable")
            attachment_documents[attachment_id] = document
            supersedes = attachment.get("supersedes_attachment_id")
            if supersedes is not None:
                if not isinstance(supersedes, str) or not supersedes:
                    raise RuntimeError("attachment supersession authority is invalid")
                superseded_attachment_ids.add(supersedes)
        if not superseded_attachment_ids.issubset(attachment_documents):
            raise RuntimeError("attachment predecessor authority is unavailable")
        return [
            document
            for document in assembled
            if not isinstance(document.get("attachment"), dict)
            or document["attachment"].get("attachment_id")
            not in superseded_attachment_ids
        ]

    @staticmethod
    def _effective_page_memberships(
        memberships: Any,
    ) -> dict[str, dict[str, Any]]:
        """Compute each ledger page's current effective decision from the
        append-only membership records.

        Only explicit accepted facts can select a page: an ``accepted`` record
        with status ``active`` selects one document instance and role; an
        ``unassigned`` record is an explicit disposition that keeps the page
        outside the projection.  Conflict among active accepted decisions makes
        the page ambiguous.  Presence of candidate claims without any active
        decision leaves the page unresolved.  Confidence, order, count, majority
        and last write never select a winner."""
        if not isinstance(memberships, list):
            return {}
        by_sha: dict[str, dict[str, Any]] = {}
        for record in memberships:
            if not isinstance(record, dict):
                continue
            page = record.get("page")
            source_sha = page.get("source_sha256") if isinstance(page, dict) else None
            if not isinstance(source_sha, str) or not source_sha:
                continue
            entry = by_sha.setdefault(
                source_sha, {"candidates": [], "decisions": []}
            )
            kind = record.get("record_kind")
            if kind == "candidate":
                entry["candidates"].append(record)
            elif kind in {"accepted", "unassigned"}:
                entry["decisions"].append(record)
        out: dict[str, dict[str, Any]] = {}
        for source_sha, entry in by_sha.items():
            if not entry["candidates"]:
                continue
            active = [
                decision
                for decision in entry["decisions"]
                if decision.get("status") == "active"
            ]
            accepted = [
                decision
                for decision in active
                if decision.get("record_kind") == "accepted"
            ]
            unassigned = [
                decision
                for decision in active
                if decision.get("record_kind") == "unassigned"
            ]
            if accepted:
                selections = {
                    (decision.get("document_instance_id"), decision.get("document_role"))
                    for decision in accepted
                    if isinstance(decision.get("document_instance_id"), str)
                    and isinstance(decision.get("document_role"), str)
                    and decision["document_instance_id"]
                    and decision["document_role"]
                }
                if len(selections) == 1:
                    instance_id, role = next(iter(selections))
                    out[source_sha] = {
                        "kind": "selected",
                        "document_instance_id": instance_id,
                        "document_role": role,
                    }
                else:
                    out[source_sha] = {"kind": "ambiguous"}
            elif unassigned:
                out[source_sha] = {"kind": "unassigned"}
            else:
                out[source_sha] = {"kind": "unresolved"}
        return out

    def _project_membership_evidence(
        self, app: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Project checker evidence after applying only explicit accepted
        page-membership decisions (S10).

        A page is included only when its effective decision selects one
        document instance and role; unresolved, explicitly unassigned and
        ambiguous pages stay outside the checker projection.  Pages with no
        membership ledger entry are unaffected, so non-membership fixtures keep
        their exact current evidence."""
        evidence = self._admitted_evidence(app)
        memberships = self._admitted_graph(app).get("page_memberships")
        selected = self._effective_page_memberships(memberships)
        if not selected:
            return evidence
        projected = copy.deepcopy(evidence)
        for document in projected:
            role = document.get("document_role")
            kept_observations = []
            found_change = False
            for observation in document.get("observations", []):
                if not isinstance(observation, dict):
                    kept_observations.append(observation)
                    continue
                state = selected.get(observation.get("source_sha256"))
                if state is None or (
                    state["kind"] == "selected"
                    and state["document_role"] == role
                ):
                    kept_observations.append(observation)
                    continue
                found_change = True
            if found_change:
                document["observations"] = kept_observations
                fields = document.get("fields")
                if isinstance(fields, dict):
                    for name in list(fields):
                        value = fields[name]
                        if not isinstance(value, dict):
                            continue
                        state = selected.get(value.get("source_sha256"))
                        if state is not None and not (
                            state["kind"] == "selected"
                            and state["document_role"] == role
                        ):
                            del fields[name]
        return projected

    def _membership_findings(
        self, app: dict[str, Any], run_spec: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Surface unresolved and ambiguous membership pages as mandatory
        review blockers.  Explicitly unassigned pages are not blockers (their
        disposition is decided); selected pages are not blockers.  Every
        coexisting candidate claim stays visible for the Reviewer."""
        graph = self._admitted_graph(app)
        memberships = graph.get("page_memberships") if isinstance(graph, dict) else None
        if not memberships:
            return []
        effective = self._effective_page_memberships(memberships)
        run_id = run_spec["run_id"]
        findings: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in memberships:
            if not isinstance(record, dict) or record.get("record_kind") != "candidate":
                continue
            page = record.get("page")
            source_sha = page.get("source_sha256") if isinstance(page, dict) else None
            if not isinstance(source_sha, str) or not source_sha or source_sha in seen:
                continue
            state = effective.get(source_sha)
            if state is None or state.get("kind") in {"selected", "unassigned"}:
                continue
            seen.add(source_sha)
            candidates = []
            for item in memberships:
                if (
                    not isinstance(item, dict)
                    or item.get("record_kind") != "candidate"
                    or not isinstance(item.get("page"), dict)
                    or item["page"].get("source_sha256") != source_sha
                ):
                    continue
                candidate_document = item.get("candidate_document")
                candidates.append(
                    {
                        "document_instance_id": candidate_document.get(
                            "document_instance_id"
                        ),
                        "document_role": candidate_document.get("document_role"),
                        "claim_id": item.get("claim_id"),
                        "provenance": copy.deepcopy(
                            item.get("provenance") or {}
                        ),
                    }
                )
            rule_id = (
                "MEMBERSHIP_UNRESOLVED"
                if state["kind"] == "unresolved"
                else "MEMBERSHIP_AMBIGUOUS"
            )
            finding_id = self._stable_id(
                "finding", f"{run_id}:{rule_id}:{source_sha}"
            )
            accepted = [
                decision.get("decision_id")
                for decision in memberships
                if isinstance(decision, dict)
                and decision.get("record_kind") == "accepted"
                and decision.get("status") == "active"
                and isinstance(decision.get("page"), dict)
                and decision["page"].get("source_sha256") == source_sha
            ]
            unassigned = any(
                isinstance(decision, dict)
                and decision.get("record_kind") == "unassigned"
                and decision.get("status") == "active"
                and isinstance(decision.get("page"), dict)
                and decision["page"].get("source_sha256") == source_sha
                for decision in memberships
            )
            findings.append(
                {
                    "finding_id": finding_id,
                    "application_id": app["application_id"],
                    "run_id": run_id,
                    "rule_id": rule_id,
                    "verdict": "uncertain",
                    "severity": "critical",
                    "reason_code": rule_id,
                    "mandatory": True,
                    "membership": {
                        "page_source_sha256": source_sha,
                        "page_ordinal": page.get("page_ordinal"),
                        "state": state["kind"],
                        "candidates": candidates,
                        "accepted_decision_ids": accepted,
                        "unassigned": unassigned,
                    },
                    # Membership blockers reference pages and candidate document
                    # instances, not field observations, so they carry no
                    # S01 evidence links; the coexisting candidates/provenance
                    # travel on the ``membership`` projection.
                    "evidence_links": [],
                }
            )
        return findings

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
            evidence_document = {
                "document_id": document_id,
                "document_role": str(document.get("doc_type") or ""),
                "fields": fields,
            }
            if source.get("scenario_id") == "app_missing_vin_docs.json":
                attachment_material = (
                    f"{source['source_object_ref']}:{document_id}:attachment:1"
                )
                attachment_id = "attachment_" + hashlib.sha256(
                    attachment_material.encode("utf-8")
                ).hexdigest()[:24]
                evidence_document["attachment"] = {
                    "attachment_id": attachment_id,
                    "version": 1,
                    "source_object_ref": source["source_object_ref"],
                    "source_sha256": source["source_sha256"],
                    "media_type": "application/json",
                    "producer_id": "c-demo-registered-source",
                    "producer_version": "1",
                    "page_ids": [
                        "page_"
                        + hashlib.sha256(
                            f"{attachment_id}:page:1".encode("utf-8")
                        ).hexdigest()[:24]
                    ],
                }
            evidence.append(evidence_document)
        if not evidence or any(not x["document_id"] or not x["document_role"] for x in evidence):
            raise ValueError("evidence requires document IDs and roles")
        application: dict[str, Any] = {"evidence": evidence}
        # S10: pass through an explicitly declared application-local
        # page-membership ledger.  Only a fixture that explicitly declares
        # ``page_memberships`` carries the ledger; a non-membership fixture
        # stays graph-free and the projection is a no-op for it.
        fixture_graph = payload.get("graph")
        if isinstance(fixture_graph, dict) and isinstance(
            fixture_graph.get("page_memberships"), list
        ):
            application["graph"] = copy.deepcopy(fixture_graph)
        return application

    def _claim_s07_failure_publication(
        self, worker_id: str, now: int
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str] | None:
        for _ in range(2):
            staged = copy.deepcopy(self._store)
            selected = next(
                (
                    job
                    for job in staged.jobs
                    if isinstance(job.get("failure_publication_pending"), str)
                    and job.get("status") not in {"diagnostic", "dead_lettered"}
                    and not (
                        job.get("status") == "leased"
                        and int(job.get("lease_until", 0)) > now
                    )
                    and int(job.get("retry_not_before", 0)) <= now
                ),
                None,
            )
            if selected is None:
                return None
            failure_kind = str(selected["failure_publication_pending"])
            if failure_kind not in self._S07_FAILURES:
                raise _ApplicationStateAuthorityUnavailable(
                    self._APPLICATION_STATE_FAILURE
                )
            pending_runs = [
                run
                for run in staged.runs
                if run.get("job_id") == selected.get("job_id")
                and run.get("status") == "failure_publication_pending"
            ]
            if len(pending_runs) != 1:
                raise _ApplicationStateAuthorityUnavailable(
                    self._APPLICATION_STATE_FAILURE
                )
            pending_run = pending_runs[0]
            attempts = [
                attempt
                for attempt in staged.attempts
                if attempt.get("attempt_id") == pending_run.get("attempt_id")
                and attempt.get("job_id") == selected.get("job_id")
                and isinstance(attempt.get("run_spec"), dict)
            ]
            if (
                len(attempts) != 1
                or pending_run.get("spec") != attempts[0]["run_spec"]
            ):
                raise _ApplicationStateAuthorityUnavailable(
                    self._APPLICATION_STATE_FAILURE
                )
            selected.update(
                {
                    "status": "leased",
                    "worker_id": worker_id,
                    "fence": int(selected.get("fence", 0)) + 1,
                    "lease_until": now + 30,
                    "failure_publication_attempts": int(
                        selected.get("failure_publication_attempts", 1)
                    )
                    + 1,
                }
            )
            selected.pop("retry_not_before", None)
            try:
                staged.persist()
            except StaleStoreRevision:
                self._reload_store()
                continue
            self._store = staged
            return (
                selected,
                attempts[0],
                copy.deepcopy(attempts[0]["run_spec"]),
                failure_kind,
            )
        return None

    def _claim_job(
        self, worker_id: str, now: int
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | None:
        for _ in range(2):
            staged = copy.deepcopy(self._store)
            selected = None
            for job in staged.jobs:
                if job.get("failure_publication_pending"):
                    continue
                if job.get("kind") in {
                    "business_exception_route",
                    "recovery_route",
                }:
                    continue
                if job["status"] in {
                    "complete",
                    "diagnostic",
                    "blocked",
                    "exhausted",
                    "dead_lettered",
                    "outcome_unknown",
                    "compensation_failed",
                }:
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
                "attempt_no": selected["attempt_no"],
                "started_at": now,
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
            and receipt.request_id is None
        ]
        if (
            len(accepted_receipts) != 1
            or accepted_receipts[0].artifact_manifest_digest != self._manifest.digest
        ):
            raise _PinnedReleaseUnavailable(self._PINNED_RELEASE_FAILURE)
        if (
            not isinstance(manifest, dict)
            or manifest.get("digest") != self._manifest.digest
        ):
            raise _PinnedReleaseUnavailable(self._PINNED_RELEASE_FAILURE)
        # The admission manifest binds only adapter/input provenance; the
        # policy release is resolved at RunSpec freeze time.

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
                    or not self.is_controlled_scope(event.get("scope"))
                    or not isinstance(authenticated_context, dict)
                    or authenticated_context.get("scope") != event.get("scope")
                    or event.get("envelope_fingerprint")
                    != envelope.get("fingerprint")
                ):
                    raise ValueError("accepted admission authority is invalid")
                if self.is_registered_scope(event.get("scope")) and envelope.get(
                    "track"
                ) != "R-OBSERVED":
                    raise ValueError("registered admission track is invalid")
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
            # S09 auxiliary facts (impact dispositions, invalidations, hold
            # release consumption) are not phase transitions: they must not
            # participate in the contiguous transition chain.
            transition_events = [
                event for event in lifecycle if not event.get("auxiliary")
            ]
            canonical_events: list[tuple[int, int, str]] = []
            for event in transition_events:
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
            expected_evidence_ready = current_phase not in {
                "Intake",
                "Assembly",
                "Awaiting Evidence",
                "Unprocessable",
            }
            current_lifecycle_event = max(
                transition_events, key=lambda event: int(event["revision"])
            )
            expected_route = {
                "Manual Review": "manual_review",
                "Pending Exception Approval": "pending_exception_approval",
                "Supplement": "supplement_pending",
                "Awaiting Evidence": "awaiting_evidence",
                "Unprocessable": "unprocessable",
                "Routing Determination": "routing_determination",
                "Verification Completed": (
                    "human_complete"
                    if current_lifecycle_event.get("reason_code")
                    in {"HUMAN_REVIEW_COMPLETED", "BUSINESS_EXCEPTION_COMPLETED"}
                    else "auto_complete"
                ),
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
            evidence_successors = sorted(
                (
                    event
                    for event in self._store.evidence_events
                    if event.get("application_id") == application_id
                    and event.get("kind")
                    in {
                        "field_correction",
                        "membership_correction",
                        "supplement_attachment_version",
                    }
                ),
                key=lambda event: int(event["revision"]),
            )
            graph_revisions = [
                admitted_events[0].get("revision"),
                *(event.get("revision") for event in evidence_successors),
            ]
            observed_evidence_revision = app.get("evidence_revision")
            if (
                graph_revisions != list(range(1, len(graph_revisions) + 1))
                or isinstance(observed_evidence_revision, bool)
                or not isinstance(observed_evidence_revision, int)
                or observed_evidence_revision != graph_revisions[-1]
                or any(
                    isinstance(event.get("revision"), bool)
                    or not isinstance(event.get("revision"), int)
                    or not 1 <= event["revision"] <= observed_evidence_revision
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
        same_check_gate_retry = bool(job.pop("s07_retry", False)) and app[
            "phase"
        ] == "Checking"
        if (
            app["phase"] == "Checking"
            and recovery_reason is not None
            and not same_check_gate_retry
        ):
            app["evidence_ready"] = False
            app["route"] = "pending_check"
            app["projection_visible"] = False
            app["projection_pending"] = False
            self._transition_lifecycle(app, "Assembly", recovery_reason, store=owner)
        if app["phase"] == "Intake":
            self._transition_lifecycle(
                app, "Assembly", "ADMITTED_EVIDENCE_ASSEMBLED", store=owner
            )
        if not app["evidence_ready"] and not same_check_gate_retry:
            app["evidence_ready"] = True
            self._transition_lifecycle(
                app, "Evidence Ready", "EVIDENCE_SNAPSHOT_FROZEN", store=owner
            )
        if not same_check_gate_retry:
            self._transition_lifecycle(
                app, "Checking", "CHECK_JOB_STARTED", store=owner
            )
        snapshot_payload = {
            "schema_version": "s01-evidence-snapshot/1",
            "evidence": self._assemble_evidence(
                self._project_membership_evidence(app)
            ),
        }
        snapshot_bytes = json.dumps(
            snapshot_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()
        snapshot_id = f"snapshot_sha256_{snapshot_digest}"
        policy_pin = None
        if self._policy_governance is not None:
            try:
                policy_pin = self._policy_governance.resolve_run_pin(
                    "C-DEMO/demo", int(self._clock()), store=owner
                )
            except Exception as error:
                raise _PinnedReleaseUnavailable(self._PINNED_RELEASE_FAILURE) from error
            if policy_pin is not None and self._holds_cover_application(
                policy_pin.get("hold_union") or [],
                str(app.get("application_id") or ""),
            ):
                # A Policy Safety Hold scoped to this application blocks
                # new/current RunSpec publication: automatic routing and
                # current completion fail closed until an explicit governed
                # recovery releases every covering hold in the union.
                raise _PinnedReleaseUnavailable(self._S09_HOLD_FAILURE)
        if policy_pin is not None:
            release = policy_pin["release"]
        elif self._policy_governance is not None:
            raise _PinnedReleaseUnavailable(self._POLICY_UNAVAILABLE_FAILURE)
        else:
            release = self._legacy_run_release()
        run_id_material = [
            job["job_id"],
            str(app["cycle"]),
            snapshot_id,
            release["digest"],
            release["checker_build"],
        ]
        if policy_pin is not None:
            run_id_material.extend(
                (policy_pin["manifest_digest"], str(policy_pin["active_generation"]))
            )
        run_id = self._stable_id("run", ":".join(run_id_material))
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
        run_spec: dict[str, Any] = {
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
                    for key, value in release.items()
                    if key not in {"target_release", "legacy_oracle"}
                }
            ),
            "release_id": release["release_id"],
            "release_digest": release["digest"],
            "checker_build": release["checker_build"],
            "fence": job["fence"],
            "limits": copy.deepcopy(release["limits"]),
            "applicable_check_ids": release["applicable_check_ids"],
            "applicable_check_count": release["applicable_check_count"],
        }
        if policy_pin is not None:
            run_spec.update(
                {
                    "policy_scope": policy_pin["policy_scope"],
                    "activation_event_id": policy_pin["activation_event_id"],
                    "active_generation": policy_pin["active_generation"],
                    "candidate_id": policy_pin["candidate_id"],
                    "manifest_id": policy_pin["manifest_id"],
                    "manifest_digest": policy_pin["manifest_digest"],
                    "validation_bundle_id": policy_pin["validation_bundle_id"],
                    "validation_bundle_digest": policy_pin["validation_bundle_digest"],
                    "approval_binding_id": policy_pin["approval_binding_id"],
                    "approval_binding_digest": policy_pin["approval_binding_digest"],
                    "components": copy.deepcopy(policy_pin["components"]),
                    "final_impact_digest": policy_pin.get("final_impact_digest"),
                    "holds": copy.deepcopy(
                        [
                            {
                                "hold_id": hold["hold_id"],
                                "reason_code": hold["reason_code"],
                                "hold_scope": hold["hold_scope"],
                            }
                            for hold in policy_pin.get("hold_union") or []
                        ]
                    ),
                }
            )
        return run_spec

    @staticmethod
    def _checker_application(run_spec: dict[str, Any]) -> Application:
        documents = []
        for evidence in run_spec["evidence_snapshot"]["evidence"]:
            documents.append(
                {
                    "doc_id": evidence["document_id"],
                    "doc_type": evidence["document_role"],
                    "fields": copy.deepcopy(evidence["fields"]),
                }
            )
        return Application.from_dict(
            {"application_id": run_spec["application_id"], "documents": documents}
        )

    def _run_checker(self, run_spec: dict[str, Any]):
        application = self._checker_application(run_spec)
        if self._checker_runner is not None:
            probe_result = self._checker_runner(application)
            self._convert_run_result(probe_result, run_spec)
        return self._checker_for_run(run_spec).run(run_spec)

    def _pinned_release_for(self, run_spec: dict[str, Any]) -> dict[str, Any]:
        """The complete release for a governed RunSpec pin, or the legacy
        singleton for pre-cutover runs.  S05/S07 consumers resolve the exact
        release the run pinned instead of any current singleton."""
        if self._policy_governance is not None:
            # Governed runtimes resolve only the Registry/Ledger.  A pinned
            # RunSpec loads its exact artifact; a pre-cutover RunSpec is
            # exact-mapped to the Registry checker artifact with the same
            # release identity.  The legacy singleton is never a target
            # fallback.
            if run_spec.get("activation_event_id"):
                release = self._policy_governance.load_pinned_release(run_spec)
            else:
                release = self._policy_governance.load_compat_release(run_spec)
            public = release.public_manifest()
            return {
                "release_id": public["release_id"],
                "digest": public["digest"],
                "checker_build": public["checker_build"],
                "rules_digest": public["rules_digest"],
                "knowledge_digest": public["knowledge_digest"],
                "normalizer_digest": public["normalizer_digest"],
                "waiver_policy_id": public["waiver_policy_id"],
                "waiver_policy_digest": public["waiver_policy_digest"],
                "limits": public["limits"],
                "applicable_check_ids": public["applicable_check_ids"],
                "applicable_check_count": public["applicable_check_count"],
                "target_release": release,
            }
        return self._legacy_run_release()

    def _checker_for_run(self, run_spec: dict[str, Any]) -> TargetChecker:
        """Workers execute only the RunSpec-pinned Registry checker."""
        if self._policy_governance is not None:
            try:
                if run_spec.get("activation_event_id"):
                    return self._policy_governance.load_pinned_checker(run_spec)
                return TargetChecker(
                    self._pinned_release_for(run_spec)["target_release"]
                )
            except Exception as error:
                raise _PinnedReleaseUnavailable(
                    self._PINNED_RELEASE_FAILURE
                ) from error
        return self._legacy_target_checker()

    def _reconcile_s07_checker_timeout(
        self,
        app: dict[str, Any],
        job: dict[str, Any],
        attempt: dict[str, Any],
        run_spec: dict[str, Any],
        *,
        now: int,
    ) -> WorkerResult:
        logical_operation_id = str(job["job_id"])
        query = {
            "schema_version": "s07-checker-status-query/1",
            "logical_operation_id": logical_operation_id,
            "semantic_idempotency_identity": str(job["fingerprint"]),
            "run_id": run_spec["run_id"],
            "application_id": run_spec["application_id"],
            "attempt": int(job["attempt_no"]),
            "dependency": "c-demo-target-checker",
            "release_id": run_spec["release_id"],
            "release_digest": run_spec["release_digest"],
            "checker_build": run_spec["checker_build"],
            "evidence_snapshot_id": run_spec["evidence_snapshot_id"],
            "evidence_snapshot_digest": run_spec["evidence_snapshot_digest"],
            "application": self._checker_application(run_spec),
        }
        try:
            response = (
                self._checker_status_query(copy.deepcopy(query))
                if self._checker_status_query is not None
                else None
            )
        except Exception:
            response = None

        expected_common = {"status", "logical_operation_id"}
        status = response.get("status") if isinstance(response, dict) else None
        identity_matches = (
            isinstance(response, dict)
            and response.get("logical_operation_id") == logical_operation_id
        )
        if (
            identity_matches
            and status == "not_committed"
            and set(response) == expected_common
        ):
            return self._record_s07_operation_failure(
                app,
                job,
                attempt,
                run_spec,
                failure_kind="checker_transient",
                now=now,
                reconciliation={
                    "status": "not_committed",
                    "logical_operation_id": logical_operation_id,
                },
            )

        committed_keys = {
            *expected_common,
            "result_id",
            "result_digest",
            "result",
        }
        if (
            identity_matches
            and status == "committed"
            and set(response) == committed_keys
            and isinstance(response.get("result_id"), str)
            and bool(response["result_id"])
            and response["result_id"].strip() == response["result_id"]
            and isinstance(response.get("result_digest"), str)
            and len(response["result_digest"]) == 64
            and all(
                character in "0123456789abcdef"
                for character in response["result_digest"]
            )
        ):
            try:
                run_result = self._convert_run_result(response["result"], run_spec)
                semantic_differential = self._semantic_differential(app, run_result, run_spec)
            except _InvalidRunResult:
                pass
            else:
                reconciliation = {
                    "status": "committed",
                    "logical_operation_id": logical_operation_id,
                    "result_id": response["result_id"],
                    "result_digest": response["result_digest"],
                }
                return self._commit_complete_result(
                    app,
                    job,
                    attempt,
                    run_spec,
                    run_result,
                    self._completion_context(run_spec),
                    semantic_differential,
                    now=now,
                    reconciliation=reconciliation,
                )

        return self._record_s07_operation_failure(
            app,
            job,
            attempt,
            run_spec,
            failure_kind="checker_outcome_unknown",
            now=now,
            reconciliation={
                "status": "unknown",
                "logical_operation_id": logical_operation_id,
            },
        )

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
        *,
        retry_superseded: bool = False,
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
                active_lease = (
                    current_job is not None
                    and current_job.get("status") == "leased"
                    and current_job.get("worker_id") == attempt_template.get("worker_id")
                    and current_job.get("fence") == run_spec["fence"]
                )
                superseded = current_job is not None and (
                    int(current_job.get("fence", 0)) > int(run_spec["fence"])
                    or current_job.get("status") in {"complete", "diagnostic"}
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
                    or not (active_lease or (retry_superseded and superseded))
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

    def _record_s07_operation_failure(
        self,
        app: dict[str, Any],
        job: dict[str, Any],
        attempt: dict[str, Any],
        run_spec: dict[str, Any],
        *,
        failure_kind: str,
        now: int,
        reconciliation: dict[str, Any] | None = None,
        expired_lease: bool = False,
    ) -> WorkerResult:
        failure = self._S07_FAILURES[failure_kind]
        attempt_no = int(job.get("attempt_no", 0))
        retrying = (
            failure["classification"] == "transient"
            and attempt_no < self._MAX_FAILURE_ATTEMPTS
        )
        retry_after = 2 ** (attempt_no - 1) if retrying else 0
        retry_not_before = now + retry_after if retrying else None
        related_reason_codes = list(failure["related_reason_codes"])
        if failure["classification"] == "transient" and not retrying:
            related_reason_codes.append("operation.retry_exhausted")
        failure_status = (
            "transient_failure"
            if retrying
            else "exhausted"
            if failure["classification"] == "transient"
            else "terminal_failure"
        )
        retry_policy = {
            "id": self._S07_RETRY_POLICY_ID,
            "max_attempts": 3,
            "retry_offsets_seconds": [1, 2],
            "jitter": False,
        }
        retry_policy_digest = hashlib.sha256(
            json.dumps(
                retry_policy, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        conditions = tuple(
            failure.get(
                "conditions",
                (
                    {
                        "condition_id": failure["criterion_id"],
                        "reason_code": failure["primary_reason_code"],
                    },
                ),
            )
        )
        criterion_body = {
            "id": failure["criterion_id"],
            "version": "1",
            "operation": failure["operation"],
            "dependency": failure["dependency"],
            "required_conditions": [condition["reason_code"] for condition in conditions],
            "trusted_verifier": failure["responsible_party"],
            "evidence_kind": failure["evidence_kind"],
            "conditions": copy.deepcopy(conditions),
        }
        criterion_digest = hashlib.sha256(
            json.dumps(
                criterion_body, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        recovery_work_id = self._stable_id(
            "recovery_work",
            ":".join(
                (
                    app["application_id"],
                    str(app["cycle"]),
                    job["job_id"],
                    criterion_digest,
                )
            ),
        )
        staged = copy.deepcopy(self._store)
        staged_app = staged.applications[app["application_id"]]
        staged_job = next(
            item for item in staged.jobs if item["job_id"] == job["job_id"]
        )
        staged_attempt = next(
            item
            for item in staged.attempts
            if item["attempt_id"] == attempt["attempt_id"]
        )
        reconciling_publication = isinstance(
            staged_job.get("failure_publication_pending"), str
        )
        publication_attempt_no = int(
            staged_job.get(
                "failure_publication_attempts", staged_attempt["attempt_no"]
            )
        )
        publication_started_at = (
            now if reconciling_publication else int(staged_attempt["started_at"])
        )
        run_record_identity = f"{attempt['attempt_id']}:s07:{failure_kind}"
        if reconciling_publication:
            run_record_identity += f":publication:{publication_attempt_no}"
        pre_block_revision = staged_app["lifecycle_revision"]
        failed_from_phase = staged_app["phase"]
        try:
            self._before_write("s07.failure.attempt")
            if expired_lease and not retrying:
                recorded_attempts = {
                    record.get("attempt_id") for record in staged.runs
                }
                for previous in sorted(
                    (
                        item
                        for item in staged.attempts
                        if item.get("job_id") == staged_job["job_id"]
                        and int(item.get("attempt_no", 0)) < attempt_no
                        and item.get("attempt_id") not in recorded_attempts
                    ),
                    key=lambda item: int(item["attempt_no"]),
                ):
                    previous_spec = previous.get("run_spec")
                    if not isinstance(previous_spec, dict):
                        raise _ApplicationStateAuthorityUnavailable(
                            self._APPLICATION_STATE_FAILURE
                        )
                    previous_no = int(previous["attempt_no"])
                    staged.runs.append(
                        {
                            "run_record_id": self._stable_id(
                                "run_record",
                                f"{previous['attempt_id']}:s07:lease_expired",
                            ),
                            "run_id": previous_spec["run_id"],
                            "job_id": staged_job["job_id"],
                            "attempt_id": previous["attempt_id"],
                            "application_id": app["application_id"],
                            "spec": copy.deepcopy(previous_spec),
                            "status": "transient_failure",
                            "reason_code": failure["primary_reason_code"],
                            "failure_classification": "transient",
                            "attempt_no": previous_no,
                            "started_at": previous["started_at"],
                            "retry_not_before": int(previous["started_at"])
                            + 2 ** (previous_no - 1),
                            "finding_ids": [],
                        }
                    )
            staged.runs.append(
                {
                    "run_record_id": self._stable_id(
                        "run_record", run_record_identity
                    ),
                    "run_id": run_spec["run_id"],
                    "job_id": staged_job["job_id"],
                    "attempt_id": attempt["attempt_id"],
                    "application_id": app["application_id"],
                    "spec": copy.deepcopy(run_spec),
                    "status": failure_status,
                    "reason_code": failure["primary_reason_code"],
                    "failure_classification": failure["classification"],
                    "attempt_no": publication_attempt_no,
                    "started_at": publication_started_at,
                    "retry_not_before": retry_not_before,
                    "finding_ids": [],
                    **(
                        {"reconciliation": copy.deepcopy(reconciliation)}
                        if reconciliation is not None
                        else {}
                    ),
                }
            )
            if retrying:
                self._before_write("s07.retry.job")
                staged_job.update(
                    {
                        "status": "queued",
                        "retryable": True,
                        "recovery_reason": "S07_TRANSIENT_RETRY",
                        "retry_not_before": retry_not_before,
                        "s07_retry": True,
                        "logical_operation_id": staged_job["job_id"],
                    }
                )
                staged_job.pop("failure_publication_pending", None)
                staged_job.pop("failure_publication_attempts", None)
                visibility_scope = self._application_visibility_scope(
                    staged_app["application_id"]
                )
                self._before_write("s07.retry.audit")
                staged.audit_events.append(
                    {
                        "event_id": self._stable_id(
                            "audit", f"s07_retry:{staged_job['job_id']}:{attempt_no}"
                        ),
                        "action": "protected_operation_retry_scheduled",
                        "subject": staged_job.get("worker_id")
                        or self._worker_identity,
                        "role": "worker",
                        "scope": visibility_scope,
                        "source_id": "s07-target-worker",
                        "application_id": staged_app["application_id"],
                        "job_id": staged_job["job_id"],
                        "attempt_id": staged_attempt["attempt_id"],
                        "result": "retry_wait",
                        "reason_code": failure["primary_reason_code"],
                        "failure_classification": "transient",
                        "retry_not_before": retry_not_before,
                        **self._audit_time_fields(staged),
                    }
                )
                self._before_write("s07.retry.outbox")
                staged.outbox.append(
                    {
                        "event_id": self._stable_id(
                            "outbox", f"s07_retry:{staged_job['job_id']}:{attempt_no}"
                        ),
                        "kind": "s07_retry_scheduled",
                        "application_id": staged_app["application_id"],
                        "job_id": staged_job["job_id"],
                        "attempt_no": attempt_no,
                        "retry_not_before": retry_not_before,
                        "visibility_scope": visibility_scope,
                        "status": "pending",
                    }
                )
                self._before_write("s07.retry.publish")
                staged.persist()
                self._store = staged
                return WorkerResult(
                    status="retry_wait",
                    application_id=staged_app["application_id"],
                    job_id=staged_job["job_id"],
                    attempt_id=staged_attempt["attempt_id"],
                    run_id=run_spec["run_id"],
                    reason_code=failure["primary_reason_code"],
                    lifecycle_revision=staged_app["lifecycle_revision"],
                    evidence_revision=staged_app["evidence_revision"],
                    lifecycle_phases=tuple(staged_app["phase_history"]),
                    release_id=run_spec["release_id"],
                    release_digest=run_spec["release_digest"],
                    checker_build=run_spec["checker_build"],
                    fence=run_spec["fence"],
                    evidence_snapshot_id=run_spec["evidence_snapshot_id"],
                    evidence_snapshot_digest=run_spec[
                        "evidence_snapshot_digest"
                    ],
                    retry_after_seconds=retry_after,
                    reconciliation=copy.deepcopy(reconciliation),
                )
            self._before_write("s07.failure.job")
            staged_job.update(
                {
                    "status": failure.get(
                        "job_status",
                        "exhausted" if failure_status == "exhausted" else "blocked",
                    ),
                    "retryable": False,
                    "terminal_reason_code": failure["primary_reason_code"],
                    "logical_operation_id": staged_job["job_id"],
                }
            )
            staged_job.pop("retry_not_before", None)
            staged_job.pop("failure_publication_pending", None)
            staged_job.pop("failure_publication_attempts", None)
            self._before_write("s07.failure.lifecycle")
            staged_app["evidence_ready"] = False
            staged_app["route"] = "unprocessable"
            staged_app["projection_visible"] = False
            staged_app["projection_pending"] = False
            self._transition_lifecycle(
                staged_app,
                "Unprocessable",
                failure["primary_reason_code"],
                store=staged,
            )
            staged.lifecycle_events[-1]["recovery_work_id"] = recovery_work_id
            self._before_write("s07.failure.recovery_work")
            opened = {
                "event_id": self._stable_id(
                    "recovery_event", f"{recovery_work_id}:opened"
                ),
                "kind": "opened",
                "schema_version": "recovery-work/1",
                "recovery_work_id": recovery_work_id,
                "application_id": staged_app["application_id"],
                "visibility_scope": self._application_visibility_scope(
                    staged_app["application_id"]
                ),
                "cycle": staged_app["cycle"],
                "evidence_revision": staged_app["evidence_revision"],
                "release_id": run_spec["release_id"],
                "release_digest": run_spec["release_digest"],
                "checker_build": run_spec["checker_build"],
                "pre_block_lifecycle_revision": pre_block_revision,
                "lifecycle_revision": staged_app["lifecycle_revision"],
                "failed_from_phase": failed_from_phase,
                "operation": failure["operation"],
                "logical_operation_id": staged_job["job_id"],
                "job_id": staged_job["job_id"],
                "attempt_ids": [
                    str(record["attempt_id"])
                    for record in sorted(
                        (
                            record
                            for record in staged.runs
                            if record.get("application_id")
                            == staged_app["application_id"]
                            and record.get("job_id") == staged_job["job_id"]
                            and record.get("status")
                            in {
                                "failure_publication_pending",
                                "transient_failure",
                                "terminal_failure",
                                "exhausted",
                            }
                        ),
                        key=lambda record: int(record.get("attempt_no", 0)),
                    )
                ],
                "dependency": failure["dependency"],
                "safe_correlation_id": self._stable_id(
                    "correlation", f"{staged_job['job_id']}:{staged_attempt['attempt_id']}"
                ),
                "primary_reason_code": failure["primary_reason_code"],
                "related_reason_codes": related_reason_codes,
                "retry_policy": retry_policy,
                "retry_policy_digest": retry_policy_digest,
                "outcome_known": failure.get("outcome_known", True),
                "responsible_party": failure["responsible_party"],
                "recovery_action": failure["recovery_action"],
                "recovery_target": failure["recovery_target"],
                "criterion": {**criterion_body, "digest": criterion_digest},
                "conditions": copy.deepcopy(conditions),
                **(
                    {"reconciliation": copy.deepcopy(reconciliation)}
                    if reconciliation is not None
                    else {}
                ),
                "opened_at": now,
                "idempotency_fingerprint": hashlib.sha256(
                    f"{staged_job['job_id']}:{failure_kind}".encode("utf-8")
                ).hexdigest(),
            }
            staged.recovery_events.append(opened)
            self._before_write("s07.failure.audit")
            staged.audit_events.append(
                {
                    "event_id": self._stable_id(
                        "audit", f"s07_failure:{recovery_work_id}"
                    ),
                    "action": "protected_operation_failed",
                    "subject": staged_job.get("worker_id") or self._worker_identity,
                    "role": "worker",
                    "scope": opened["visibility_scope"],
                    "source_id": "s07-target-worker",
                    "application_id": staged_app["application_id"],
                    "job_id": staged_job["job_id"],
                    "attempt_id": staged_attempt["attempt_id"],
                    "recovery_work_id": recovery_work_id,
                    "result": "blocked",
                    "reason_code": failure["primary_reason_code"],
                    "lifecycle_revision": staged_app["lifecycle_revision"],
                    **self._audit_time_fields(staged),
                }
            )
            self._before_write("s07.failure.idempotency")
            failure_binding = f"s07:failure:{staged_job['job_id']}"
            failure_fingerprint = str(opened["idempotency_fingerprint"])
            staged.idempotency[failure_binding] = (
                failure_fingerprint,
                {
                    "status": "blocked",
                    "recovery_work_id": recovery_work_id,
                    "lifecycle_revision": staged_app["lifecycle_revision"],
                },
            )
            self._before_write("s07.failure.outbox")
            staged.outbox.append(
                {
                    "event_id": self._stable_id(
                        "outbox", f"s07_recovery:{recovery_work_id}"
                    ),
                    "kind": "s07_recovery_work_opened",
                    "application_id": staged_app["application_id"],
                    "recovery_work_id": recovery_work_id,
                    "lifecycle_revision": staged_app["lifecycle_revision"],
                    "visibility_scope": opened["visibility_scope"],
                    "status": "pending",
                }
            )
            self._before_write("s07.failure.publish")
        except _StoreWriteFailure:
            publication = self._record_s07_failure_publication_stop(
                job,
                attempt,
                run_spec,
                failure_kind=failure_kind,
                now=now,
            )
            return WorkerResult(
                status=str(publication["status"]),
                application_id=app["application_id"],
                job_id=job["job_id"],
                attempt_id=attempt["attempt_id"],
                run_id=run_spec["run_id"],
                reason_code=str(publication["reason_code"]),
                lifecycle_revision=app["lifecycle_revision"],
                evidence_revision=app["evidence_revision"],
                retry_after_seconds=int(publication["retry_after_seconds"]),
                reconciliation=copy.deepcopy(publication["reconciliation"]),
            )
        staged.persist()
        self._store = staged
        return WorkerResult(
            status="blocked",
            application_id=staged_app["application_id"],
            job_id=staged_job["job_id"],
            attempt_id=staged_attempt["attempt_id"],
            run_id=run_spec["run_id"],
            reason_code=failure["primary_reason_code"],
            lifecycle_revision=staged_app["lifecycle_revision"],
            evidence_revision=staged_app["evidence_revision"],
            lifecycle_phases=tuple(staged_app["phase_history"]),
            release_id=run_spec["release_id"],
            release_digest=run_spec["release_digest"],
            checker_build=run_spec["checker_build"],
            fence=run_spec["fence"],
            evidence_snapshot_id=run_spec["evidence_snapshot_id"],
            evidence_snapshot_digest=run_spec["evidence_snapshot_digest"],
            recovery_work_id=recovery_work_id,
            reconciliation=copy.deepcopy(reconciliation),
        )

    def _record_s07_failure_publication_stop(
        self,
        job: dict[str, Any],
        attempt: dict[str, Any],
        run_spec: dict[str, Any],
        *,
        failure_kind: str,
        now: int,
    ) -> dict[str, Any]:
        staged = copy.deepcopy(self._store)
        staged_job = next(
            item for item in staged.jobs if item.get("job_id") == job["job_id"]
        )
        already_pending = isinstance(
            staged_job.get("failure_publication_pending"), str
        )
        publication_attempt = int(
            staged_job.get("failure_publication_attempts", 1)
        )
        exhausted = publication_attempt >= self._MAX_FAILURE_ATTEMPTS
        retry_after = 0 if exhausted else (1, 2)[publication_attempt - 1]
        retry_not_before = now + retry_after if retry_after else None
        staged_job.update(
            {
                "status": "diagnostic" if exhausted else "queued",
                "s07_retry": True,
                "recovery_reason": "S07_FAILURE_PUBLICATION_RECONCILE",
                "failure_publication_pending": failure_kind,
                "failure_publication_attempts": publication_attempt,
                "retryable": False,
                "logical_operation_id": staged_job["job_id"],
            }
        )
        if retry_not_before is None:
            staged_job.pop("retry_not_before", None)
        else:
            staged_job["retry_not_before"] = retry_not_before
        staged_job.pop("worker_id", None)
        staged_job.pop("lease_until", None)
        if not already_pending:
            staged.runs.append(
                {
                    "run_record_id": self._stable_id(
                        "run_record",
                        f"{attempt['attempt_id']}:failure_publication_pending",
                    ),
                    "run_id": run_spec["run_id"],
                    "job_id": job["job_id"],
                    "attempt_id": attempt["attempt_id"],
                    "application_id": job["application_id"],
                    "spec": copy.deepcopy(run_spec),
                    "status": "failure_publication_pending",
                    "reason_code": "control.failure_publication_unavailable",
                    "failure_classification": "authority_unavailable",
                    "attempt_no": publication_attempt,
                    "started_at": int(attempt["started_at"]),
                    "retry_not_before": retry_not_before,
                    "finding_ids": [],
                }
            )
        reason_code = "control.failure_publication_unavailable"
        status = "authority_unavailable"
        if exhausted:
            reason_code = self._S07_FAILURE_PUBLICATION_EXHAUSTED
            status = "stopped"
            staged_job["terminal_reason_code"] = reason_code
            requested_stop = {
                "track": "C-DEMO",
                "admission": "stopped",
                "reason_code": self._RUNTIME_STOP_REASON,
                "failure_reason_code": reason_code,
            }
            current_stop = staged.cohort_stop
            next_stop = self._runtime_stop_with_resume(
                requested_stop, current_stop
            )
            staged.cohort_stop = next_stop
            if next_stop != current_stop:
                self._append_cohort_stop_audit(
                    staged,
                    principal=S01CommandPrincipal(
                        subject=str(job.get("worker_id") or self._worker_identity),
                        role="worker",
                        scope=self._application_visibility_scope(
                            str(job["application_id"])
                        ),
                        source_id="s07-target-worker",
                    ),
                    reason_code=self._RUNTIME_STOP_REASON,
                    failure_reason_code=reason_code,
                    cohort_stop=next_stop,
                )
        try:
            staged.persist()
        except Exception:
            return {
                "status": "authority_unavailable",
                "reason_code": "control.failure_publication_unavailable",
                "retry_after_seconds": 0,
                "reconciliation": {
                    "status": "failure_publication_unavailable",
                    "logical_operation_id": job["job_id"],
                    "attempt": publication_attempt,
                    "max_attempts": self._MAX_FAILURE_ATTEMPTS,
                },
            }
        self._store = staged
        return {
            "status": status,
            "reason_code": reason_code,
            "retry_after_seconds": retry_after,
            "reconciliation": {
                "status": (
                    "failure_publication_exhausted"
                    if exhausted
                    else "failure_publication_pending"
                ),
                "logical_operation_id": job["job_id"],
                "attempt": publication_attempt,
                "max_attempts": self._MAX_FAILURE_ATTEMPTS,
            },
        }

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
        reconciliation: dict[str, Any] | None = None,
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
                    reconciliation=reconciliation,
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
        reconciliation: dict[str, Any] | None = None,
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
            "release_id": run_spec["release_id"],
            "release_digest": run_spec["release_digest"],
            "checker_build": run_spec["checker_build"],
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
        if app.get("track") == "R-OBSERVED":
            findings.append(self._r_observed_finding(app, run_spec))
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
        # S10: unresolved and ambiguous membership pages surface as mandatory
        # review blockers; explicitly unassigned and selected pages do not.
        # The membership selection is already frozen inside the Evidence
        # snapshot, and any later membership edit fails the CAS fence below.
        findings.extend(self._membership_findings(app, run_spec))
        route = self.verification_route_for_checks(run_result.checks, findings)
        has_mandatory_blocker = route == "manual_review"
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
        # S09 generation/impact/hold fence: the authoritative governance
        # generation, final-impact membership and active hold union are
        # rechecked at the commit point inside the same store snapshot.  Once
        # Governance has an active generation, every governed run -- pinned
        # or pre-cutover -- must prove the exact authoritative generation at
        # the commit point: a missing or older generation, a listed member
        # without a reconcilable disposition, or any active hold keeps the
        # old result as a non-current diagnostic only.
        s09_fence_mismatches: list[str] = []
        if str(run_spec.get("run_id") or "").startswith(
            ("s09-replay:", "s09-simulation:")
        ):
            # A diagnostic workload identity can never become current:
            # Lifecycle rejects it as a stale diagnostic even before any
            # governance comparison.
            s09_fence_mismatches.append("diagnostic_identity")
        if self._policy_governance is not None:
            authority_unavailable = False
            try:
                pin = self._policy_governance.resolve_run_pin(
                    "C-DEMO/demo", int(self._clock()), store=staged
                )
            except Exception:
                # Authority unavailable: this is not a successfully resolved
                # pre-governance state.  Fail closed -- pinned AND unpinned
                # pre-cutover runs retain the result only as a stale
                # diagnostic with a stable CAS mismatch.
                pin = None
                authority_unavailable = True
            if authority_unavailable:
                s09_fence_mismatches.append("authority_unavailable")
            else:
                authoritative_generation = (
                    int(pin["active_generation"]) if pin is not None else None
                )
                hold_active = bool(
                    pin
                    and self._holds_cover_application(
                        pin.get("hold_union") or [],
                        str(staged_app.get("application_id") or ""),
                    )
                )
                member_pending = False
                impact_integrity_failed = False
                if (
                    pin is not None
                    and pin.get("final_impact_digest")
                    and staged_app.get("application_id")
                ):
                    try:
                        manifest = self._policy_governance.load_final_impact(
                            pin["final_impact_digest"], store=staged
                        )
                    except Exception:
                        manifest = None
                    if manifest is None:
                        # Integrity fail-closed: the pinned final impact
                        # cannot be verified, so no completion may rely on
                        # "not a member" -- the result stays a stale
                        # diagnostic with a stable CAS mismatch.
                        impact_integrity_failed = True
                    else:
                        key = (
                            str(staged_app.get("application_id") or ""),
                            int(staged_app.get("cycle") or 0),
                        )
                        if any(
                            str(member.get("application_id") or "") == key[0]
                            and int(member.get("cycle") or 0) == key[1]
                            for member in manifest.get("members", [])
                        ):
                            receipt = self._impact_receipts(
                                staged, pin["final_impact_digest"]
                            ).get(key)
                            member_pending = (
                                receipt is None
                                or receipt.get("disposition") == "outstanding"
                            )
                pinned_generation = run_spec.get("active_generation")
                run_generation = (
                    int(pinned_generation)
                    if isinstance(pinned_generation, int)
                    and not isinstance(pinned_generation, bool)
                    else None
                )
                if impact_integrity_failed:
                    s09_fence_mismatches.append("impact_integrity")
                if authoritative_generation is None:
                    # No authoritative generation is resolvable: a run that
                    # claims a pin can never become current (fail closed),
                    # while a genuinely pre-cutover run stays compatible.
                    if run_generation is not None:
                        s09_fence_mismatches.append("active_generation")
                elif run_generation != authoritative_generation:
                    # Governance has an active generation: a missing or older
                    # pinned generation finishes as a non-current diagnostic.
                    s09_fence_mismatches.append("active_generation")
                if hold_active:
                    s09_fence_mismatches.append("policy_hold")
                if member_pending:
                    s09_fence_mismatches.append("impact_disposition")
        if s09_fence_mismatches:
            return self._record_stale_complete_result(
                app,
                job,
                attempt,
                run_spec,
                completion_context,
                tuple(s09_fence_mismatches),
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
                    **(
                        {"reconciliation": copy.deepcopy(reconciliation)}
                        if reconciliation is not None
                        else {}
                    ),
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
            staged_app["route"] = route
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
                        "claim_subject": None,
                        "claim_fence": 0,
                        "claim_started_at": 0,
                        "claim_expires_at": 0,
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
                    **(
                        {"reconciliation": copy.deepcopy(reconciliation)}
                        if reconciliation is not None
                        else {}
                    ),
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
                    "lifecycle_revision": staged_app["lifecycle_revision"],
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
            reconciliation=copy.deepcopy(reconciliation),
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
        self,
        app: dict[str, Any],
        run_result: _RunResult,
        run_spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if (
            self._policy_governance is not None
            and run_spec is not None
            and run_spec.get("validation_bundle_id")
        ):
            # Governed runs reference the immutable validation bundle; the
            # legacy oracle never executes on the target path.
            return {
                "oracle": "s08-validation-bundle",
                "bundle_id": run_spec["validation_bundle_id"],
                "bundle_digest": run_spec["validation_bundle_digest"],
                "checks_compared": len(run_result.checks),
                "status": "bundle_bound",
            }
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

        return self._publish_run_diagnostic(
            app,
            job,
            attempt,
            run_spec,
            stage,
            retry_superseded=True,
        )

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

    def _r_observed_finding(
        self, app: dict[str, Any], run_spec: dict[str, Any]
    ) -> dict[str, Any]:
        links: list[dict[str, Any]] = []
        for document in run_spec["evidence_snapshot"]["evidence"]:
            observations = document.get("observations", [])
            superseded = {
                observation.get("supersedes_observation_id")
                for observation in observations
                if observation.get("supersedes_observation_id") is not None
            }
            for observation in observations:
                if observation["observation_id"] in superseded:
                    continue
                links.append(
                    {
                        "document_id": document["document_id"],
                        "document_role": document["document_role"],
                        "field": observation["field"],
                        "value_state": observation["value_state"],
                        "raw_masked": (
                            "[REDACTED]"
                            if observation["value_state"] in {"present", "empty"}
                            else None
                        ),
                        "observation_id": observation["observation_id"],
                        "source_object_ref": observation.get("source_object_ref"),
                        "source_sha256": observation.get("source_sha256"),
                        "provenance_manifest_digest": observation.get(
                            "provenance_manifest_digest"
                        ),
                        "source_page": observation.get("source_page"),
                        "source_region": observation.get("source_region"),
                        "coordinate_system": copy.deepcopy(
                            observation.get("coordinate_system")
                        ),
                        "producer_id": observation.get("producer_id"),
                        "producer_family": observation.get("producer_family"),
                        "producer_run_id": observation.get("producer_run_id"),
                        "model_id": observation.get("model_id"),
                        "model_version": observation.get("model_version"),
                        "source_receipt_id": observation.get("source_receipt_id"),
                        "evidence_eligible": observation.get("evidence_eligible")
                        is True,
                        "eligibility_reason": observation.get(
                            "eligibility_reason"
                        ),
                    }
                )
        return {
            "finding_id": self._stable_id(
                "finding", f"{run_spec['run_id']}:R-OBSERVED"
            ),
            "application_id": app["application_id"],
            "run_id": run_spec["run_id"],
            "rule_id": "R-OBSERVED",
            "verdict": "uncertain",
            "severity": "major",
            "reason_code": "R_OBSERVED_PROVENANCE_REVIEW",
            "mandatory": True,
            "evidence_links": links,
        }

    @staticmethod
    def _finding_projection(
        finding: dict[str, Any],
        region_identities: dict[tuple[Any, Any], list[Any]],
    ) -> dict[str, Any]:
        trace_keys = (
            "source_page",
            "source_region",
            "producer_id",
            "producer_family",
            "producer_run_id",
            "model_id",
            "model_version",
            "source_receipt_id",
        )
        links = []
        for link in finding["evidence_links"]:
            projected = {
                key: copy.deepcopy(link[key])
                for key in (
                    "document_id",
                    "document_role",
                    "field",
                    "value_state",
                    "raw_masked",
                    "observation_id",
                    "source_sha256",
                    "provenance_manifest_digest",
                    "evidence_eligible",
                    "eligibility_reason",
                )
            }
            projected.update(
                {
                    key: copy.deepcopy(link[key])
                    for key in trace_keys
                    if key != "source_region" and link.get(key) is not None
                }
            )
            raw_region = link.get("source_region")
            if raw_region is not None:
                scope = (link.get("source_sha256"), link.get("source_page"))
                regions = region_identities.setdefault(scope, [])
                if raw_region not in regions:
                    regions.append(raw_region)
                projected["source_region"] = (
                    f"region:{regions.index(raw_region) + 1}"
                )
            links.append(projected)
        projection: dict[str, Any] = {
            "finding_id": finding["finding_id"],
            "run_id": finding["run_id"],
            "rule_id": finding["rule_id"],
            "verdict": finding["verdict"],
            "severity": finding["severity"],
            "reason_code": finding["reason_code"],
            "mandatory": finding["mandatory"],
            "evidence_links": links,
        }
        # S10: membership blockers carry their coexisting candidate/provenance
        # facts (never any inferred selection) so the Reviewer can decide.
        if isinstance(finding.get("membership"), dict):
            projection["membership"] = copy.deepcopy(finding["membership"])
        return projection

    def _mandatory_blocker_projections(
        self, application_id: str, run_id: str
    ) -> list[dict[str, Any]]:
        findings = [
            finding
            for finding in self._store.findings
            if finding.get("application_id") == application_id
            and finding.get("run_id") == run_id
            and finding.get("mandatory") is True
            and finding.get("verdict") != "consistent"
        ]
        region_identities: dict[tuple[Any, Any], list[Any]] = {}
        for finding in findings:
            for link in finding["evidence_links"]:
                raw_region = link.get("source_region")
                if raw_region is None:
                    continue
                scope = (link.get("source_sha256"), link.get("source_page"))
                regions = region_identities.setdefault(scope, [])
                if raw_region not in regions:
                    regions.append(raw_region)
        for regions in region_identities.values():
            regions.sort(
                key=lambda region: json.dumps(
                    region,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        return [
            self._finding_projection(finding, region_identities)
            for finding in findings
        ]

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
            key: (
                fingerprint,
                self._admission_from_payload(result)
                if isinstance(result, AdmissionResult)
                or isinstance(result, dict)
                and "disposition" in result
                else result,
            )
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
                and receipt.request_id is None
            ]
            accepted_applications = {
                str(receipt.application_id): receipt
                for receipt in accepted_receipts
                if isinstance(receipt.application_id, str)
            }
            if len(accepted_applications) != len(accepted_receipts):
                recovery_failure = True
            supplement_receipts = [
                receipt
                for receipt in self._store.receipts.values()
                if isinstance(receipt, AdmissionResult)
                and receipt.disposition is AdmissionDisposition.ACCEPTED
                and receipt.request_status == "fulfilled"
                and isinstance(receipt.request_id, str)
                and receipt.request_id
                and isinstance(receipt.job_id, str)
                and receipt.job_id
            ]
            supplement_receipts_by_job = {
                str(receipt.job_id): receipt for receipt in supplement_receipts
            }
            if len(supplement_receipts_by_job) != len(supplement_receipts):
                recovery_failure = True
            correction_events_by_job: dict[str, dict[str, Any]] = {}
            for event in self._store.audit_events:
                if (
                    event.get("action")
                    not in {"evidence_correction", "page_membership_corrected"}
                    or event.get("result") != "accepted"
                ):
                    continue
                job_id = event.get("job_id")
                application_id = event.get("application_id")
                correction_id = event.get("correction_id")
                if not all(
                    isinstance(value, str) and value
                    for value in (job_id, application_id, correction_id)
                ):
                    recovery_failure = True
                    break
                previous = correction_events_by_job.get(job_id)
                if previous is not None and previous != event:
                    recovery_failure = True
                    break
                correction_events_by_job[job_id] = event

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
                for job_id, receipt in supplement_receipts_by_job.items():
                    application_id = receipt.application_id
                    event = events_by_job.get(job_id)
                    app = (
                        self._store.applications.get(application_id)
                        if isinstance(application_id, str)
                        else None
                    )
                    if not self._supplement_job_authority_matches(
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
                    unprocessed = self._supplement_is_unprocessed(app, receipt)
                    canonical_job = self._supplement_job_record(
                        job_id,
                        str(application_id),
                        str(receipt.request_id),
                        str(receipt.envelope_fingerprint),
                    )
                    if jobs:
                        job = jobs[0]
                        if not self._job_matches_supplement(job, app, receipt):
                            recovery_failure = True
                            break
                        if (
                            unprocessed
                            and job != canonical_job
                            and not self._pristine_leased_job(job)
                        ):
                            repair_jobs.append(canonical_job)
                        continue
                    if not unprocessed:
                        recovery_failure = True
                        break
                    repair_jobs.append(canonical_job)

            if not recovery_failure:
                known_application_ids = set(accepted_applications)
                known_job_ids = {
                    str(receipt.job_id) for receipt in accepted_receipts
                } | set(supplement_receipts_by_job) | set(correction_events_by_job)
                for event in request_events:
                    application_id = event.get("application_id")
                    correction_event = correction_events_by_job.get(
                        str(event.get("job_id"))
                    )
                    if (
                        not isinstance(application_id, str)
                        or application_id not in known_application_ids
                        or event.get("job_id") not in known_job_ids
                        or correction_event is not None
                        and (
                            correction_event.get("application_id") != application_id
                            or correction_event.get("correction_id")
                            != event.get("fingerprint")
                            or event.get("request_id") is not None
                        )
                    ):
                        recovery_failure = True
                        break

            if not recovery_failure:
                recovery_gate_events = [
                    event
                    for event in self._store.outbox
                    if event.get("kind") == "s07_recovery_gate_requested"
                ]
                events_by_successor: dict[str, dict[str, Any]] = {}
                for event in recovery_gate_events:
                    frozen_job = event.get("job")
                    job_id = (
                        frozen_job.get("job_id")
                        if isinstance(frozen_job, dict)
                        else None
                    )
                    if (
                        not isinstance(job_id, str)
                        or not job_id
                        or job_id in events_by_successor
                    ):
                        recovery_failure = True
                        break
                    events_by_successor[job_id] = event

                for job_id, event in events_by_successor.items():
                    frozen_job = event["job"]
                    jobs = [
                        job
                        for job in self._store.jobs
                        if job.get("job_id") == job_id
                    ]
                    if len(jobs) > 1:
                        recovery_failure = True
                        break
                    if jobs:
                        job = jobs[0]
                        if job.get("status") in {
                            "complete",
                            "diagnostic",
                            "blocked",
                            "exhausted",
                            "dead_lettered",
                            "outcome_unknown",
                            "compensation_failed",
                        }:
                            continue
                        recovery_attempts = [
                            record
                            for record in self._store.attempts
                            if record.get("job_id") == job_id
                        ]
                        if not recovery_attempts and job != frozen_job:
                            recovery_failure = True
                            break
                        if recovery_attempts:
                            latest_attempt = max(
                                recovery_attempts,
                                key=lambda record: int(record.get("attempt_no", 0)),
                            )
                            if any(
                                job.get(key) != latest_attempt.get(key)
                                for key in ("attempt_no", "fence")
                            ):
                                recovery_failure = True
                                break
                        if (
                            any(
                                job.get(key) != frozen_job.get(key)
                                for key in (
                                    "job_id",
                                    "application_id",
                                    "kind",
                                    "fingerprint",
                                    "logical_operation_id",
                                    "recovery_work_id",
                                )
                            )
                            or isinstance(job.get("fence"), bool)
                            or not isinstance(job.get("fence"), int)
                            or job["fence"] < frozen_job.get("fence", 0)
                        ):
                            recovery_failure = True
                            break
                        continue

                    application_id = event.get("application_id")
                    recovery_work_id = event.get("recovery_work_id")
                    recovery_fact_id = event.get("recovery_fact_id")
                    lifecycle_revision = event.get("lifecycle_revision")
                    app = (
                        self._store.applications.get(application_id)
                        if isinstance(application_id, str)
                        else None
                    )
                    if (
                        not isinstance(app, dict)
                        or app.get("lifecycle_revision") != lifecycle_revision
                        or any(
                            record.get("job_id") == job_id
                            for record in (*self._store.attempts, *self._store.runs)
                        )
                    ):
                        continue
                    work_events = [
                        item
                        for item in self._store.recovery_events
                        if item.get("recovery_work_id") == recovery_work_id
                    ]
                    opened = [
                        item for item in work_events if item.get("kind") == "opened"
                    ]
                    facts = [
                        item for item in work_events if item.get("kind") == "fact"
                    ]
                    resolved = [
                        item for item in work_events if item.get("kind") == "resolved"
                    ]
                    if any(
                        item.get("kind") in {"terminated", "superseded"}
                        for item in work_events
                    ):
                        continue
                    lifecycle = [
                        item
                        for item in self._store.lifecycle_events
                        if item.get("application_id") == application_id
                        and item.get("revision") == lifecycle_revision
                    ]
                    if not (
                        len(opened) == len(facts) == len(resolved) == len(lifecycle) == 1
                    ):
                        continue
                    work = opened[0]
                    fact = facts[0]
                    resolution = resolved[0]
                    lifecycle_event = lifecycle[0]
                    criterion = work.get("criterion")
                    criterion_digest = (
                        criterion.get("digest")
                        if isinstance(criterion, dict)
                        else None
                    )
                    target = work.get("recovery_target")
                    expected_kind = (
                        "recovery_check"
                        if target in {"Assembly", "Evidence Ready"}
                        else "recovery_route"
                        if target == "Routing Determination"
                        else None
                    )
                    expected_job = {
                        "job_id": job_id,
                        "application_id": application_id,
                        "kind": expected_kind,
                        "status": "queued",
                        "fingerprint": criterion_digest,
                        "logical_operation_id": job_id,
                        "recovery_work_id": recovery_work_id,
                        "fence": frozen_job.get("fence"),
                        "attempt_no": 0,
                    }
                    if (
                        frozen_job != expected_job
                        or event.get("status") not in {"pending", "published"}
                        or event.get("visibility_scope")
                        != work.get("visibility_scope")
                        or work.get("application_id") != application_id
                        or fact.get("application_id") != application_id
                        or resolution.get("application_id") != application_id
                        or fact.get("recovery_fact_id") != recovery_fact_id
                        or resolution.get("recovery_fact_id") != recovery_fact_id
                        or fact.get("criterion_digest") != criterion_digest
                        or resolution.get("recovery_target") != target
                        or lifecycle_event.get("phase") != target
                        or lifecycle_event.get("reason_code")
                        != "VERIFIED_RECOVERY_FACT_ACCEPTED"
                        or lifecycle_event.get("recovery_work_id")
                        != recovery_work_id
                        or lifecycle_event.get("recovery_fact_id")
                        != recovery_fact_id
                        or app.get("phase") != target
                    ):
                        continue
                    repair_jobs.append(copy.deepcopy(frozen_job))

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

    def _supplement_job_authority_matches(
        self,
        app: dict[str, Any] | None,
        receipt: AdmissionResult,
        event: dict[str, Any] | None,
    ) -> bool:
        if (
            not isinstance(app, dict)
            or not isinstance(event, dict)
            or event.get("status") != "pending"
            or event.get("application_id") != receipt.application_id
            or event.get("job_id") != receipt.job_id
            or event.get("request_id") != receipt.request_id
            or event.get("fingerprint") != receipt.envelope_fingerprint
        ):
            return False
        fulfillment = [
            record
            for record in self._store.review_records
            if record.get("record_type")
            in {"supplement_request_fulfilled", "supplement_recovery_successor"}
            and record.get("application_id") == receipt.application_id
            and record.get("request_id") == receipt.request_id
            and record.get("receipt_id") == receipt.receipt_id
            and record.get("envelope_fingerprint") == receipt.envelope_fingerprint
            and record.get("lifecycle_revision") == receipt.lifecycle_revision
            and record.get("evidence_revision") == receipt.evidence_revision
        ]
        lifecycle = [
            item
            for item in self._store.lifecycle_events
            if item.get("application_id") == receipt.application_id
            and item.get("revision") == receipt.lifecycle_revision
            and item.get("reason_code")
            in {
                "SUPPLEMENT_REQUEST_FULFILLED",
                "RECOVERY_CONTEXT_SUCCESSOR_ACCEPTED",
            }
            and item.get("request_id") == receipt.request_id
            and item.get("job_id") == receipt.job_id
        ]
        return len(fulfillment) == 1 and len(lifecycle) == 1

    @staticmethod
    def _job_matches_supplement(
        job: dict[str, Any], app: dict[str, Any] | None, receipt: AdmissionResult
    ) -> bool:
        return (
            isinstance(app, dict)
            and job.get("application_id") == app.get("application_id")
            and job.get("kind") == "supplement_check"
            and job.get("request_id") == receipt.request_id
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

    @classmethod
    def _supplement_job_record(
        cls,
        job_id: str,
        application_id: str,
        request_id: str,
        fingerprint: str,
    ) -> dict[str, Any]:
        return {
            **cls._admission_job_record(job_id, application_id, fingerprint),
            "kind": "supplement_check",
            "request_id": request_id,
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

    def _supplement_is_unprocessed(
        self,
        app: dict[str, Any] | None,
        receipt: AdmissionResult,
    ) -> bool:
        if (
            not isinstance(app, dict)
            or app.get("application_id") != receipt.application_id
            or app.get("phase") != "Assembly"
            or app.get("route") != "pending_check"
            or app.get("lifecycle_revision") != receipt.lifecycle_revision
            or app.get("evidence_revision") != receipt.evidence_revision
            or app.get("evidence_ready") is not False
            or app.get("current_run_id") is not None
            or app.get("projection_pending") is not False
            or app.get("projection_visible") is not False
        ):
            return False
        return not any(
            record.get("job_id") == receipt.job_id
            for record in (*self._store.attempts, *self._store.runs)
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
        operation_fault: str | None = None,
    ) -> WorkerResult:
        return self._service._process_next_job(
            worker_id=worker_id,
            now=now,
            crash=crash,
            partial=partial,
            stale=stale,
            cas_fault=cas_fault,
            duplicate=duplicate,
            operation_fault=operation_fault,
        )
