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

_HEX64 = r"^[0-9a-f]{64}$"


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


def _s13_require_operator(request: Request) -> None:
    module = _s13_app_module(request)
    # S13 operator credential follows the S08/S12 pattern: distinct bearer
    # credential bound to one module attribute.
    credential = getattr(module, "S13_OPERATOR_CREDENTIAL", "")
    subject = getattr(module, "S13_OPERATOR_SUBJECT", "")
    # Also accept S01 operator as a fallback for demo operator convenience
    # (only when S13-specific vars are absent).
    if not subject or not credential:
        credential = getattr(module, "S01_OPERATOR_CREDENTIAL", "")
        subject = getattr(module, "S01_OPERATOR_SUBJECT", "")
    if not subject or not module._s01_has_credential(request, credential):  # type: ignore[attr-defined]
        raise HTTPException(
            403,
            detail={
                "error": "S13_FORBIDDEN",
                "message": "Registered S13 operator identity required",
            },
        )


# ------------------------------------------------------------------ DTOs

_S13_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"description": "Registered S13 operator identity required"},
    404: {"description": "Application or obligation is unavailable"},
    422: {"description": "S13 command does not match the registered contract"},
    503: {"description": "S13 delivery plane is unavailable"},
}


class S13QueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["s13-delivery-view/1"]
    application_id: str = Field(min_length=1, max_length=200)
    phase: str
    route: str
    cycle: int = Field(ge=1)
    lifecycle_revision: int = Field(ge=0)
    verification_completed: bool
    obligation: dict[str, Any] | None
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

@s13_router.get(
    "/controlled/s13/delivery/{application_id}",
    response_model=S13QueryResponse,
    responses=_S13_ERROR_RESPONSES,
)
def s13_delivery_query(
    application_id: str,
    request: Request,
) -> dict[str, Any]:
    _s13_require_operator(request)
    service = _s13_service(request)
    try:
        return service.delivery_view(application_id=application_id)
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
    _s13_require_operator(request)
    service = _s13_service(request)
    try:
        result = service.reconcile_delivery(obligation_id=body.obligation_id)
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
    _s13_require_operator(request)
    service = _s13_service(request)
    try:
        result = service.compensate_delivery(obligation_id=body.obligation_id)
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
    _s13_require_operator(request)
    service = _s13_service(request)
    result = service.process_next_delivery()
    return {
        "status": result.get("status") or "idle",
        "obligation_id": result.get("obligation_id"),
        "operation_id": result.get("operation_id"),
        "remote_message_id": result.get("remote_message_id"),
        "reason_code": result.get("reason_code"),
    }
