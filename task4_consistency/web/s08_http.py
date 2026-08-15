"""S08 governed policy release HTTP adapter.

The complete existing S08 HTTP adapter family moved verbatim from
``web/app.py`` (architecture preflight): S08 identity checks, closed Pydantic
DTOs, stable error mapping, command/query adapters, the ``/controlled/s08/*``
routes and the React shell adapter.  The module owns no process-level
authority construction: ``app.py`` builds ``S08_SERVICE`` and registers the
router through ``register_router`` on the shared FastAPI application, so the
URL set, methods, status codes, error envelopes, response schemas, OpenAPI
operation identities, cache headers, auth behavior, lifespan behavior and
runtime output form are preserved unchanged.

Every application-level authority name (``S08_SERVICE``, the six S08
credential constants that ``tests/test_s08_http.py`` rebinds on ``web.app``,
and the never-rebound helpers ``_s01_has_credential``/``_s01_disable_cache``/
``_react_shell_index_html``) is read dynamically from the application module
each FastAPI app binds at registration time.  The binding lives on the app
itself (private ``app.state`` key) and is resolved per request through
``request.app``, so two apps registered in one process stay isolated.
Registration is idempotent for the same app and module, and a conflicting
module for an already registered app is rejected before any state or route
change.  This module never imports the application at import time and stays
independently importable.
"""

from __future__ import annotations

from typing import Any, Callable, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from task4_consistency.controlled.s08 import (
    PolicyConflict,
    PolicyGovernanceService,
    PolicyInvalidTransition,
    PolicyNotFound,
    PolicyPrincipal,
    PolicyUnavailable,
    S08_SCOPE,
)

s08_router = APIRouter()

# S09 policy impact / safety hold / recovery commands live on their own
# router so the S08 surface (and its 17-route contract) stays unchanged;
# they share the same DTO, auth and error mapping helpers.
s09_router = APIRouter()

# The private FastAPI ``app.state`` key holding the application module bound
# by ``register_router``; requests resolve their authority per app.
_S08_APP_MODULE_STATE_KEY = "_s08_application_module"


def _s08_app_module(request: Request) -> Any:
    """The application module bound to this request's FastAPI app."""
    module = getattr(request.app.state, _S08_APP_MODULE_STATE_KEY, None)
    if module is None:
        raise HTTPException(
            503,
            detail={
                "error": "S08_UNAVAILABLE",
                "message": "Controlled S08 policy governance is unavailable",
            },
        )
    return module


def register_router(app: FastAPI, app_module: Any) -> None:
    """Bind the live application module on the app itself and register the
    S08 router.  Idempotent for the same app and module; a conflicting module
    for an already registered app is rejected before any state or route
    change.  Called once by ``web.app`` at the former inline S08 section
    position."""
    existing = getattr(app.state, _S08_APP_MODULE_STATE_KEY, None)
    if existing is not None:
        if existing is not app_module:
            raise RuntimeError(
                "S08 router already registered with a different application module"
            )
        return
    setattr(app.state, _S08_APP_MODULE_STATE_KEY, app_module)
    app.include_router(s08_router)
    app.include_router(s09_router)


def _s08_service(request: Request) -> PolicyGovernanceService:
    module = _s08_app_module(request)
    if module.S08_SERVICE is None:
        raise HTTPException(
            503,
            detail={
                "error": "S08_UNAVAILABLE",
                "message": "Controlled S08 policy governance is unavailable",
            },
        )
    return module.S08_SERVICE


def _s08_require_role(request: Request, expected: str) -> PolicyPrincipal:
    module = _s08_app_module(request)
    credentials = {
        "admin": (module.S08_ADMIN_CREDENTIAL, module.S08_ADMIN_SUBJECT),
        "approver": (module.S08_APPROVER_CREDENTIAL, module.S08_APPROVER_SUBJECT),
        "operator": (module.S08_OPERATOR_CREDENTIAL, module.S08_OPERATOR_SUBJECT),
        # The S09 least-privilege diagnostic identities are optional: an
        # environment without them stays fail-closed (403) for those roles.
        "replay_operator": (
            getattr(module, "S09_REPLAY_CREDENTIAL", ""),
            getattr(module, "S09_REPLAY_SUBJECT", ""),
        ),
        "simulation_operator": (
            getattr(module, "S09_SIMULATION_CREDENTIAL", ""),
            getattr(module, "S09_SIMULATION_SUBJECT", ""),
        ),
    }
    credential, subject = credentials.get(expected, ("", ""))
    if expected in {"replay_operator", "simulation_operator"} and not (
        module._s09_diagnostic_configuration_valid()
    ):
        # S09 configuration gate: missing or aliased replay/simulation
        # identities fail diagnostic authorization closed.  S08 command
        # authorization is never affected by this gate.
        raise HTTPException(
            403,
            detail={
                "error": "S08_FORBIDDEN",
                "message": "Registered S09 diagnostic identity required",
            },
        )
    if not subject or not module._s01_has_credential(request, credential):
        raise HTTPException(
            403,
            detail={
                "error": "S08_FORBIDDEN",
                "message": "Registered S08 identity required",
            },
        )
    return PolicyPrincipal(
        subject=subject,
        role=expected,
        scope=S08_SCOPE,
        source_id="s08-web-bearer",
    )


def _s08_require_preview_role(request: Request) -> PolicyPrincipal:
    """The impact preview is the read-only computation the independent
    Policy Approver binds at approval time, so both the Rule Administrator
    and the Policy Approver may run it; every other surface keeps its
    single-role separation."""
    module = _s08_app_module(request)
    credentials = {
        "admin": (module.S08_ADMIN_CREDENTIAL, module.S08_ADMIN_SUBJECT),
        "approver": (module.S08_APPROVER_CREDENTIAL, module.S08_APPROVER_SUBJECT),
    }
    for role, (credential, subject) in credentials.items():
        if subject and module._s01_has_credential(request, credential):
            return PolicyPrincipal(
                subject=subject,
                role=role,
                scope=S08_SCOPE,
                source_id="s08-web-bearer",
            )
    raise HTTPException(
        403,
        detail={
            "error": "S08_FORBIDDEN",
            "message": "Registered S08 identity required",
        },
    )


def _s08_not_found(error: Exception) -> HTTPException:
    return HTTPException(
        404,
        detail={"error": "S08_NOT_FOUND", "message": "Governance object is unavailable"},
    )


