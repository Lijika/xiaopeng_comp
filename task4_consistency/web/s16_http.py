"""S16 governed-deletion HTTP adapter (Ticket #32).

The typed data-governance surface for the S16 plane: preflight, approval,
cancel, commit, repair, query, receipt and the two shell routes.  The
module owns no process-level authority construction: ``web/app.py`` builds
``S16_SERVICE`` (with the registered owners and identities) and registers
the router through ``register_router``.  Missing S16 configuration leaves
every other plane available while every S16 route reports scoped
unavailability (``S16_UNAVAILABLE``).

Every command is bound to subject, role, scope, action and request id;
every response is no-store; unknown requests and applications share one
existence-hiding 404; the worker surface is not user-triggered — the
registered process endpoint runs exactly one bounded attempt under the
governance identity.
"""

from __future__ import annotations

import hmac
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from task4_consistency.controlled.s16 import (
    S16Blocked,
    S16Conflict,
    S16Forbidden,
    S16NotFound,
    S16Unavailable,
)

s16_router = APIRouter()

_S16_APP_MODULE_STATE_KEY = "_s16_application_module"
_S16_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}


def _s16_app_module(request: Request) -> Any:
    module = getattr(request.app.state, _S16_APP_MODULE_STATE_KEY, None)
    if module is None:
        raise HTTPException(
            503,
            detail={
                "error": "S16_UNAVAILABLE",
                "message": "Controlled S16 governed-deletion plane is unavailable",
            },
            headers=_S16_NO_STORE_HEADERS,
        )
    return module


def register_router(app: FastAPI, app_module: Any) -> None:
    """Bind the live application module on the app and register S16 routes.

    Idempotent for the same (app, module); a conflicting module is rejected
    before any state or route change.
    """
    existing = getattr(app.state, _S16_APP_MODULE_STATE_KEY, None)
    if existing is not None:
        if existing is not app_module:
            raise RuntimeError(
                "S16 router already registered with a different application module"
            )
        return
    setattr(app.state, _S16_APP_MODULE_STATE_KEY, app_module)
    app.include_router(s16_router)


def _s16_service(request: Request) -> Any:
    module = _s16_app_module(request)
    service = getattr(module, "S16_SERVICE", None)
    if service is None:
        raise HTTPException(
            503,
            detail={
                "error": "S16_UNAVAILABLE",
                "message": "Controlled S16 governed-deletion plane is unavailable",
            },
            headers=_S16_NO_STORE_HEADERS,
        )
    return service


def _s16_governance_principal(request: Request) -> Any:
    """The one registered data-governance owner identity for the S16 plane."""
    module = _s16_app_module(request)
    service = _s16_service(request)
    credential = getattr(module, "S16_GOVERNANCE_CREDENTIAL", "")
    subject = getattr(module, "S16_GOVERNANCE_SUBJECT", "")
    if not subject or not module._s01_has_credential(request, credential):  # type: ignore[attr-defined]
        raise HTTPException(
            403,
            detail={
                "error": "S16_FORBIDDEN",
                "message": "Registered S16 governance owner identity required",
            },
            headers=_S16_NO_STORE_HEADERS,
        )
    scope = getattr(module, "S16_GOVERNANCE_SCOPE", "C-DEMO")
    principal_type = getattr(module, "S01CommandPrincipal", None)
    if principal_type is None or not isinstance(scope, str) or not scope:
        raise HTTPException(
            503,
            detail={
                "error": "S16_UNAVAILABLE",
                "message": "Controlled S16 governance identity is unavailable",
            },
            headers=_S16_NO_STORE_HEADERS,
        )
    return principal_type(
        subject=subject,
        role="operator",
        scope=scope,
        source_id="s16-governance-console",
        expires_at=float("inf"),
    )


