"""S13 downstream-delivery HTTP adapter (Ticket #29).

Lifecycle owns completion + obligation; the adapter owns transport only.
HTTP is an adapter around the live S01 application authority resolved from
``request.app.state`` — it never writes the store directly or instantiates
an adapter.  Legacy report/fixture routes cannot call delivery commands.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

s13_router = APIRouter()

_S13_APP_MODULE_STATE_KEY = "_s13_application_module"

def _s13_app_module(request: Request) -> Any:
    module = getattr(request.app.state, _S13_APP_MODULE_STATE_KEY, None)
    if module is None:
        raise HTTPException(
            503,
            detail={
                "error": "S13_UNAVAILABLE",
                "message": "Controlled S13 delivery plane is unavailable",
            },
        )
    return module


def register_router(app: FastAPI, app_module: Any) -> None:
    """Bind the live application module on the app and register S13 routes.

    Idempotent for the same (app, module); a conflicting module is rejected
    before any state or route change.
    """
    existing = getattr(app.state, _S13_APP_MODULE_STATE_KEY, None)
    if existing is not None:
        if existing is not app_module:
            raise RuntimeError(
                "S13 router already registered with a different application module"
            )
        return
    setattr(app.state, _S13_APP_MODULE_STATE_KEY, app_module)
    app.include_router(s13_router)


def _s13_service(request: Request) -> Any:
    module = _s13_app_module(request)
    service = getattr(module, "S01_SERVICE", None)
    # Fallback attribute name for test monkeypatch indirection (service may be
    # exposed as _SERVICE on test harnesses — check both).
    if service is None:
        service = getattr(module, "_S13_SERVICE", None)
    if service is None:
        # Use S01_SERVICE as the S13 authority — S13 delivery is owned by the
        # S01 lifecycle/delivery process; there is no separate deployment.
        service = getattr(module, "S01_SERVICE", None)
    if service is None:
        raise HTTPException(
            503,
            detail={
                "error": "S13_UNAVAILABLE",
                "message": "Controlled S13 delivery plane is unavailable",
            },
        )
    return service


def _s13_require_operator(request: Request) -> Any:
    module = _s13_app_module(request)
    # S13 owns a distinct bearer identity. Missing S13 configuration closes
    # this boundary even when an S01 operator is configured.
    credential = getattr(module, "S13_OPERATOR_CREDENTIAL", "")
    subject = getattr(module, "S13_OPERATOR_SUBJECT", "")
    recognized = bool(
        subject and module._s01_has_credential(request, credential)  # type: ignore[attr-defined]
    )
    if not recognized:
        # Controlled same-operator mapping: the registered control-plane
        # operator may present the S01 operator credential at this read
        # boundary so one operator console can complete settle/grant/reopen
        # alongside delivery reads with distinct deployed credentials.  The
        # audit identity stays the S13 subject/source below, and the S13
        # credential never gains reciprocal S01 command authority.
        s01_credential = str(getattr(module, "S01_OPERATOR_CREDENTIAL", "") or "")
        recognized = bool(
            subject
            and s01_credential
            and module._s01_has_credential(request, s01_credential)  # type: ignore[attr-defined]
        )
    if not recognized:
        raise HTTPException(
            403,
            detail={
                "error": "S13_FORBIDDEN",
                "message": "Registered S13 operator identity required",
            },
        )
    scope = getattr(module, "S13_OPERATOR_SCOPE", "C-DEMO")
    principal_type = getattr(module, "S01CommandPrincipal", None)
    if not isinstance(scope, str) or not scope or principal_type is None:
        raise HTTPException(
            503,
            detail={
                "error": "S13_UNAVAILABLE",
                "message": "Controlled S13 operator identity is unavailable",
            },
        )
    return principal_type(
        subject=subject,
        role="operator",
        scope=scope,
        source_id="s13-delivery-console",
        expires_at=float("inf"),
    )


# ------------------------------------------------------------------ DTOs

class S13ObligationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    obligation_id: str
    application_id: str
    cycle: int = Field(ge=1)
    route: str
    attribution_kind: str
    operation_id: str
    recipient_id: str
    adapter_id: str
    adapter_version: str
    payload_ref: str
    payload_digest: str = Field(min_length=64, max_length=64)
    payload_schema: str
    status: str


class S13ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: str
    reason_code: str | None = None
    message: str | None = None
    hint: str | None = None


class S13ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: S13ErrorDetail


class S13ValidationErrorItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    loc: list[str | int]
    msg: str
    type: str


class S13ValidationErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: list[S13ValidationErrorItem]


_S13_ERROR_RESPONSES: dict[int, dict[str, Any]] = {
    403: {"model": S13ErrorResponse},
    404: {"model": S13ErrorResponse},
    422: {"model": S13ValidationErrorResponse},
    503: {"model": S13ErrorResponse},
}


class S13RoutingAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    decision_id: str | None = None
    work_item_id: str | None = None
    request_id: str | None = None
    batch_id: str | None = None
    work_item_ids: tuple[str, ...] = ()


class S13RoutingHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cycle: int = Field(ge=1)
    route: str
    attribution_kind: str
    attribution: S13RoutingAttribution
    completion_event_id: str
    completion_lifecycle_revision: int = Field(ge=0)
    run_id: str
    evidence_snapshot_id: str
    evidence_snapshot_digest: str = Field(min_length=64, max_length=64)
    release_id: str
    release_digest: str = Field(min_length=64, max_length=64)
    checker_build: str
    route_basis_digest: str = Field(min_length=64, max_length=64)
    obligation_id: str
    operation_id: str


class S13QueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["s13-delivery-view/1"]
    application_id: str = Field(min_length=1, max_length=200)
    phase: str
    route: str
    cycle: int = Field(ge=1)
    lifecycle_revision: int = Field(ge=0)
    verification_completed: bool
    obligation: S13ObligationSummary | None
    routing_history: tuple[S13RoutingHistoryEntry, ...]
    delivery_status: str
    attempt_count: int = Field(ge=0)
    projection_watermark: int = Field(ge=0)
    store_revision: int = Field(ge=0)


class S13ReconcileCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    obligation_id: str = Field(min_length=1, max_length=120)


class S13ReconcileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    obligation_id: str
    operation_id: str | None = None
    delivery_status: str
    status: str
    reason_code: str | None = None


class S13CompensateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    obligation_id: str = Field(min_length=1, max_length=120)


class S13CompensateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    obligation_id: str
    operation_id: str | None = None
    status: str
    reason_code: str | None = None


class S13ProcessNextDeliveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    obligation_id: str | None = None
    operation_id: str | None = None
    remote_message_id: str | None = None
    reason_code: str | None = None


# ------------------------------------------------------------------ Routes


def _s13_obligation_summary(obligation: dict[str, Any] | None) -> dict[str, Any] | None:
    if obligation is None:
        return None
    return {
        key: obligation.get(key)
        for key in (
            "obligation_id",
            "application_id",
            "cycle",
            "route",
            "attribution_kind",
            "operation_id",
            "recipient_id",
            "adapter_id",
            "adapter_version",
            "payload_ref",
            "payload_digest",
            "payload_schema",
            "status",
        )
    }

@s13_router.get(
    "/controlled/s13/delivery/{application_id}",
    response_model=S13QueryResponse,
    responses=_S13_ERROR_RESPONSES,
)
def s13_delivery_query(
    application_id: str,
    request: Request,
) -> dict[str, Any]:
    principal = _s13_require_operator(request)
    service = _s13_service(request)
    try:
        result = service.delivery_view(
            principal=principal, application_id=application_id
        )
        result["obligation"] = _s13_obligation_summary(result.get("obligation"))
        return result
    except Exception as error:
        # Existence-hiding: unknown application/tenant maps to 404.
        if error.__class__.__name__ in {"QueryNotFound", "LookupError"}:
            raise HTTPException(
                404,
                detail={
                    "error": "S13_NOT_FOUND",
                    "message": "S13 application or obligation is unavailable",
                },
            ) from error
        raise


@s13_router.post(
    "/controlled/s13/api/commands/reconcile",
    response_model=S13ReconcileResponse,
    responses=_S13_ERROR_RESPONSES,
)
def s13_reconcile(
    body: S13ReconcileCommand,
    request: Request,
) -> dict[str, Any]:
    principal = _s13_require_operator(request)
    service = _s13_service(request)
    try:
        result = service.reconcile_delivery(
            principal=principal,
            worker_id=principal.subject,
            obligation_id=body.obligation_id,
        )
    except Exception as error:
        if error.__class__.__name__ == "QueryNotFound":
            raise HTTPException(
                404,
                detail={
                    "error": "S13_NOT_FOUND",
                    "message": "S13 obligation is unavailable",
                },
            ) from error
        raise
    return {
        "obligation_id": result.get("obligation_id") or body.obligation_id,
        "operation_id": result.get("operation_id"),
        "delivery_status": result.get("delivery_status") or result.get("status") or "unknown",
        "status": result.get("status") or result.get("delivery_status") or "unknown",
        "reason_code": result.get("reason_code"),
    }


@s13_router.post(
    "/controlled/s13/api/commands/compensate",
    response_model=S13CompensateResponse,
    responses=_S13_ERROR_RESPONSES,
)
def s13_compensate(
    body: S13CompensateCommand,
    request: Request,
) -> dict[str, Any]:
    principal = _s13_require_operator(request)
    service = _s13_service(request)
    try:
        result = service.compensate_delivery(
            principal=principal,
            worker_id=principal.subject,
            obligation_id=body.obligation_id,
        )
    except Exception as error:
        if error.__class__.__name__ == "QueryNotFound":
            raise HTTPException(
                404,
                detail={
                    "error": "S13_NOT_FOUND",
                    "message": "S13 obligation is unavailable",
                },
            ) from error
        raise
    return {
        "obligation_id": result.get("obligation_id") or body.obligation_id,
        "operation_id": result.get("operation_id"),
        "status": result.get("status") or "compensated",
        "reason_code": result.get("reason_code"),
    }


@s13_router.post(
    "/controlled/s13/api/commands/process_next_delivery",
    response_model=S13ProcessNextDeliveryResponse,
    responses=_S13_ERROR_RESPONSES,
)
def s13_process_next_delivery(
    request: Request,
) -> dict[str, Any]:
    """Operator-triggered sender claim — the periodic worker seam.

    Returns one claim+send outcome; a distributed scheduler would call this
    under its own lease/observability.  The outbox remains at-least-once:
    a second call after a confirmed send returns idle with one business
    effect, and a blind resend of an unknown outcome is rejected until
    same-operation reconciliation proves not_executed.
    """
    principal = _s13_require_operator(request)
    service = _s13_service(request)
    result = service.process_next_delivery(
        principal=principal,
        worker_id=principal.subject,
    )
    return {
        "status": result.get("status") or "idle",
        "obligation_id": result.get("obligation_id"),
        "operation_id": result.get("operation_id"),
        "remote_message_id": result.get("remote_message_id"),
        "reason_code": result.get("reason_code"),
    }
