"""Typed HTTP adapter for the S17 governed export plane."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from task4_consistency.controlled.s01 import S01CommandPrincipal
from task4_consistency.controlled.s17 import (
    GovernedExportService,
    S17Blocked,
    S17Forbidden,
    S17NotFound,
    S17Unavailable,
)

s17_router = APIRouter()
_STATE_KEY = "_s17_application_module"
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class S17ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    error: str
    reason_code: str | None = None
    message: str | None = None


class S17ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail: S17ErrorDetail


class S17ValidationErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail: list[dict[str, Any]]


class S17PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: str = Field(min_length=1, max_length=80)
    fields: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    recipient_id: str = Field(min_length=1, max_length=200)
    classification: str = Field(min_length=1, max_length=40)
    expiry: int = Field(ge=1)
    scope_reference: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)


class S17ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=200)


class S17CommitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    idempotency_key: str = Field(min_length=1, max_length=200)


class S17AccessBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=512)


class S17CommandResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    request_id: str | None = None
    job_id: str | None = None
    package_id: str | None = None
    receipt_id: str | None = None
    attempt: int | None = None
    reason_code: str | None = None
    replayed: bool = False


class S17PreviewResponse(S17CommandResponse):
    preview_digest: str
    scope_fingerprint: str | None = None
    field_count: int | None = None
    artifact_count: int | None = None
    watermark_plan: Any | None = None


_S17_PREVIEW_FIELDS = frozenset(S17PreviewResponse.model_fields)


class S17QueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str
    request_id: str
    status: str
    preview_digest: str | None = None
    scope_fingerprint: str | None = None
    fields: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    scope_reference: str | None = None
    purpose: str | None = None
    recipient_id: str | None = None
    classification: str | None = None
    expiry: int | None = None
    source_revisions: dict[str, Any] = Field(default_factory=dict)
    policy_digest: str | None = None
    package_id: str | None = None
    package_digest: str | None = None
    watermark_id: str | None = None
    delivery_status: str | None = None
    attempt: int = 0
    operation_id: str | None = None


class S17ReceiptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str
    receipt_id: str
    status: str
    request_fingerprint: str | None = None
    package_digest: str | None = None
    delivery_status: str | None = None
    attempt: int = 0
    expiry: int | None = None
    cleanup_result: str | None = None
    replayed: bool = False


_ERRORS = {code: {"model": S17ErrorResponse} for code in (403, 404, 409, 422, 503)}


def register_router(app: FastAPI, app_module: Any) -> None:
    existing = getattr(app.state, _STATE_KEY, None)
    if existing is not None and existing is not app_module:
        raise RuntimeError("S17 router already registered with a different application module")
    if existing is None:
        setattr(app.state, _STATE_KEY, app_module)
        app.include_router(s17_router)


def _module(request: Request) -> Any:
    module = getattr(request.app.state, _STATE_KEY, None)
    if module is None:
        raise HTTPException(503, detail={"error": "S17_UNAVAILABLE", "message": "Controlled S17 plane is unavailable"}, headers=_NO_STORE)
    return module


def _configuration_unavailable(module: Any) -> HTTPException:
    return HTTPException(
        503,
        detail={
            "error": "S17_UNAVAILABLE",
            "reason_code": "S17_CONFIGURATION_ERROR",
            "message": "Controlled S17 plane is unavailable",
        },
        headers=_NO_STORE,
    )


def _bearer(request: Request, header: str = "Authorization") -> str:
    scheme, sep, value = request.headers.get(header, "").partition(" ")
    return value if sep and scheme.lower() == "bearer" else ""


def _principal(request: Request, kind: str) -> S01CommandPrincipal:
    module = _module(request)
    prefix = {"requester": "S17_REQUESTER", "approver": "S17_APPROVER", "worker": "S17_WORKER", "recipient": "S17_RECIPIENT"}[kind]
    credential = str(getattr(module, f"{prefix}_CREDENTIAL", ""))
    subject = str(getattr(module, f"{prefix}_SUBJECT", ""))
    supplied = _bearer(request, "X-S17-Approver-Token" if kind == "approver" else "Authorization")
    if not supplied or not credential or not hmac.compare_digest(supplied, credential):
        raise HTTPException(403, detail={"error": "S17_FORBIDDEN", "message": "Registered S17 identity required"}, headers=_NO_STORE)
    scope = str(getattr(module, "S17_EXPORT_SCOPE", "C-DEMO"))
    return S01CommandPrincipal(subject=subject, role={"requester": "operator", "approver": "operator", "worker": "system", "recipient": "recipient"}[kind], scope=scope, source_id={"requester": "s17-export-console", "approver": "s17-approval-desk", "worker": "s17-export-worker", "recipient": "s17-recipient-channel"}[kind], expires_at=float("inf"))


def _service(request: Request) -> GovernedExportService:
    module = _module(request)
    service = getattr(module, "S17_SERVICE", None)
    if service is None:
        raise _configuration_unavailable(module)
    return service


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, S17Forbidden):
        code, status, message = "S17_FORBIDDEN", 403, "Registered S17 identity required"
    elif isinstance(exc, S17NotFound):
        code, status, message = "S17_NOT_FOUND", 404, "S17 export is unavailable"
    elif isinstance(exc, S17Blocked):
        code, status, message = "S17_BLOCKED", 409, "S17 command is blocked by a registered gate"
        return HTTPException(status, detail={"error": code, "reason_code": exc.reason_code, "message": message}, headers=_NO_STORE)
    elif isinstance(exc, S17Unavailable):
        code, status, message = "S17_UNAVAILABLE", 503, "Controlled S17 plane is unavailable"
    else:
        code, status, message = "S17_INVALID_COMMAND", 422, "S17 command does not match the registered contract"
    return HTTPException(status, detail={"error": code, "message": message}, headers=_NO_STORE)


def _nocache(response: Response) -> None:
    response.headers.update(_NO_STORE)


@s17_router.post("/controlled/s17/api/exports/preview", response_model=S17PreviewResponse, responses=_ERRORS)
def s17_preview(body: S17PreviewBody, request: Request, response: Response) -> dict[str, Any]:
    _nocache(response)
    try:
        p = _principal(request, "requester")
        result = _service(request).preview(**body.model_dump(), principal=p)
        return {key: result[key] for key in _S17_PREVIEW_FIELDS if key in result}
    except Exception as exc:
        raise _error(exc) from exc


@s17_router.post("/controlled/s17/api/exports/{request_id}/approve", response_model=S17CommandResponse, responses=_ERRORS)
def s17_approve(request_id: str, body: S17ApproveBody, request: Request, response: Response) -> dict[str, Any]:
    _nocache(response)
    try:
        return _service(request).approve(request_id=request_id, principal=_principal(request, "approver"), **body.model_dump())
    except Exception as exc:
        raise _error(exc) from exc


@s17_router.post("/controlled/s17/api/exports/{request_id}/commit", response_model=S17CommandResponse, responses=_ERRORS)
def s17_commit(request_id: str, body: S17CommitBody, request: Request, response: Response) -> dict[str, Any]:
    _nocache(response)
    try:
        return _service(request).commit(request_id=request_id, principal=_principal(request, "requester"), **body.model_dump())
    except Exception as exc:
        raise _error(exc) from exc


@s17_router.post("/controlled/s17/api/process", response_model=S17CommandResponse, responses=_ERRORS)
def s17_process(request: Request, response: Response) -> dict[str, Any]:
    _nocache(response)
    try:
        return _service(request).process_next_export(principal=_principal(request, "worker"))
    except Exception as exc:
        raise _error(exc) from exc


@s17_router.get("/controlled/s17/api/exports/{request_id}", response_model=S17QueryResponse, responses=_ERRORS)
def s17_query(request_id: str, request: Request, response: Response) -> dict[str, Any]:
    _nocache(response)
    try:
        module = _module(request)
        credential = _bearer(request)
        kind = "approver" if credential and hmac.compare_digest(credential, str(getattr(module, "S17_APPROVER_CREDENTIAL", ""))) else "requester"
        return _service(request).query(request_id=request_id, principal=_principal(request, kind))
    except Exception as exc:
        raise _error(exc) from exc


@s17_router.post("/controlled/s17/api/exports/{request_id}/access", response_model=S17CommandResponse, responses=_ERRORS)
def s17_access(request_id: str, body: S17AccessBody, request: Request, response: Response) -> dict[str, Any]:
    _nocache(response)
    try:
        return _service(request).access(request_id=request_id, principal=_principal(request, "recipient"), token=body.token)
    except Exception as exc:
        raise _error(exc) from exc


@s17_router.post("/controlled/s17/api/exports/{request_id}/confirm", response_model=S17CommandResponse, responses=_ERRORS)
def s17_confirm(request_id: str, request: Request, response: Response) -> dict[str, Any]:
    _nocache(response)
    try:
        return _service(request).confirm(request_id=request_id, principal=_principal(request, "recipient"), idempotency_key=request.headers.get("Idempotency-Key", "confirm"))
    except Exception as exc:
        raise _error(exc) from exc


@s17_router.post("/controlled/s17/api/exports/{request_id}/revoke", response_model=S17CommandResponse, responses=_ERRORS)
def s17_revoke(request_id: str, request: Request, response: Response) -> dict[str, Any]:
    _nocache(response)
    try:
        return _service(request).revoke(request_id=request_id, principal=_principal(request, "requester"), idempotency_key=request.headers.get("Idempotency-Key", "revoke"))
    except Exception as exc:
        raise _error(exc) from exc


@s17_router.post("/controlled/s17/api/exports/{request_id}/expire", response_model=S17CommandResponse, responses=_ERRORS)
def s17_expire(request_id: str, request: Request, response: Response) -> dict[str, Any]:
    _nocache(response)
    try:
        return _service(request).expire(request_id=request_id, principal=_principal(request, "worker"), idempotency_key=request.headers.get("Idempotency-Key", "expire"))
    except Exception as exc:
        raise _error(exc) from exc


@s17_router.get("/controlled/s17/api/exports/{request_id}/receipt", response_model=S17ReceiptResponse, responses=_ERRORS)
def s17_receipt(request_id: str, request: Request, response: Response) -> dict[str, Any]:
    _nocache(response)
    try:
        return _service(request).receipt(request_id=request_id, principal=_principal(request, "requester"))
    except Exception as exc:
        raise _error(exc) from exc


def _shell(request: Request) -> Response:
    _principal(request, "requester")
    _service(request)
    module = _module(request)
    index = getattr(module, "S17_REACT_INDEX", None)
    if index is None:
        return JSONResponse(503, {"detail": {"error": "S17_REACT_UNAVAILABLE", "message": "Controlled S17 React shell is not built"}}, headers=_NO_STORE)
    try:
        return HTMLResponse(index.read_text(encoding="utf-8"), headers=_NO_STORE)
    except Exception:
        return JSONResponse(503, {"detail": {"error": "S17_REACT_UNAVAILABLE", "message": "Controlled S17 React shell is not built"}}, headers=_NO_STORE)


@s17_router.get("/controlled/s17")
def s17_shell(request: Request) -> Response:
    return _shell(request)


@s17_router.get("/controlled/s17/react")
def s17_shell_alias(request: Request) -> Response:
    return _shell(request)