def _s16_approver_principal(request: Request) -> Any:
    """One of the two registered early-deletion approver identities."""
    module = _s16_app_module(request)
    _s16_service(request)
    # The approver presents her own credential either on the standard
    # Authorization header (API clients) or on the dedicated approver-token
    # header (the governed panel: the browser context already carries the
    # governance identity on Authorization, so the approver identity needs
    # its own transport slot).  Both are plain bearer transports for the
    # same registered approver identities.
    provided = request.headers.get("X-S16-Approver-Token", "") or _s16_bearer_credential(
        request
    )
    for credential_key, subject_key in (
        ("S16_APPROVER1_CREDENTIAL", "S16_APPROVER1_SUBJECT"),
        ("S16_APPROVER2_CREDENTIAL", "S16_APPROVER2_SUBJECT"),
    ):
        credential = getattr(module, credential_key, "")
        subject = getattr(module, subject_key, "")
        if (
            subject
            and provided
            and hmac.compare_digest(provided, credential)
        ):
            scope = getattr(module, "S16_GOVERNANCE_SCOPE", "C-DEMO")
            principal_type = getattr(module, "S01CommandPrincipal", None)
            if principal_type is None:
                raise HTTPException(
                    503,
                    detail={
                        "error": "S16_UNAVAILABLE",
                        "message": "Controlled S16 approver identity is unavailable",
                    },
                    headers=_S16_NO_STORE_HEADERS,
                )
            return principal_type(
                subject=subject,
                role="operator",
                scope=scope,
                source_id="s16-approval-desk",
                expires_at=float("inf"),
            )
    raise HTTPException(
        403,
        detail={
            "error": "S16_FORBIDDEN",
            "message": "Registered S16 deletion approver identity required",
        },
        headers=_S16_NO_STORE_HEADERS,
    )


def _s16_bearer_credential(request: Request) -> str:
    scheme, separator, supplied = request.headers.get(
        "Authorization", ""
    ).partition(" ")
    if scheme.lower() != "bearer" or not separator:
        return ""
    return supplied


def _s16_no_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


# ------------------------------------------------------------------ DTOs


class S16ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: str
    reason_code: str | None = None
    message: str | None = None
    hint: str | None = None


class S16ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: S16ErrorDetail


class S16ValidationErrorItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loc: list[str | int]
    msg: str
    type: str


class S16ValidationErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: list[S16ValidationErrorItem]


_S16_ERROR_RESPONSES: dict[int, dict[str, Any]] = {
    403: {"model": S16ErrorResponse},
    404: {"model": S16ErrorResponse},
    409: {"model": S16ErrorResponse},
    422: {"model": S16ValidationErrorResponse},
    503: {"model": S16ErrorResponse},
}


class S16PreflightBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_reference: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)


class S16ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=200)


class S16CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=200)


class S16CommitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=200)


class S16RepairBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_id: str = Field(min_length=1, max_length=40)
    repair_fact: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)


class S16ManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_id: str
    copy_class: str
    classification: str
    content_sha256: str = Field(min_length=64, max_length=64)
    identity_fingerprint: str = Field(min_length=64, max_length=64)
    retention_policy_id: str
    retention_policy_version: str
    retention_due_at: int | None = None
    legal_hold_generation: int = Field(ge=0)
    hold_state: str
    shared_state: str
    planned_action: str
    count: int = Field(ge=0)


class S16PreflightResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    request_id: str
    application_reference: str
    scope_fingerprint: str = Field(min_length=64, max_length=64)
    manifest_digest: str = Field(min_length=64, max_length=64)
    entries_digest: str = Field(min_length=64, max_length=64)
    owner_registry_digest: str = Field(min_length=64, max_length=64)
    s01_revision: int = Field(ge=0)
    s12_revision: str = Field(min_length=64, max_length=64)
    policy_digest: str = Field(min_length=64, max_length=64)
    retention_due: int | None = None
    early_deletion: bool
    retained_scan_clean: bool
    entries: tuple[S16ManifestEntry, ...]
    replayed: bool = False


class S16CommandResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    request_id: str | None = None
    job_id: str | None = None
    hold_id: str | None = None
    approved_by: str | None = None
    reason_code: str | None = None
    replayed: bool = False


class S16StableFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_id: str
    reason_code: str
    responsible_party: str
    recovery_action: str
    attempt: int = Field(ge=0)


class S16ApprovalSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_fingerprint: str = Field(min_length=64, max_length=64)
    manifest_digest: str = Field(min_length=64, max_length=64)
    appended_at: int = Field(ge=0)


