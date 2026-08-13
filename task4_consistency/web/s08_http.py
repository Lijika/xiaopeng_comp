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
    }
    credential, subject = credentials.get(expected, ("", ""))
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


class S08StatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track: str
    capability_gate: str
    scope: str
    governance_revision: int
    active_generation: int | None = None
    bootstrap: bool
    activation_hold: S08ActivationHold | None = None
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


class S08ApprovalBinding(BaseModel):
    """The fixed approver binding: the exact candidate/validation digests,
    the bound diff, scope, activation time and recovery release."""

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