def _s08_rejected(error: Exception) -> HTTPException:
    if isinstance(error, PolicyUnavailable):
        return HTTPException(
            503,
            detail={
                "error": "S08_UNAVAILABLE",
                "message": "Governance authority is unavailable",
            },
        )
    if isinstance(error, PolicyNotFound):
        return _s08_not_found(error)
    if isinstance(error, RuntimeError) and not isinstance(
        error, (PolicyInvalidTransition, PolicyConflict)
    ):
        return HTTPException(
            503,
            detail={
                "error": "S08_UNAVAILABLE",
                "message": "Governance authority is unavailable",
            },
        )
    return HTTPException(
        409,
        detail={
            "error": "S08_CONFLICT",
            "message": "Governance command conflicts with current state",
        },
    )


# --- S08 governed policy release surface ----------------------------------

class S08CommandBody(BaseModel):
    """Common command envelope: semantic payload plus required expected
    governance revision and semantic idempotency key.  Bodies never carry
    paths, URLs, code, I/O or credentials, and neither field may be omitted
    or nulled: every typed command is fenced by its revision CAS."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_governance_revision: int = Field(ge=0)


class S08ImportLegacyBody(S08CommandBody):
    source_bundle_id: str


class S08DraftValidity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid_from: str


class S08DraftMetadata(BaseModel):
    """The closed, non-runtime draft metadata the Admin may revise: exactly
    scope, validity window, source and reason.  No wildcard dict is exposed
    to the generated client."""

    model_config = ConfigDict(extra="forbid")

    scope: str
    validity: S08DraftValidity
    source: str
    reason: str


class S08ReviseDraftBody(S08CommandBody):
    draft_id: str
    metadata: S08DraftMetadata


class S08FreezeCandidateBody(S08CommandBody):
    draft_id: str


class S08CandidateCommandBody(S08CommandBody):
    candidate_id: str


class S08ApproveBody(S08CandidateCommandBody):
    activation_time: int
    recovery_release_id: str
    preview_manifest_id: str


class S08RejectBody(S08CandidateCommandBody):
    reason_code: str


class S08ScheduleBody(S08CommandBody):
    approval_binding_id: str
    activation_at: int


class S08StopActivationsBody(S08CommandBody):
    reason_code: str


class S08ComponentRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    id: str
    digest: str


class S08ActivationHold(BaseModel):
    """The closed activation-hold model: event identity, registered reason
    code, trusted stop time and stopping subject."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    reason_code: str
    stopped_at: int
    stopped_by: str


class S09HoldRef(BaseModel):
    """One immutable Policy Safety Hold fact: identity, reason, scope,
    actor, authority revision, evidence digest and the fixed recovery
    criterion.  Never auto-expires."""

    model_config = ConfigDict(extra="forbid")

    hold_id: str
    event_id: str
    reason_code: str
    scope: str  # the served governance scope the hold was imposed in
    hold_scope: str  # the application/partition scope the hold fences
    imposed_by: str
    imposed_at: int | None = None
    authority_revision: int | None = None
    evidence_digest: str | None = None
    recovery_criterion_id: str | None = None
    recovery_criterion_digest: str | None = None


class S08StatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track: str
    capability_gate: str
    scope: str
    governance_revision: int
    active_generation: int | None = None
    bootstrap: bool
    activation_hold: S08ActivationHold | None = None
    holds: list[S09HoldRef] = Field(default_factory=list)
    final_impact_digest: str | None = None
    watermark: int


class S08ActiveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    track: str
    capability_gate: str
    scope: str
    active_generation: int | None = None
    activation_event_id: str | None = None
    candidate_id: str | None = None
    manifest_id: str | None = None
    manifest_digest: str | None = None
    approval_binding_id: str | None = None
    approval_binding_digest: str | None = None
    validation_bundle_id: str | None = None
    validation_bundle_digest: str | None = None
    recovery_release_id: str | None = None
    activated_at: int | None = None
    bootstrap: bool = False
    activation_hold: S08ActivationHold | None = None
    holds: list[S09HoldRef] = Field(default_factory=list)
    final_impact_digest: str | None = None
    final_impact_manifest_id: str | None = None
    final_impact_member_count: int | None = None
    components: list[S08ComponentRef] = Field(default_factory=list)


class S08CandidateSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    status: str
    manifest_id: str | None = None
    manifest_digest: str | None = None
    validation_bundle_id: str | None = None
    approval_binding_id: str | None = None
    active_generation: int | None = None
    author_subject: str | None = None


class S08CandidatesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track: str
    capability_gate: str
    scope: str
    candidates: list[S08CandidateSummary]


class S08DraftSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_id: str
    status: str
    revision: int
    source_bundle_id: str
    source_sha256: str
    mapping_ledger_id: str
    mapping_ledger_digest: str
    candidate_id: str | None = None
    bootstrap: bool = False


class S08DraftsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track: str
    capability_gate: str
    scope: str
    drafts: list[S08DraftSummary]


class S08EventActor(BaseModel):
    """The closed identity recorded on every governance event by the single
    ledger writer: subject, role and source id, with no wildcard payload."""

    model_config = ConfigDict(extra="forbid")

    subject: str
    role: str
    source_id: str


class S08EventRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    revision: int
    kind: str
    actor: S08EventActor
    trusted_time: int
    reason_code: str | None = None
    candidate_id: str | None = None
    draft_id: str | None = None
    manifest_id: str | None = None
    approval_binding_id: str | None = None
    activation_event_id: str | None = None
    active_generation: int | None = None


class S08EventsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track: str
    capability_gate: str
    scope: str
    governance_revision: int
    events: list[S08EventRef]


# --- T08 / Issue #42: closed typed workspace and command DTOs ---------------

class S08ErrorDetail(BaseModel):
    """The closed error envelope shared by every S08 HTTP error: the stable
    registered code plus its fixed generic message, with no caller or
    internal detail."""

    model_config = ConfigDict(extra="forbid")

    error: str
    message: str


class S08ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: S08ErrorDetail


class S08ValidationErrorItem(BaseModel):
    """One sanitized 422 item: the request-validation handler never reflects
    rejected input, context, credentials or oversized payload content."""

    model_config = ConfigDict(extra="forbid")

    loc: list[str | int]
    msg: str
    type: str


class S08ValidationErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: list[S08ValidationErrorItem]