class S16LegalHoldSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hold_id: str
    generation: int = Field(ge=1)
    reason_code: str
    owner: str
    effective_time: int = Field(ge=0)
    expiry: int | None = None
    released: bool


class S16JobSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: str
    attempt: int = Field(ge=0)
    fence: int = Field(ge=0)
    lease_owner: str | None = None
    pending_owner_fingerprints: dict[str, int]
    owner_results: dict[str, str]
    stable_failure: S16StableFailure | None = None
    completed_at: int | None = None


class S16QueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["s16-query/1"]
    request_id: str
    scope_fingerprint: str = Field(min_length=64, max_length=64)
    manifest_digest: str = Field(min_length=64, max_length=64)
    owner_registry_digest: str = Field(min_length=64, max_length=64)
    s01_revision: int = Field(ge=0)
    s12_revision: str = Field(min_length=64, max_length=64)
    policy_digest: str = Field(min_length=64, max_length=64)
    retention_due: int | None = None
    early_deletion: bool
    cancelled: bool
    approvals: tuple[S16ApprovalSummary, ...]
    legal_holds: tuple[S16LegalHoldSummary, ...]
    job: S16JobSummary | None = None


class S16ReceiptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    receipt_id: str
    schema_version: str
    action: str
    policy: str
    scope_fingerprint: str = Field(min_length=64, max_length=64)
    completed_at: int = Field(ge=0)
    authority: str
    result: str
    owner_counts: dict[str, int]
    restore_replay_status: str
    subject: str
    role: str


class S16ProcessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    job_id: str | None = None
    request_id: str | None = None
    reason_code: str | None = None
    owner_id: str | None = None
    attempt: int | None = None


# ------------------------------------------------------------------ Routes


def _s16_http_error(error: Exception) -> HTTPException:
    if isinstance(error, S16Forbidden):
        return HTTPException(
            403,
            detail={
                "error": "S16_FORBIDDEN",
                "message": "Registered S16 governed-deletion identity required",
            },
        )
    if isinstance(error, S16NotFound):
        return HTTPException(
            404,
            detail={
                "error": "S16_NOT_FOUND",
                "message": "S16 request or application is unavailable",
            },
        )
    if isinstance(error, S16Conflict):
        return HTTPException(
            409,
            detail={
                "error": "S16_CONFLICT",
                "reason_code": str(error),
                "message": "S16 command conflicts with the committed state",
            },
        )
    if isinstance(error, S16Blocked):
        return HTTPException(
            409,
            detail={
                "error": "S16_BLOCKED",
                "reason_code": error.reason_code,
                "message": "S16 command is blocked by a registered gate",
            },
        )
    if isinstance(error, S16Unavailable):
        return HTTPException(
            503,
            detail={
                "error": "S16_UNAVAILABLE",
                "message": "Controlled S16 governed-deletion plane is unavailable",
            },
        )
    return HTTPException(
        422,
        detail={
            "error": "S16_INVALID_COMMAND",
            "message": "S16 command does not match the registered contract",
        },
    )


