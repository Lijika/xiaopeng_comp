"""S12 isolated evaluation HTTP adapter (Ticket #28 R1).

The typed operator surface for the S12 evaluation plane: freeze a plan from
stable references, start/cancel/process/query a durable job, rerun a
published job, and query an immutable content-addressed bundle.  The module
owns no process-level authority construction: ``web/app.py`` builds
``S12_SERVICE`` (with concrete S01/S08/label providers and the registered
server worker identity) and registers the router through
``register_router``.  Missing S12 configuration leaves S01-S11 startup and
routes available while every S12 route reports scoped unavailability
(``S12_UNAVAILABLE``).

DTOs are closed and nested (``extra="forbid"`` with string/digest/literal/
count/duration constraints at the HTTP seam): generated clients can never
invent a field the authority will reject, and the caller can never claim an
environment, run spec, checker artifact, gold label or worker identity.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from task4_consistency.controlled.s12 import (
    EvaluationService,
    S12IntegrityError,
    S12Unavailable,
)

s12_router = APIRouter()

# The private FastAPI ``app.state`` key holding the application module bound
# by ``register_router``; requests resolve their authority per app.
_S12_APP_MODULE_STATE_KEY = "_s12_application_module"

_DIGEST64 = r"^[0-9a-f]{64}$"


def _s12_app_module(request: Request) -> Any:
    module = getattr(request.app.state, _S12_APP_MODULE_STATE_KEY, None)
    if module is None:
        raise HTTPException(
            503,
            detail={
                "error": "S12_UNAVAILABLE",
                "message": "Controlled S12 evaluation plane is unavailable",
            },
        )
    return module


def register_router(app: FastAPI, app_module: Any) -> None:
    """Bind the live application module on the app itself and register the
    S12 router.  Idempotent for the same app and module; a conflicting
    module for an already registered app is rejected before any state or
    route change.  Called once by ``web.app``."""
    existing = getattr(app.state, _S12_APP_MODULE_STATE_KEY, None)
    if existing is not None:
        if existing is not app_module:
            raise RuntimeError(
                "S12 router already registered with a different application module"
            )
        return
    setattr(app.state, _S12_APP_MODULE_STATE_KEY, app_module)
    app.include_router(s12_router)


def _s12_service(request: Request) -> EvaluationService:
    module = _s12_app_module(request)
    if module.S12_SERVICE is None:
        raise HTTPException(
            503,
            detail={
                "error": "S12_UNAVAILABLE",
                "message": "Controlled S12 evaluation plane is unavailable",
            },
        )
    return module.S12_SERVICE


def _s12_require_operator(request: Request) -> None:
    module = _s12_app_module(request)
    if module.S12_SERVICE is None:
        raise HTTPException(
            503,
            detail={
                "error": "S12_UNAVAILABLE",
                "message": "Controlled S12 evaluation plane is unavailable",
            },
        )
    if (
        not module.S12_SUBJECT
        or not module._s01_has_credential(request, module.S12_CREDENTIAL)
    ):
        raise HTTPException(
            403,
            detail={
                "error": "S12_FORBIDDEN",
                "message": "Registered S12 operator identity required",
            },
        )


def _s12_invalid_command() -> HTTPException:
    return HTTPException(
        422,
        detail={
            "error": "S12_INVALID_COMMAND",
            "message": "S12 command does not match the registered contract",
        },
    )


def _s12_not_found() -> HTTPException:
    return HTTPException(
        404,
        detail={
            "error": "S12_NOT_FOUND",
            "message": "S12 plan, job or bundle does not exist",
        },
    )


def _s12_closed_failure() -> HTTPException:
    """The closed envelope for authority-integrity failures: the evaluation
    store cannot be proven intact, so the plane fails closed exactly like the
    S08 authority-unavailable mapping -- never a raw 500."""
    return HTTPException(
        503,
        detail={
            "error": "S12_UNAVAILABLE",
            "message": "Controlled S12 evaluation plane is unavailable",
        },
    )


# ---------------------------------------------------------------------------
# Closed nested typed DTOs
# ---------------------------------------------------------------------------


class S12Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_opportunities: int = Field(gt=0)
    max_runtime_ms: int = Field(gt=0)


class S12Split(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheme: str
    usage_partitions: list[
        Literal["development", "calibration", "acceptance_holdout"]
    ]


class S12Cluster(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster_id: str
    stratum: str
    applications: list[str] = Field(min_length=1)
    usage: Literal["development", "calibration", "acceptance_holdout"]
    variants: list[str] | None = None


class S12Opportunity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opportunity_id: str
    track: Literal["R", "C"]
    cluster: str
    application_id: str
    cycle: int = Field(ge=1)
    check_id: str
    target_scope: Literal["C", "R-E2E", "R-T4-conditional"]
    evidence_snapshot_id: str
    variant_id: str | None = None
    difficulty: str | None = None
    data_source: str | None = None
    document_combination: str | None = None
    perturbation_family: str | None = None


class S12Membership(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opportunities: list[str]


class S12TrackMemberships(BaseModel):
    model_config = ConfigDict(extra="forbid")

    R: S12Membership
    C: S12Membership


class S12ViewMemberships(BaseModel):
    model_config = ConfigDict(extra="forbid")

    R_E2E: S12Membership = Field(alias="R-E2E")
    R_T4_conditional: S12Membership = Field(alias="R-T4-conditional")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class S12TrackStatistics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    R: "S12StatisticsBlock"
    C: "S12StatisticsBlock"


class S12ViewStatistics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    R_E2E: "S12StatisticsBlock" = Field(alias="R-E2E")
    R_T4_conditional: "S12StatisticsBlock" = Field(alias="R-T4-conditional")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class S12ReleaseReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_id: str
    release_digest: str = Field(pattern=_DIGEST64)


class S12LabelManifestReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_id: str
    manifest_digest: str = Field(pattern=_DIGEST64)


class S12EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    snapshot_id: str
    snapshot_digest: str = Field(pattern=_DIGEST64)
    cycle: int = Field(ge=1)


class S12MandatoryFamily(BaseModel):
    model_config = ConfigDict(extra="forbid")

    family_id: str
    check_ids: list[str] = Field(min_length=1)


class S12Exclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item: str
    reason: str
    reference_sha256: str = Field(pattern=_DIGEST64)


class S12Cohort(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exclusions: list[S12Exclusion]


class S12FreezePlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["s12-plan-command/1"]
    plan_id: str
    scope_declared: Literal["C", "R-E2E", "R-T4-conditional"]
    seed: int = Field(ge=0)
    budget: S12Budget
    stop_rule: Literal["plan-exhausted", "budget-or-plan"]
    split: S12Split
    clusters: list[S12Cluster] = Field(min_length=1)
    tracks: S12TrackMemberships
    views: S12ViewMemberships
    opportunities: list[S12Opportunity] = Field(min_length=1)
    evidence_references: list[S12EvidenceReference] = Field(min_length=1)
    release_reference: S12ReleaseReference
    label_manifest: S12LabelManifestReference
    mandatory_check_families: list[S12MandatoryFamily] = Field(min_length=1)
    cohort: S12Cohort | None = None


class S12ClusterResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster_id: str
    stratum: str
    applications: list[str]
    usage: str
    variants: list[str] | None = None


class S12OpportunityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opportunity_id: str
    track: str
    cluster: str
    application_id: str
    cycle: int
    check_id: str
    target_scope: Literal["C", "R-E2E", "R-T4-conditional"]
    evidence_snapshot_id: str
    variant_id: str | None = None
    label: str
    label_custody: str | None = None
    run_id: str
    difficulty: str | None = None
    data_source: str | None = None
    document_combination: str | None = None
    perturbation_family: str | None = None


class S12LabelManifestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["s12-label-manifest/1"]
    manifest_id: str
    manifest_digest: str
    label_custody: str | None = None
    labels: dict[
        str,
        Literal["consistent", "inconsistent", "indeterminate", "not_applicable"],
    ] = Field(default_factory=dict)


class S12Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_documents: int
    max_findings: int
    max_runtime_ms: int


class S12PublicRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_id: str
    digest: str
    checker_build: str
    rules_digest: str
    knowledge_digest: str
    normalizer_digest: str
    waiver_policy_id: str
    waiver_policy_digest: str
    limits: S12Limits
    applicable_check_ids: list[str]
    applicable_check_count: int


class S12CheckerRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    rule_type: str
    field: str | None = None
    document_roles: list[str]
    on_missing: str
    severity: str
    threshold: float
    uncertain_band: float
    abs_tol: float
    rel_tol: float
    list_field: str | None = None
    item_field: str | None = None
    if_field_present: str | None = None
    required_field: str | None = None
    min_confidence: float
    require_all_docs: bool
    transfer_name_policy: str | None = None
    transfer_old_roles: list[str]
    transfer_new_roles: list[str]
    waivable: bool
    waiver_policy_id: str | None = None
    waiver_policy_digest: str | None = None
    waiver_reasons: list[str]
    waiver_scope: str | None = None
    waiver_ttl_seconds: int


class S12CheckerArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    release_id: str
    rules_digest: str
    knowledge_digest: str
    normalizer_digest: str
    waiver_policy_id: str
    waiver_policy_digest: str
    checker_build: str
    knowledge: list[tuple[str, list[tuple[str, str]]]]
    aliases: list[tuple[str, list[str]]]
    field_types: list[tuple[str, str]]
    rules: list[S12CheckerRule]
    low_confidence_threshold: float
    critical_low_conf_compare: bool
    date_order: str | None = None
    vin_fix_ioq: bool
    vin_strict_check_digit: bool
    expand_id15_to_18: bool
    limits: list[tuple[str, int]]


class S12EvidenceField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw: str | None
    confidence: float
    observation_id: str
    source_object_ref: str | None = None
    source_sha256: str | None = None
    provenance_manifest_digest: str | None = None
    source_page: int | None = None
    source_region: str | None = None
    evidence_eligible: bool
    eligibility_reason: str
    producer_id: str | None = None
    producer_version: str | None = None


class S12EvidenceDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    document_role: str
    fields: dict[str, S12EvidenceField]


class S12ConfidenceSemantics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum: float
    maximum: float
    higher_is: str
    meaning: str
    granularity: str
    calibration: str


class S12CoordinateSystem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    origin: str
    unit: str


class S12RegisteredObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_status: Literal["observed", "not_detected"]
    confidence: float | None
    confidence_semantics: S12ConfidenceSemantics | None
    coordinate_system: S12CoordinateSystem | None
    eligibility_reason: str
    evidence_eligible: bool
    field: str
    model_id: str | None
    model_version: str | None
    observation_id: str
    producer_family: str
    producer_id: str | None
    producer_run_id: str | None
    producer_task_id: str | None
    producer_task_version: str | None
    provenance_manifest_digest: str = Field(pattern=_DIGEST64)
    raw: str | None
    raw_lexeme: str
    raw_type: Literal["null", "string"]
    source_object_ref: str
    source_page: int
    source_pointer: str
    source_receipt_id: str
    source_region: str | None
    source_result_object_ref: str
    source_result_sha256: str = Field(pattern=_DIGEST64)
    source_sha256: str = Field(pattern=_DIGEST64)
    value_state: Literal["explicit_null", "empty", "present"]


class S12RegisteredEvidenceDocument(S12EvidenceDocument):
    document_type: str
    observations: list[S12RegisteredObservation]


class S12EvidenceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    evidence: list[S12EvidenceDocument | S12RegisteredEvidenceDocument]


class S12RunSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    application_id: str
    cycle: int
    lifecycle_revision: int
    evidence_snapshot_id: str
    evidence_snapshot_digest: str
    evidence_snapshot: S12EvidenceSnapshot
    evidence_revision: int
    check_id: str
    target_scope: Literal["C", "R-E2E", "R-T4-conditional"]
    variant_id: str | None = None
    evidence_readiness_policy: str
    baseline_release: S12PublicRelease
    release_id: str
    release_digest: str
    checker_build: str
    fence: int
    limits: S12Limits
    applicable_check_ids: list[str]
    applicable_check_count: int


class S12EnvironmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    python: str
    evaluator_build: str
    dependency_identity: str
    schema_version: str


class S12ReleaseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_id: str
    release_digest: str
    checker_build: str
    manifest_id: str
    manifest_digest: str
    protected_baseline_digest: str
    limits: S12Limits
    applicable_check_ids: list[str]
    applicable_check_count: int


class S12EvidenceReferenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    cycle: int
    snapshot_id: str
    snapshot_digest: str


class S12MandatoryFamilyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    family_id: str
    check_ids: list[str]


class S12CohortResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exclusions: list[S12Exclusion]


class S12ApplicationVector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    cycle: int
    lifecycle_revision: int
    evidence_revision: int
    current_run_id: str
    current_evidence_snapshot_id: str
    phase: str
    route: str


class S12EvidenceEventVector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    kind: str
    application_id: str
    cycle: int
    revision: int
    snapshot_id: str
    declared_content_sha256: str
    payload_digest: str


class S12LifecycleEventVector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    application_id: str
    cycle: int
    revision: int
    reason_code: str
    phase: str


class S12AuditEventVector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    reason_code: str
    revision: int
    recorded_at: int


class S12GovernanceEventVector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    revision: int
    kind: str
    scope: str
    reason_code: str
    payload_digest: str


class S12ManifestVector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_id: str
    schema_version: str
    declared_digest: str
    content_digest: str


class S12ArtifactVector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    content_digest: str


class S12BusinessMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lifecycle_revision: int
    evidence_revision: int
    evidence_count: int
    evidence_digest: str | None = None
    current_run_reference: str | None = None
    governance_revision: int = 0
    activation_count: int = 0
    activation_digest: str | None = None
    applications_vector: list[S12ApplicationVector] = Field(default_factory=list)
    applications_id_list_digest: str = ""
    evidence_events_vector: list[S12EvidenceEventVector] = Field(
        default_factory=list
    )
    evidence_events_id_list_digest: str = ""
    lifecycle_events_vector: list[S12LifecycleEventVector] = Field(
        default_factory=list
    )
    lifecycle_events_id_list_digest: str = ""
    audit_events_vector: list[S12AuditEventVector] = Field(default_factory=list)
    audit_events_id_list_digest: str = ""
    governance_events_vector: list[S12GovernanceEventVector] = Field(
        default_factory=list
    )
    governance_events_id_list_digest: str = ""
    manifests_vector: list[S12ManifestVector] = Field(default_factory=list)
    manifests_id_list_digest: str = ""
    artifacts_vector: list[S12ArtifactVector] = Field(default_factory=list)
    artifacts_id_list_digest: str = ""


class S12Denominators(BaseModel):
    model_config = ConfigDict(extra="forbid")

    E: int
    E_all: int
    applicable_opportunities: int
    n_consistent: int
    n_inconsistent: int
    n_consistent_decisive: int
    labelability: int
    uncertain_on_inconsistent: int
    skipped_rate: int
    missing_rate: int
    error_rate: int
    conditional_fpr: int


class S12PointMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coverage: float | None = None
    false_positive_rate: float | None = None
    false_negative_rate: float | None = None
    miss_rate: float | None = None
    labelability: float | None = None
    uncertain_on_inconsistent: float | None = None
    skipped_rate: float | None = None
    missing_rate: float | None = None
    error_rate: float | None = None
    conditional_fpr: float | None = None


class S12PredictionCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consistent: int
    inconsistent: int
    uncertain: int
    skipped: int
    missing: int
    error: int


class S12MetricIntervals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coverage: list[float]
    false_positive_rate: list[float]
    false_negative_rate: list[float]
    miss_rate: list[float]
    labelability: list[float]
    uncertain_on_inconsistent: list[float]
    skipped_rate: list[float]
    missing_rate: list[float]
    error_rate: list[float]
    conditional_fpr: list[float]

    def values(self) -> list[list[float]]:
        return list(self.model_dump().values())


class S12MetricBounds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coverage_lower: float | None = None
    false_positive_rate_upper: float | None = None
    false_negative_rate_upper: float | None = None
    miss_rate_upper: float | None = None


class S12StatisticsBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    membership: str
    opportunity_count: int
    denominators: S12Denominators
    prediction_counts: "S12PredictionCounts"
    point: S12PointMetrics
    # Two-sided 95% bootstrap intervals are per-metric [low, high] pairs.
    interval_95_two_sided: S12MetricIntervals | None = None
    bounds_95_one_sided: S12MetricBounds | None = None
    estimable: bool
    not_estimable_reasons: list[str]
    conclusion: str


class S12ScopeEligibility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    holdout_eligible: bool
    reasons: list[str]


class S12PredictionError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opportunity_id: str
    reason_code: str


class S12BusinessDeltas(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lifecycle_revision: int
    evidence_rows: int
    evidence_digest: str | None = None
    current_run_pointer: int
    policy_revision: int
    governance_revision: int


class S12PlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["s12-evaluation-plan/1"]
    plan_id: str
    scope: Literal["C", "R-E2E", "R-T4-conditional"]
    seed: int
    budget: S12Budget
    stop_rule: Literal["plan-exhausted", "budget-or-plan"]
    split: S12Split
    clusters: list[S12ClusterResponse]
    tracks: S12TrackMemberships
    views: S12ViewMemberships
    opportunities: list[S12OpportunityResponse]
    label_manifest: S12LabelManifestResponse
    environment: S12EnvironmentResponse
    release: S12ReleaseResponse
    checker_artifact: S12CheckerArtifact
    run_specs: dict[str, S12RunSpec]
    evidence_references: list[S12EvidenceReferenceResponse]
    mandatory_check_families: list[S12MandatoryFamilyResponse]
    cohort: S12CohortResponse | None = None
    business_before: S12BusinessMeasurement
    frozen_at: int
    plan_digest: str


class S12StartJobBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str


class S12JobResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle_id: str | None = None
    status: str | None = None
    reason_codes: list[str] | None = None


class S12JobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["s12-job/1"]
    job_id: str
    plan_id: str
    plan_digest: str
    worker_id: str
    status: str
    fence: int
    attempt_no: int
    lease_until: int | None = None
    rerun_of_bundle_id: str | None = None
    result: S12JobResult | None = None
    reason_codes: list[str] | None = None
    created_at: int


class S12ProcessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    job_id: str = ""
    bundle_id: str | None = None
    attempt_no: int = 0
    reason_code: str | None = None
    reason_codes: list[str] | None = None


class S12StrataStatistics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    difficulty: dict[str, S12StatisticsBlock]
    data_source: dict[str, S12StatisticsBlock]
    document_combination: dict[str, S12StatisticsBlock]
    perturbation_family: dict[str, S12StatisticsBlock]


class S12RunnerCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    verdict: Literal["consistent", "inconsistent", "uncertain", "skipped"]
    severity: str
    reason_codes: list[str]


class S12RunnerApplication(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    run_id: str
    checks: list[S12RunnerCheck] | None = None
    error: str | None = None


class S12StopObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stop_reason: Literal["plan-exhausted", "budget-or-plan"]
    elapsed_ms: int
    completed_run_ids: list[str]


class S12ResultMaterial(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["C", "R-E2E", "R-T4-conditional"]
    seed: int
    budget: S12Budget
    stop_rule: Literal["plan-exhausted", "budget-or-plan"]
    split: S12Split
    release: S12ReleaseResponse
    environment: S12EnvironmentResponse
    evidence_references: list[S12EvidenceReferenceResponse]
    label_manifest: S12LabelManifestResponse
    mandatory_check_families: list[S12MandatoryFamilyResponse]
    cohort: S12CohortResponse | None = None
    clusters: list[S12ClusterResponse]
    opportunities: list[S12OpportunityResponse]
    tracks: S12TrackMemberships
    views: S12ViewMemberships
    predictions: dict[
        str,
        Literal[
            "consistent",
            "inconsistent",
            "uncertain",
            "skipped",
            "missing",
            "error",
        ],
    ]
    errors: list[S12PredictionError]
    missing_opportunities: list[str]
    tracks_statistics: S12TrackStatistics
    views_statistics: S12ViewStatistics
    mandatory_family_statistics: dict[str, S12StatisticsBlock]
    strata: S12StrataStatistics
    scope_eligibility: S12ScopeEligibility
    status: str
    status_reasons: list[str]
    runner_result_digest: str
    stop_reason: Literal["plan-exhausted", "budget-or-plan"]
    completed_run_ids: list[str]
    business_before: S12BusinessMeasurement
    business_after: S12BusinessMeasurement
    business_deltas: S12BusinessDeltas


class S12ReplayPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["s12-replay-package/1"]
    plan: S12PlanResponse
    predictions: dict[
        str,
        Literal[
            "consistent",
            "inconsistent",
            "uncertain",
            "skipped",
            "missing",
            "error",
        ],
    ]
    errors: list[S12PredictionError]
    missing_opportunities: list[str]
    applications: list[S12RunnerApplication]
    stop: S12StopObservation
    runner_result_digest: str
    status: str
    status_reasons: list[str]
    scope_eligibility: S12ScopeEligibility
    tracks_statistics: S12TrackStatistics
    views_statistics: S12ViewStatistics
    mandatory_family_statistics: dict[str, S12StatisticsBlock]
    strata: S12StrataStatistics
    business_before: S12BusinessMeasurement
    business_after: S12BusinessMeasurement
    business_deltas: S12BusinessDeltas
    result_material: S12ResultMaterial


class S12BundleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["s12-evaluation-bundle/1"]
    bundle_id: str
    plan_id: str
    plan_digest: str
    job_id: str
    fence: int
    attempt_no: int
    worker_id: str
    rerun_of_bundle_id: str | None
    run_started_at: int
    run_settled_at: int
    status: str
    scope: Literal["C", "R-E2E", "R-T4-conditional"]
    status_reasons: list[str]
    tracks: S12TrackStatistics
    views: S12ViewStatistics
    mandatory_check_families: dict[str, S12StatisticsBlock]
    strata: S12StrataStatistics
    scope_eligibility: S12ScopeEligibility
    clusters: list[S12ClusterResponse]
    opportunities: list[S12OpportunityResponse]
    tracks_declared: S12TrackMemberships
    views_declared: S12ViewMemberships
    evidence_references: list[S12EvidenceReferenceResponse]
    label_manifest: S12LabelManifestResponse
    cohort: S12CohortResponse | None
    predictions: dict[
        str,
        Literal[
            "consistent",
            "inconsistent",
            "uncertain",
            "skipped",
            "missing",
            "error",
        ],
    ]
    prediction_alphabet: list[
        Literal[
            "consistent",
            "inconsistent",
            "uncertain",
            "skipped",
            "missing",
            "error",
        ]
    ]
    gold_alphabet: list[
        Literal["consistent", "inconsistent", "indeterminate", "not_applicable"]
    ]
    errors: list[S12PredictionError]
    missing_opportunities: list[str]
    release: S12ReleaseResponse
    environment: S12EnvironmentResponse
    stop_rule: Literal["plan-exhausted", "budget-or-plan"]
    stop_reason: Literal["plan-exhausted", "budget-or-plan"]
    stop_elapsed_ms: int
    completed_run_ids: list[str]
    stop_rule_satisfied: bool
    runner_result_digest: str
    evidence_snapshot_ids: list[str]
    seed: int
    budget: S12Budget
    split: S12Split
    business_before: S12BusinessMeasurement
    business_after: S12BusinessMeasurement
    business_deltas: S12BusinessDeltas
    result_digest: str
    replay_package: S12ReplayPackage
    replay_package_digest: str
    command: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@s12_router.post("/controlled/s12/plans/freeze", response_model=S12PlanResponse)
def s12_freeze_plan(body: S12FreezePlanBody, request: Request) -> dict[str, Any]:
    """Freeze one evaluation plan from stable references: the authority
    resolves the S01 evidence snapshots, the governed S08 release and the
    independent label manifest, derives the environment, and measures
    business state."""
    _s12_require_operator(request)
    try:
        return _s12_service(request).freeze_plan(body.model_dump(by_alias=True))
    except ValueError:
        raise _s12_invalid_command()
    except (S12IntegrityError, S12Unavailable):
        raise _s12_closed_failure()


@s12_router.post("/controlled/s12/jobs/start", response_model=S12JobResponse)
def s12_start_job(body: S12StartJobBody, request: Request) -> dict[str, Any]:
    """Start one durable evaluation job for a frozen plan, bound to the
    registered server worker identity."""
    _s12_require_operator(request)
    try:
        return _s12_service(request).start_job(body.plan_id)
    except ValueError:
        raise _s12_not_found()
    except (S12IntegrityError, S12Unavailable):
        raise _s12_closed_failure()


@s12_router.post(
    "/controlled/s12/jobs/{job_id}/process", response_model=S12ProcessResponse
)
def s12_process_job(job_id: str, request: Request) -> dict[str, Any]:
    """Execute one claimed durable job: restricted runner, parent
    verification/materialization, eligibility and statistics, status
    selection, and atomic bundle publication."""
    _s12_require_operator(request)
    try:
        return _s12_service(request).process_job(job_id)
    except ValueError:
        raise _s12_not_found()
    except (S12IntegrityError, S12Unavailable):
        raise _s12_closed_failure()


@s12_router.post(
    "/controlled/s12/jobs/{job_id}/cancel", response_model=S12JobResponse
)
def s12_cancel_job(job_id: str, request: Request) -> dict[str, Any]:
    """Cancel a queued or leased job with zero business delta."""
    _s12_require_operator(request)
    try:
        return _s12_service(request).cancel_job(job_id)
    except ValueError:
        raise _s12_not_found()
    except (S12IntegrityError, S12Unavailable):
        raise _s12_closed_failure()


@s12_router.get("/controlled/s12/jobs/{job_id}", response_model=S12JobResponse)
def s12_query_job(job_id: str, request: Request) -> dict[str, Any]:
    """Query one durable job and its lease/fence/attempt state."""
    _s12_require_operator(request)
    try:
        return _s12_service(request).query_job(job_id)
    except ValueError:
        raise _s12_not_found()
    except (S12IntegrityError, S12Unavailable):
        raise _s12_closed_failure()


@s12_router.post(
    "/controlled/s12/jobs/{job_id}/rerun", response_model=S12JobResponse
)
def s12_rerun_job(job_id: str, request: Request) -> dict[str, Any]:
    """Start a linked rerun job from the source bundle's frozen replay
    package: its bundle will reference the source bundle via
    ``rerun_of_bundle_id`` and never overwrite it."""
    _s12_require_operator(request)
    try:
        return _s12_service(request).rerun_job(job_id)
    except ValueError:
        raise _s12_not_found()
    except (S12IntegrityError, S12Unavailable):
        raise _s12_closed_failure()


@s12_router.get(
    "/controlled/s12/bundles/{bundle_id}", response_model=S12BundleResponse
)
def s12_query_bundle(bundle_id: str, request: Request) -> dict[str, Any]:
    """Query one immutable content-addressed evaluation bundle (the complete
    frozen replay package with the independent result digest)."""
    _s12_require_operator(request)
    try:
        return _s12_service(request).query_bundle(bundle_id)
    except ValueError:
        raise _s12_not_found()
    except (S12IntegrityError, S12Unavailable):
        raise _s12_closed_failure()