# The closed error responses every T08 path declares: 403 (identity/role),
# 404 (existence-hiding), 409 (stale governance revision), 422 (invalid
# command) and 503 (unavailable authority).
_S08_ERROR_RESPONSES: dict[int, dict[str, Any]] = {
    403: {"model": S08ErrorResponse},
    404: {"model": S08ErrorResponse},
    409: {"model": S08ErrorResponse},
    422: {"model": S08ValidationErrorResponse},
    503: {"model": S08ErrorResponse},
}


class S08CommandResult(BaseModel):
    """Base of every command success DTO; ``replayed`` marks an idempotent
    replay of an already-accepted command."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"]
    replayed: bool = False


class S08ImportLegacyResponse(S08CommandResult):
    draft_id: str
    mapping_ledger_id: str
    mapping_ledger_digest: str
    source_sha256: str
    knowledge_sha256: str
    governance_revision: int


class S08ReviseDraftResponse(S08CommandResult):
    draft_id: str
    draft_revision: int
    governance_revision: int


class S08FreezeCandidateResponse(S08CommandResult):
    candidate_id: str
    manifest_id: str
    manifest_digest: str
    components: list[S08ComponentRef]
    governance_revision: int


class S08RequestValidationResponse(S08CommandResult):
    policy_job_id: str
    candidate_id: str
    governance_revision: int


class S08SubmitReviewResponse(S08CommandResult):
    candidate_id: str
    validation_bundle_id: str
    governance_revision: int


class S08ApproveResponse(S08CommandResult):
    candidate_id: str
    approval_binding_id: str
    approval_binding_digest: str
    validation_bundle_id: str
    validation_bundle_digest: str
    author_subject: str
    approver_subject: str
    activation_time: int
    recovery_release_id: str
    preview_manifest_id: str | None = None
    preview_manifest_digest: str | None = None
    impact_envelope: S09ImpactEnvelope | None = None
    impact_envelope_digest: str | None = None
    governance_revision: int


class S08RejectResponse(S08CommandResult):
    candidate_id: str
    reason_code: str
    governance_revision: int


class S08ScheduleResponse(S08CommandResult):
    candidate_id: str
    reservation_id: str
    policy_job_id: str
    activation_at: int
    governance_revision: int


class S08CancelResponse(S08CommandResult):
    candidate_id: str
    reason_code: str
    governance_revision: int


class S08StopActivationsResponse(S08CommandResult):
    """The closed operator response for an activation hold: scope, the
    registered hold reason, the governance event identity and revision."""

    scope: str
    reason_code: str
    governance_event_id: str
    governance_revision: int


# --- T08 candidate workspace models -----------------------------------------

class S08ManifestCompatibility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checker_build: str
    input_contract_schema: str
    evidence_readiness_policy: str


class S09PreviewBody(S08CommandBody):
    """The closed impact-preview command: one governed candidate whose
    conservative impact is computed against the active predecessor."""

    candidate_id: str


class S09PreviewResponse(S08CommandResult):
    phase: Literal["preview"]
    manifest_id: str
    digest: str
    scope: str
    oracle_version: str
    level: int
    expanded_to_full_scope: bool
    member_count: int
    partition_counts: dict[str, int]
    zero_hit_proof: bool
    target_generation: int
    governance_revision: int


class S09ImposeHoldBody(S08CommandBody):
    """The closed hold-imposition command: a registered reason and the
    scope the Policy Safety Hold covers (open_cycle, the served scope, or
    one application id)."""

    reason_code: str
    hold_scope: str


class S09ImposeHoldResponse(S08CommandResult):
    hold_id: str
    hold_scope: str
    reason_code: str
    recovery_criterion_id: str
    recovery_criterion_digest: str
    governance_event_id: str
    governance_revision: int


class S09ReplayBody(S08CommandBody):
    """The closed reproduction-replay command: one exact governed release
    over one fixed evidence snapshot for one explicit application.  A
    separate replay identity; an omitted application never enumerates."""

    release_candidate_id: str
    application_id: str


class S09SimulationBody(S08CommandBody):
    """The closed counterfactual-simulation command: one exact governed
    release over one fixed evidence snapshot for one explicit application.
    A separate simulation identity; an omitted application never
    enumerates."""

    release_candidate_id: str
    application_id: str


class S09DiagnosticCheck(BaseModel):
    """The minimized diagnostic check: rule identity, verdict, severity and
    registered reason codes only -- no raw value, OCR text, locator or free
    text ever leaves the runner."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    verdict: str
    severity: str
    reason_codes: list[str] = Field(default_factory=list)


class S09DiagnosticSelectionOutcome(BaseModel):
    """The minimized selection outcome: rule/document/field identity and
    the machine decision only -- no raw field value, OCR text, locator or
    free text ever leaves the runner."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    observation_id: str | None = None
    document_id: str
    document_role: str
    field: str
    selected: bool
    reason_code: str


class S09DiagnosticNormalizationOutcome(BaseModel):
    """The minimized normalization outcome: rule/document/field identity,
    the OCR-fix flag and a digest of the normalized value (never the raw
    value itself), so migration differentials compare machine-decidable
    behavior without exposing content."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    observation_id: str | None = None
    document_id: str
    document_role: str
    field: str
    ocr_fix: bool
    normalized_sha256: str | None = None


class S09DiagnosticBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    namespace: str
    bundle_id: str | None = None
    bundle_digest: str | None = None
    release_candidate_id: str
    release_manifest_id: str | None = None
    release_manifest_digest: str | None = None
    application_id: str
    outcome: str
    reason_code: str | None = None
    check_count: int | None = None
    finding_count: int | None = None
    checks: list[S09DiagnosticCheck] = Field(default_factory=list)
    selection_outcomes: list[S09DiagnosticSelectionOutcome] = Field(
        default_factory=list
    )
    normalization_outcomes: list[S09DiagnosticNormalizationOutcome] = Field(
        default_factory=list
    )
    route: str | None = None
    approval_binding_id: str | None = None
    run_identity: str | None = None
    business_revision_delta: int = 0


class S09ReplayBundle(S09DiagnosticBundle):
    """The closed replay result schema: one bundle whose namespace is fixed
    to the isolated replay identity."""

    namespace: Literal["s09-replay"]


class S09SimulationBundle(S09DiagnosticBundle):
    """The closed simulation result schema: one bundle whose namespace is
    fixed to the isolated simulation identity."""

    namespace: Literal["s09-simulation"]