@s16_router.post(
    "/controlled/s16/api/deletions/preflight",
    response_model=S16PreflightResponse,
    responses=_S16_ERROR_RESPONSES,
)
def s16_preflight(
    body: S16PreflightBody,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s16_no_cache(response)
    principal = _s16_governance_principal(request)
    service = _s16_service(request)
    try:
        result = service.preflight(
            application_reference=body.application_reference,
            principal=principal,
            idempotency_key=body.idempotency_key,
        )
    except Exception as error:
        raise _s16_http_error(error) from error
    return result


@s16_router.post(
    "/controlled/s16/api/deletions/{request_id}/approve",
    response_model=S16CommandResponse,
    responses=_S16_ERROR_RESPONSES,
)
def s16_approve(
    request_id: str,
    body: S16ApproveBody,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s16_no_cache(response)
    principal = _s16_approver_principal(request)
    service = _s16_service(request)
    try:
        result = service.approve(
            request_id=request_id,
            manifest_digest=body.manifest_digest,
            principal=principal,
            idempotency_key=body.idempotency_key,
        )
    except Exception as error:
        raise _s16_http_error(error) from error
    return {
        "status": result.get("status") or "accepted",
        "request_id": result.get("request_id"),
        "approved_by": result.get("approved_by"),
        "replayed": bool(result.get("replayed")),
    }


@s16_router.post(
    "/controlled/s16/api/deletions/{request_id}/cancel",
    response_model=S16CommandResponse,
    responses=_S16_ERROR_RESPONSES,
)
def s16_cancel(
    request_id: str,
    body: S16CancelBody,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s16_no_cache(response)
    principal = _s16_governance_principal(request)
    service = _s16_service(request)
    try:
        result = service.cancel(
            request_id=request_id,
            principal=principal,
            idempotency_key=body.idempotency_key,
        )
    except Exception as error:
        raise _s16_http_error(error) from error
    return {
        "status": result.get("status") or "accepted",
        "request_id": result.get("request_id"),
        "replayed": bool(result.get("replayed")),
    }


@s16_router.post(
    "/controlled/s16/api/deletions/{request_id}/commit",
    response_model=S16CommandResponse,
    responses=_S16_ERROR_RESPONSES,
)
def s16_commit(
    request_id: str,
    body: S16CommitBody,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s16_no_cache(response)
    principal = _s16_governance_principal(request)
    service = _s16_service(request)
    try:
        result = service.commit(
            request_id=request_id,
            principal=principal,
            idempotency_key=body.idempotency_key,
        )
    except Exception as error:
        raise _s16_http_error(error) from error
    return {
        "status": result.get("status") or "accepted",
        "request_id": result.get("request_id"),
        "job_id": result.get("job_id"),
        "replayed": bool(result.get("replayed")),
    }


@s16_router.post(
    "/controlled/s16/api/deletions/{request_id}/repair",
    response_model=S16CommandResponse,
    responses=_S16_ERROR_RESPONSES,
)
def s16_repair(
    request_id: str,
    body: S16RepairBody,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s16_no_cache(response)
    principal = _s16_governance_principal(request)
    service = _s16_service(request)
    try:
        result = service.repair(
            request_id=request_id,
            owner_id=body.owner_id,
            repair_fact=body.repair_fact,
            principal=principal,
            idempotency_key=body.idempotency_key,
        )
    except Exception as error:
        raise _s16_http_error(error) from error
    return {
        "status": result.get("status") or "accepted",
        "request_id": result.get("request_id"),
        "job_id": result.get("job_id"),
        "replayed": bool(result.get("replayed")),
    }


@s16_router.get(
    "/controlled/s16/api/deletions/{request_id}",
    response_model=S16QueryResponse,
    responses=_S16_ERROR_RESPONSES,
)
def s16_query(
    request_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s16_no_cache(response)
    principal = _s16_governance_principal(request)
    service = _s16_service(request)
    try:
        return service.query(request_id=request_id, principal=principal)
    except Exception as error:
        raise _s16_http_error(error) from error


@s16_router.get(
    "/controlled/s16/api/deletions/{request_id}/receipt",
    response_model=S16ReceiptResponse,
    responses=_S16_ERROR_RESPONSES,
)
def s16_receipt(
    request_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s16_no_cache(response)
    principal = _s16_governance_principal(request)
    service = _s16_service(request)
    try:
        return service.receipt(request_id=request_id, principal=principal)
    except Exception as error:
        raise _s16_http_error(error) from error


@s16_router.post(
    "/controlled/s16/api/process",
    response_model=S16ProcessResponse,
    responses=_S16_ERROR_RESPONSES,
)
def s16_process_next(
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """The registered process seam: exactly one bounded worker attempt under
    the governance identity.  No ordinary user can trigger a hard delete."""
    _s16_no_cache(response)
    _s16_governance_principal(request)
    service = _s16_service(request)
    try:
        result = service.process_next_deletion_job()
    except Exception as error:
        raise _s16_http_error(error) from error
    return {
        "status": result.get("status") or "idle",
        "job_id": result.get("job_id"),
        "request_id": result.get("request_id"),
        "reason_code": result.get("reason_code"),
        "owner_id": result.get("owner_id"),
        "attempt": result.get("attempt"),
    }