class S09ReplayResponse(S08CommandResult):
    namespace: Literal["s09-replay"]
    release_candidate_id: str
    bundle_count: int
    bundles: list[S09ReplayBundle]
    business_revision_delta: int
    governance_revision: int


class S09SimulationResponse(S08CommandResult):
    namespace: Literal["s09-simulation"]
    release_candidate_id: str
    bundle_count: int
    bundles: list[S09SimulationBundle]
    business_revision_delta: int
    governance_revision: int


class S09ProposeRollbackBody(S08CommandBody):
    """The closed rollback-proposal command: revalidate the exact
    historical governed release and stage a fresh rollback candidate."""

    release_candidate_id: str
    reason_code: str


class S09RollbackCompatibility(BaseModel):
    """The machine-decidable rollback compatibility verdict: compatible
    only when the exact historical artifacts revalidate against the
    current gates."""

    model_config = ConfigDict(extra="forbid")

    compatible: bool
    reason_code: str


class S09ProposeRollbackResponse(S08CommandResult):
    candidate_id: str
    manifest_id: str
    manifest_digest: str
    validation_bundle_id: str
    validation_bundle_digest: str
    rollback_target_id: str
    compatibility: S09RollbackCompatibility
    governance_revision: int


class S09RecoverHoldBody(S08CommandBody):
    """The closed hold-recovery command: the exact hold identity and the
    exact recovery generation that must equal the active generation."""

    hold_id: str
    recovery_generation: int


class S09RecoverHoldResponse(S08CommandResult):
    hold_id: str
    hold_released_event_id: str
    recovery_generation: int
    governance_revision: int


class S08CandidateManifest(BaseModel):
    """The immutable candidate manifest: digest, scope and the exact
    registry-bound components, typed so generated clients never see a
    wildcard dict."""

    model_config = ConfigDict(extra="forbid")

    manifest_id: str
    digest: str
    schema_version: str
    scope: str
    components: list[S08ComponentRef]
    compatibility: S08ManifestCompatibility


class S08ValidatorIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite: str
    build: str
    code_sha256: str
    python: str
    machine: str


class S08CorpusItemRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    sha256: str


class S08CorpusManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track: Literal["C-DEV-REG"]
    count: int
    digest: str
    items: list[S08CorpusItemRef]


class S08ValidationInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component_digests: dict[str, str]
    mapping_ledger_id: str | None = None
    mapping_ledger_digest: str | None = None
    corpus: S08CorpusManifest | None = None


class S08ValidationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str
    outcome: Literal["pass", "fail", "protected_fail"]
    detail: str


class S08ValidationDeterminism(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs: int
    equal: bool
    digest: str | None = None
    reason: str


class S08CorpusDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor: str | None = None
    applications_compared: int
    applications_skipped: int
    checks_equal: bool
    selection_equal: bool
    normalization_equal: bool
    verdicts_equal: bool
    route_equal: bool
    corpus_digest: str | None = None
    equal: bool
    reason: str


class S08ValidationResults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checks: list[S08ValidationCheck]
    failed_count: int
    determinism: S08ValidationDeterminism
    corpus_diff: S08CorpusDiff


class S08ValidationBundle(BaseModel):
    """The immutable validation evidence: validator identity, pinned inputs
    and the typed checks/outcome, so the approval never floats to modified
    content."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    candidate_id: str
    manifest_id: str
    manifest_digest: str
    validation_suite: str
    validator_build: str
    validator: S08ValidatorIdentity
    inputs: S08ValidationInputs
    results: S08ValidationResults
    status: Literal["validated", "rejected"]


class S08ReviewChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: str
    change: Literal["added", "modified", "removed"]
    anchor_id: str | None = None
    anchor_digest: str | None = None
    candidate_id: str | None = None
    candidate_digest: str | None = None


class S08ComponentDigestRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    digest: str


class S08ApplicableCheckDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    anchor: list[str]
    candidate: list[str]
    added: list[str]
    removed: list[str]


class S08BehaviorDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    equal: bool
    reason: str


class S08MappingLedgerSourceRefs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules_bundle_id: str
    rules_sha256: str
    knowledge_sha256: str


class S08MappingLedgerItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_ref: str
    source_pointer: str
    source_digest: str
    classification: Literal[
        "exact", "unsupported", "non_runtime_excluded", "explicit_transform"
    ]
    target_ref: str | None = None
    importer_version: str
    reason: str
    result_digest: str


class S08MappingLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    importer_version: str
    source_refs: S08MappingLedgerSourceRefs
    items: list[S08MappingLedgerItem]


class S08UnsupportedReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int
    items: list[S08MappingLedgerItem]


class S08ReviewMaterial(BaseModel):
    """The deterministic review diff shared by the pre-approval workspace
    and the approval binding: component changes, applicable-check delta,
    behavior result, mapping ledger and unsupported report."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    candidate_id: str
    candidate_digest: str
    anchor_candidate_id: str | None = None
    anchor_components: dict[str, S08ComponentDigestRef]
    candidate_components: dict[str, S08ComponentDigestRef]
    changes: list[S08ReviewChange]
    applicable_check_delta: S08ApplicableCheckDelta
    behavior_delta: S08BehaviorDelta
    validation_bundle_id: str | None = None
    validation_bundle_digest: str | None = None
    mapping_ledger_id: str | None = None
    mapping_ledger: S08MappingLedger | None = None
    unsupported_report: S08UnsupportedReport


class S09EnvelopeRef(BaseModel):
    """One release reference inside the impact envelope: identity plus the
    exact manifest digest the approver bound."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    manifest_digest: str


class S09MemberDeltaRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_added: int
    removal: Literal["machine_proof_only"]


class S09CountCeilings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_total: int
    per_partition: dict[str, int]


class S09AuthorityWatermarks(BaseModel):
    """The approved preview-time authority watermarks: the Governance
    revision at preview and the Lifecycle projection watermark.  The final
    manifest may only move the governance revision forward; the lifecycle
    watermark must match exactly."""

    model_config = ConfigDict(extra="forbid")

    governance_revision: int
    lifecycle_watermark: int


class S09AuthorityRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum: int
    maximum: int


class S09PermittedAuthorityMovement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    governance_revision: S09AuthorityRange
    lifecycle_watermark: S09AuthorityRange


class S09DependencyIndex(BaseModel):
    """The approved dependency-index completeness fact bound by the
    envelope: the digest of the Lifecycle-owned dependency index."""

    model_config = ConfigDict(extra="forbid")

    complete: bool
    index_digest: str
    oracle_version: str


class S09ImpactEnvelope(BaseModel):
    """The machine-decidable approval envelope: the exact preview digest,
    predecessor/candidate references, scope, oracle version, authority
    watermarks, dependency index, dependency categories, risk class,
    member-delta rules, count ceilings, approvals and protected conditions
    -- the typed wire shape of the digest-bound envelope the Approver binds
    at approval time."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    preview_digest: str
    predecessor: S09EnvelopeRef
    candidate: S09EnvelopeRef
    scope: str
    oracle_version: str
    authority_watermarks: S09AuthorityWatermarks
    permitted_authority_movement: S09PermittedAuthorityMovement
    dependency_index: S09DependencyIndex
    dependency_categories: list[str]
    risk_class: str
    member_delta_rules: S09MemberDeltaRules
    count_ceilings: S09CountCeilings
    required_approvals: list[str]
    protected_conditions: list[str]
    digest: str


class S08ApprovalBinding(BaseModel):
    """The fixed approver binding: the exact candidate/validation digests,
    the bound diff, scope, activation time and recovery release, plus the
    S09 impact preview/envelope the Approver binds for governed changes
    (absent on pre-S09 bootstrap bindings)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    candidate_id: str
    candidate_digest: str
    validation_bundle_id: str
    validation_bundle_digest: str
    diff: S08ReviewMaterial
    scope: str
    activation_time: int
    recovery_release_id: str
    approved_by: str
    preview_manifest_id: str | None = None
    preview_manifest_digest: str | None = None
    impact_envelope: S09ImpactEnvelope | None = None
    impact_envelope_digest: str | None = None


class S08ActiveAnchor(BaseModel):
    """The current recovery/active anchor: the prior active release the
    Approver would bind as the recovery release."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    manifest_digest: str


class S08CandidateWorkspaceResponse(BaseModel):
    """The closed T08 candidate workspace.  Besides the candidate/validation/
    approval wire facts it carries the authoritative governance revision
    (so an Approver can act without a separate status call), the
    authenticated actor role, the server-owned action list and the current
    recovery/active anchor."""

    model_config = ConfigDict(extra="forbid")

    track: Literal["C-DEMO"]
    capability_gate: Literal["G3"]
    candidate_id: str
    status: Literal[
        "candidate",
        "validated",
        "in_review",
        "approved",
        "scheduled",
        "active",
        "superseded",
        "rejected",
        "cancelled",
    ]
    manifest_id: str | None = None
    manifest_digest: str | None = None
    validation_bundle_id: str | None = None
    validation_bundle_digest: str | None = None
    approval_binding_id: str | None = None
    approval_binding_digest: str | None = None
    activation_event_id: str | None = None
    active_generation: int | None = None
    author_subject: str | None = None
    recovery_release_id: str | None = None
    activation_time: int | None = None
    manifest: S08CandidateManifest | None = None
    validation_bundle: S08ValidationBundle | None = None
    approval_binding: S08ApprovalBinding | None = None
    review_material: S08ReviewMaterial | None = None
    governance_revision: int
    actor_role: Literal["admin", "approver"]
    actions: list[str]
    active_anchor: S08ActiveAnchor | None = None
    events: list[S08EventRef]
    validation_outcome: S08ValidationOutcome | None = None
    activation_outcome: S08ActivationOutcome | None = None


class S08ValidationOutcome(BaseModel):
    """The closed, server-owned terminal/in-flight state of the candidate's
    validation job: pending while the worker runs, validated/rejected once
    the ledger records the verdict (rejected carries the registered reason),
    failed when the worker itself ended diagnostic.  Never inferred by the
    browser."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["pending", "validated", "rejected", "failed"]
    reason_code: str | None = None


class S08ActivationOutcome(BaseModel):
    """The closed, server-owned terminal/in-flight state of the candidate's
    activation: pending while the job is queued/leased, active only when the
    ledger records an activated event (with its event id and generation),
    failed when the worker ended diagnostic (with only the registered stable
    reason code; internal exception and write-point text never leaves the
    service)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["pending", "active", "failed"]
    reason_code: str | None = None
    activation_event_id: str | None = None
    active_generation: int | None = None


# --- T09 / Issue #43: governance workspace DTOs ------------------------------

class S09ActiveRelease(BaseModel):
    """The server-owned active governed release projection: generation,
    release/activation identities, bound evidence references, the recorded
    recovery release and the final-impact facts.  Never derived by the
    browser."""

    model_config = ConfigDict(extra="forbid")

    active_generation: int | None = None
    candidate_id: str | None = None
    manifest_id: str | None = None
    manifest_digest: str | None = None
    activation_event_id: str | None = None
    approval_binding_id: str | None = None
    validation_bundle_id: str | None = None
    validation_bundle_digest: str | None = None
    recovery_release_id: str | None = None
    activated_at: int | None = None
    bootstrap: bool = False
    final_impact_digest: str | None = None
    final_impact_manifest_id: str | None = None
    final_impact_member_count: int | None = None


class S09RecoveryAnchor(BaseModel):
    """The recorded known-good release identity: the exact release the
    current active was approved with as its recovery fallback.  The rollback
    form's only prefill option."""

    model_config = ConfigDict(extra="forbid")

    release_candidate_id: str


class S09WorkspaceEventRef(BaseModel):
    """One append-only governance event reference in the T09 workspace: the
    shared immutable identity fields plus the S09 hold/rollback/impact/
    activation/recovery references the page renders."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    revision: int
    kind: str
    actor: S08EventActor
    trusted_time: int
    reason_code: str | None = None
    candidate_id: str | None = None
    manifest_id: str | None = None
    activation_event_id: str | None = None
    active_generation: int | None = None
    hold_id: str | None = None
    release_candidate_id: str | None = None
    recovery_generation: int | None = None


class S09GovernanceWorkspaceResponse(BaseModel):
    """The closed T09 governance workspace: the authoritative governance
    revision, the authenticated actor role, the server-owned action list,
    the active release and known-good recovery anchor, the active hold
    union and the append-only S09 event refs -- one atomic snapshot the
    page renders.  The page never derives a transition, a hold or a
    recovery option."""

    model_config = ConfigDict(extra="forbid")

    track: Literal["C-DEMO"]
    capability_gate: Literal["G3"]
    scope: str
    governance_revision: int
    actor_role: Literal["admin", "approver", "operator", "auditor"]
    actions: list[str]
    active_release: S09ActiveRelease | None = None
    recovery_anchor: S09RecoveryAnchor | None = None
    holds: list[S09HoldRef]
    events: list[S09WorkspaceEventRef]


def _s08_command(
    request: Request,
    response: Response,
    role: str,
    command: Callable[[PolicyPrincipal], dict[str, Any]],
) -> dict[str, Any]:
    module = _s08_app_module(request)
    module._s01_disable_cache(response)
    principal = _s08_require_role(request, role)
    try:
        return command(principal)
    except (
        PolicyInvalidTransition,
        PolicyConflict,
        PolicyNotFound,
        PolicyUnavailable,
        RuntimeError,
    ) as error:
        raise _s08_rejected(error) from error


def _s08_query(
    request: Request,
    response: Response,
    role: str,
    query: Callable[[PolicyPrincipal], dict[str, Any]],
) -> dict[str, Any]:
    module = _s08_app_module(request)
    module._s01_disable_cache(response)
    principal = _s08_require_role(request, role)
    try:
        return query(principal)
    except (
        PolicyInvalidTransition,
        PolicyConflict,
        PolicyNotFound,
        PolicyUnavailable,
        RuntimeError,
    ) as error:
        raise _s08_rejected(error) from error


@s08_router.post(
    "/controlled/s08/api/commands/import_legacy",
    response_model=S08ImportLegacyResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s08_import_legacy(
    body: S08ImportLegacyBody, request: Request, response: Response
) -> dict[str, Any]:
    return _s08_command(
        request, response, "admin",
        lambda principal: _s08_service(request).import_legacy(
            principal=principal,
            source_bundle_id=body.source_bundle_id,
            idempotency_key=body.idempotency_key,
            expected_governance_revision=body.expected_governance_revision,
        ),
    )


@s08_router.post(
    "/controlled/s08/api/commands/revise_draft",
    response_model=S08ReviseDraftResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s08_revise_draft(
    body: S08ReviseDraftBody, request: Request, response: Response
) -> dict[str, Any]:
    return _s08_command(
        request, response, "admin",
        lambda principal: _s08_service(request).revise_draft(
            principal=principal,
            draft_id=body.draft_id,
            metadata=body.metadata.model_dump(),
            idempotency_key=body.idempotency_key,
            expected_governance_revision=body.expected_governance_revision,
        ),
    )


@s08_router.post(
    "/controlled/s08/api/commands/freeze_candidate",
    response_model=S08FreezeCandidateResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s08_freeze_candidate(
    body: S08FreezeCandidateBody, request: Request, response: Response
) -> dict[str, Any]:
    return _s08_command(
        request, response, "admin",
        lambda principal: _s08_service(request).freeze_candidate(
            principal=principal,
            draft_id=body.draft_id,
            idempotency_key=body.idempotency_key,
            expected_governance_revision=body.expected_governance_revision,
        ),
    )


@s08_router.post(
    "/controlled/s08/api/commands/request_validation",
    response_model=S08RequestValidationResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s08_request_validation(
    body: S08CandidateCommandBody, request: Request, response: Response
) -> dict[str, Any]:
    return _s08_command(
        request, response, "admin",
        lambda principal: _s08_service(request).request_validation(
            principal=principal,
            candidate_id=body.candidate_id,
            idempotency_key=body.idempotency_key,
            expected_governance_revision=body.expected_governance_revision,
        ),
    )


@s08_router.post(
    "/controlled/s08/api/commands/submit_review",
    response_model=S08SubmitReviewResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s08_submit_review(
    body: S08CandidateCommandBody, request: Request, response: Response
) -> dict[str, Any]:
    return _s08_command(
        request, response, "admin",
        lambda principal: _s08_service(request).submit_review(
            principal=principal,
            candidate_id=body.candidate_id,
            idempotency_key=body.idempotency_key,
            expected_governance_revision=body.expected_governance_revision,
        ),
    )


@s08_router.post(
    "/controlled/s08/api/commands/approve",
    response_model=S08ApproveResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s08_approve(
    body: S08ApproveBody, request: Request, response: Response
) -> dict[str, Any]:
    return _s08_command(
        request, response, "approver",
        lambda principal: _s08_service(request).approve(
            principal=principal,
            candidate_id=body.candidate_id,
            activation_time=body.activation_time,
            recovery_release_id=body.recovery_release_id,
            preview_manifest_id=body.preview_manifest_id,
            idempotency_key=body.idempotency_key,
            expected_governance_revision=body.expected_governance_revision,
        ),
    )


@s08_router.post(
    "/controlled/s08/api/commands/reject",
    response_model=S08RejectResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s08_reject(
    body: S08RejectBody, request: Request, response: Response
) -> dict[str, Any]:
    return _s08_command(
        request, response, "approver",
        lambda principal: _s08_service(request).reject(
            principal=principal,
            candidate_id=body.candidate_id,
            reason_code=body.reason_code,
            idempotency_key=body.idempotency_key,
            expected_governance_revision=body.expected_governance_revision,
        ),
    )


@s08_router.post(
    "/controlled/s08/api/commands/schedule",
    response_model=S08ScheduleResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s08_schedule(
    body: S08ScheduleBody, request: Request, response: Response
) -> dict[str, Any]:
    return _s08_command(
        request, response, "admin",
        lambda principal: _s08_service(request).schedule(
            principal=principal,
            approval_binding_id=body.approval_binding_id,
            activation_at=body.activation_at,
            idempotency_key=body.idempotency_key,
            expected_governance_revision=body.expected_governance_revision,
        ),
    )


@s08_router.post(
    "/controlled/s08/api/commands/stop_activations",
    response_model=S08StopActivationsResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s08_stop_activations(
    body: S08StopActivationsBody, request: Request, response: Response
) -> dict[str, Any]:
    return _s08_command(
        request, response, "operator",
        lambda principal: _s08_service(request).stop_activations(
            principal=principal,
            reason_code=body.reason_code,
            idempotency_key=body.idempotency_key,
            expected_governance_revision=body.expected_governance_revision,
        ),
    )


@s09_router.post(
    "/controlled/s09/api/commands/preview_impact",
    response_model=S09PreviewResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s09_preview_impact(
    body: S09PreviewBody, request: Request, response: Response
) -> dict[str, Any]:
    """The immutable conservative impact preview for a changed governed
    release; the Policy Approver binds its exact digest at approval time.
    Both the Rule Administrator and the Policy Approver may run this
    read-only computation."""
    module = _s08_app_module(request)
    module._s01_disable_cache(response)
    principal = _s08_require_preview_role(request)
    try:
        return _s08_service(request).preview_impact(
            principal=principal,
            candidate_id=body.candidate_id,
            idempotency_key=body.idempotency_key,
            expected_governance_revision=body.expected_governance_revision,
        )
    except (
        PolicyInvalidTransition,
        PolicyConflict,
        PolicyNotFound,
        PolicyUnavailable,
        RuntimeError,
    ) as error:
        raise _s08_rejected(error) from error


@s09_router.post(
    "/controlled/s09/api/commands/impose_hold",
    response_model=S09ImposeHoldResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s09_impose_hold(
    body: S09ImposeHoldBody, request: Request, response: Response
) -> dict[str, Any]:
    """Impose a scoped Policy Safety Hold: automatic routing, new RunSpec
    publication and current completion fail closed until an explicit
    governed recovery releases the hold."""
    return _s08_command(
        request, response, "operator",
        lambda principal: _s08_service(request).impose_hold(
            principal=principal,
            reason_code=body.reason_code,
            hold_scope=body.hold_scope,
            idempotency_key=body.idempotency_key,
            expected_governance_revision=body.expected_governance_revision,
        ),
    )


@s09_router.post(
    "/controlled/s09/api/commands/propose_rollback",
    response_model=S09ProposeRollbackResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s09_propose_rollback(
    body: S09ProposeRollbackBody, request: Request, response: Response
) -> dict[str, Any]:
    """Revalidate the exact historical release and stage a fresh rollback
    candidate; an incompatible rollback keeps the hold and requires a
    governed forward fix."""
    return _s08_command(
        request, response, "operator",
        lambda principal: _s08_service(request).propose_rollback(
            principal=principal,
            release_candidate_id=body.release_candidate_id,
            reason_code=body.reason_code,
            idempotency_key=body.idempotency_key,
            expected_governance_revision=body.expected_governance_revision,
        ),
    )


@s09_router.post(
    "/controlled/s09/api/commands/replay",
    response_model=S09ReplayResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s09_replay(
    body: S09ReplayBody, request: Request, response: Response
) -> dict[str, Any]:
    """Isolated reproduction replay with a namespaced replay identity and
    its own command/result DTO: read-only, zero business revisions, one
    explicit application."""
    return _s08_command(
        request, response, "replay_operator",
        lambda principal: _s08_service(request).replay_release(
            principal=principal,
            release_candidate_id=body.release_candidate_id,
            application_id=body.application_id,
            idempotency_key=body.idempotency_key,
            expected_governance_revision=body.expected_governance_revision,
        ),
    )


@s09_router.post(
    "/controlled/s09/api/commands/simulate",
    response_model=S09SimulationResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s09_simulate(
    body: S09SimulationBody, request: Request, response: Response
) -> dict[str, Any]:
    """Isolated counterfactual simulation with a namespaced simulation
    identity and its own command/result DTO: read-only, zero business
    revisions, never current, one explicit application."""
    return _s08_command(
        request, response, "simulation_operator",
        lambda principal: _s08_service(request).simulate_release(
            principal=principal,
            release_candidate_id=body.release_candidate_id,
            application_id=body.application_id,
            idempotency_key=body.idempotency_key,
            expected_governance_revision=body.expected_governance_revision,
        ),
    )


@s09_router.post(
    "/controlled/s09/api/commands/recover_hold",
    response_model=S09RecoverHoldResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s09_recover_hold(
    body: S09RecoverHoldBody, request: Request, response: Response
) -> dict[str, Any]:
    """The separate, idempotent, separation-of-duties hold recovery: only
    the independent Policy Approver may confirm release of a hold imposed
    by the activation operator."""
    return _s08_command(
        request, response, "approver",
        lambda principal: _s08_service(request).recover_hold(
            principal=principal,
            hold_id=body.hold_id,
            recovery_generation=body.recovery_generation,
            idempotency_key=body.idempotency_key,
            expected_governance_revision=body.expected_governance_revision,
        ),
    )


@s08_router.post(
    "/controlled/s08/api/commands/cancel",
    response_model=S08CancelResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s08_cancel(
    body: S08RejectBody, request: Request, response: Response
) -> dict[str, Any]:
    return _s08_command(
        request, response, "admin",
        lambda principal: _s08_service(request).cancel(
            principal=principal,
            candidate_id=body.candidate_id,
            reason_code=body.reason_code,
            idempotency_key=body.idempotency_key,
            expected_governance_revision=body.expected_governance_revision,
        ),
    )


@s08_router.get(
    "/controlled/s08/api/queries/status",
    response_model=S08StatusResponse,
    response_model_exclude_none=True,
    responses=_S08_ERROR_RESPONSES,
)
def s08_query_status(request: Request, response: Response) -> dict[str, Any]:
    return _s08_query(request, response, "admin", _s08_service(request).query_status)


@s08_router.get(
    "/controlled/s08/api/queries/active",
    response_model=S08ActiveResponse,
    response_model_exclude_none=True,
    responses=_S08_ERROR_RESPONSES,
)
def s08_query_active(request: Request, response: Response) -> dict[str, Any]:
    return _s08_query(request, response, "admin", _s08_service(request).query_active)


@s08_router.get(
    "/controlled/s08/api/queries/candidates",
    response_model=S08CandidatesResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s08_query_candidates(request: Request, response: Response) -> dict[str, Any]:
    return _s08_query(request, response, "admin", _s08_service(request).query_candidates)


@s08_router.get(
    "/controlled/s08/api/queries/drafts",
    response_model=S08DraftsResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s08_query_drafts(request: Request, response: Response) -> dict[str, Any]:
    return _s08_query(request, response, "admin", _s08_service(request).query_drafts)


@s08_router.get(
    "/controlled/s08/api/queries/events",
    response_model=S08EventsResponse,
    responses=_S08_ERROR_RESPONSES,
)
def s08_query_events(request: Request, response: Response) -> dict[str, Any]:
    return _s08_query(request, response, "admin", _s08_service(request).query_events)


def _s08_require_read_role(request: Request) -> PolicyPrincipal:
    """Minimal read access for both the Rule Administrator and the
    independent Policy Approver; mutation stays role-separated.  The
    credential is probed first so the approver path is not rejected by
    the admin branch's 403."""
    module = _s08_app_module(request)
    credentials = {
        "admin": (module.S08_ADMIN_CREDENTIAL, module.S08_ADMIN_SUBJECT),
        "approver": (module.S08_APPROVER_CREDENTIAL, module.S08_APPROVER_SUBJECT),
    }
    for role, (credential, subject) in credentials.items():
        if subject and module._s01_has_credential(request, credential):
            return PolicyPrincipal(
                subject=subject,
                role=role,
                scope=S08_SCOPE,
                source_id="s08-web-bearer",
            )
    raise HTTPException(
        403,
        detail={
            "error": "S08_FORBIDDEN",
            "message": "Registered S08 identity required",
        },
    )


def _s09_require_read_role(request: Request) -> PolicyPrincipal:
    """The T09 read surface for the four governance workspace roles: the
    Rule Administrator, the Policy Approver, the activation Operator and
    the Auditor each read the atomic workspace under their own identity.
    Every other identity fails closed with the stable 403."""
    module = _s08_app_module(request)
    credentials = {
        "admin": (module.S08_ADMIN_CREDENTIAL, module.S08_ADMIN_SUBJECT),
        "approver": (module.S08_APPROVER_CREDENTIAL, module.S08_APPROVER_SUBJECT),
        "operator": (module.S08_OPERATOR_CREDENTIAL, module.S08_OPERATOR_SUBJECT),
        "auditor": (
            getattr(module, "S01_AUDITOR_CREDENTIAL", ""),
            getattr(module, "S01_AUDITOR_SUBJECT", ""),
        ),
    }
    for role, (credential, subject) in credentials.items():
        if subject and module._s01_has_credential(request, credential):
            return PolicyPrincipal(
                subject=subject,
                role=role,
                scope=S08_SCOPE,
                source_id="s08-web-bearer",
            )
    raise HTTPException(
        403,
        detail={
            "error": "S08_FORBIDDEN",
            "message": "Registered S08 identity required",
        },
    )


@s08_router.get(
    "/controlled/s08/api/queries/candidate/{candidate_id}",
    response_model=S08CandidateWorkspaceResponse,
    response_model_exclude_none=True,
    responses=_S08_ERROR_RESPONSES,
)
def s08_query_candidate(
    candidate_id: str, request: Request, response: Response
) -> dict[str, Any]:
    """The closed T08 candidate workspace.

    The service builds the whole snapshot atomically under one lock: the
    authoritative governance revision (from the governance ledger), the
    authenticated actor role, the server-owned action list (candidate status
    + role only), the current recovery/active anchor, the candidate's event
    timeline and the validation/activation job outcomes.  This adapter only
    maps that snapshot into the closed DTO and owns no transition rules.
    """
    module = _s08_app_module(request)
    module._s01_disable_cache(response)
    principal = _s08_require_read_role(request)
    try:
        workspace = _s08_service(request).query_candidate_workspace(
            principal, candidate_id
        )
    except (
        PolicyInvalidTransition,
        PolicyConflict,
        PolicyNotFound,
        PolicyUnavailable,
        RuntimeError,
    ) as error:
        raise _s08_rejected(error) from error
    return workspace


@s08_router.get("/controlled/s08/react", response_class=HTMLResponse)
def controlled_s08_react_page(request: Request) -> HTMLResponse:
    """The Rule Administrator / Policy Approver React shell: the same built
    artifact as the other controlled shells, served only to a registered S08
    identity (admin or approver) with no-store and no session.  A missing or
    incomplete build is an explicit closed 503; the S08 API remains the sole
    authority."""
    module = _s08_app_module(request)
    _s08_require_read_role(request)
    index_html = module._react_shell_index_html()
    if index_html is None:
        raise HTTPException(
            503,
            detail={
                "error": "S08_REACT_UNAVAILABLE",
                "message": "Controlled S08 React shell is not built",
            },
        )
    _s08_service(request)
    response = HTMLResponse(index_html)
    module._s01_disable_cache(response)
    return response


@s09_router.get(
    "/controlled/s09/api/queries/workspace",
    response_model=S09GovernanceWorkspaceResponse,
    response_model_exclude_none=True,
    responses=_S08_ERROR_RESPONSES,
)
def s09_query_workspace(request: Request, response: Response) -> dict[str, Any]:
    """The closed T09 governance workspace.

    The service builds the whole snapshot atomically under one lock: the
    authoritative governance revision (from the governance ledger), the
    authenticated actor role, the server-owned action list (role + ledger
    state only), the active release and the recorded known-good recovery
    anchor, the active hold union and the append-only S09 event refs.  This
    adapter only maps that snapshot into the closed DTO and owns no
    transition rules.
    """
    module = _s08_app_module(request)
    module._s01_disable_cache(response)
    # Authority availability is checked before identity: a missing governance
    # authority is a closed 503 for every caller, never a misleading 403.
    service = _s08_service(request)
    principal = _s09_require_read_role(request)
    try:
        return service.query_governance_workspace(principal)
    except (
        PolicyInvalidTransition,
        PolicyConflict,
        PolicyNotFound,
        PolicyUnavailable,
        RuntimeError,
    ) as error:
        raise _s08_rejected(error) from error


@s09_router.get("/controlled/s09/react", response_class=HTMLResponse)
def controlled_s09_react_page(request: Request) -> HTMLResponse:
    """The T09 governance workspace React shell: the same built artifact as
    the other controlled shells, served only to a registered T09 identity
    (admin, approver, operator or auditor) with no-store and no session.  A
    missing or incomplete build is an explicit closed 503; the S09 API
    remains the sole authority."""
    module = _s08_app_module(request)
    _s08_service(request)
    _s09_require_read_role(request)
    index_html = module._react_shell_index_html()
    if index_html is None:
        raise HTTPException(
            503,
            detail={
                "error": "S09_REACT_UNAVAILABLE",
                "message": "Controlled S09 React shell is not built",
            },
        )
    response = HTMLResponse(index_html)
    module._s01_disable_cache(response)
    return response
