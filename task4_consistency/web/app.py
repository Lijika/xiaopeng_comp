"""FastAPI demo: check applications, edit rules, maintain entity KB."""

from __future__ import annotations

import email.policy
import hmac
import json
import os
import re
import shutil
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.middleware.base import BaseHTTPMiddleware

from task4_consistency.audit import audit_log_path, audit_status, read_audit_tail, write_audit
from task4_consistency.controlled.s01 import (
    ControlledScenarioService,
    ControlledScenarioTestDriver,
    QueryNotFound,
    S01CommandPrincipal,
    _ApplicationStateAuthorityUnavailable,
)
from task4_consistency.controlled.s02 import (
    ControlledObject,
    RegisteredSource,
    load_runtime_registry,
)
from task4_consistency.kb.store import get_kb, reload_kb

from task4_consistency.models import Application
from task4_consistency.report import report_to_html
from task4_consistency.rules.critical_guard import (
    CriticalGuardError,
    enforce_critical_fingerprints,
    fingerprints_as_dicts,
)
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES = ROOT / "configs" / "rules_auto_lease.yaml"
RUNTIME_RULES = ROOT / "configs" / "runtime_rules.yaml"
FIXTURES = ROOT / "fixtures" / "applications"
STATIC = Path(__file__).resolve().parent / "static"
TEMPLATES = Path(__file__).resolve().parent / "templates"
_KB_SECTIONS = {"address_aliases", "org_aliases", "plate_prefixes"}
S01_TEMPLATE = TEMPLATES / "s01.html"
S02_TEMPLATE = TEMPLATES / "s02.html"
S01_REACT_STATIC = STATIC / "react"
S01_REACT_INDEX = S01_REACT_STATIC / "index.html"


def _s01_demo_flag(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _s02_registry_from_environment(
    *, test: bool = False
) -> tuple[tuple[RegisteredSource, ...], tuple[ControlledObject, ...]]:
    prefix = "TASK4_S02_TEST" if test else "TASK4_S02"
    registry_value = os.environ.get(f"{prefix}_REGISTRY_PATH", "").strip()
    object_root_value = os.environ.get(f"{prefix}_OBJECT_ROOT", "").strip()
    if not registry_value and not object_root_value:
        return (), ()
    if not registry_value or not object_root_value:
        raise ValueError("S02 source registry configuration is incomplete")
    registry = Path(registry_value)
    object_root = Path(object_root_value)
    if not registry.is_absolute() or not object_root.is_absolute():
        raise ValueError("S02 source registry paths must be absolute")
    return load_runtime_registry(registry, object_root)


S02_CONFIGURATION_ERROR: str | None = None
try:
    S02_REGISTERED_SOURCES, S02_CONTROLLED_OBJECTS = _s02_registry_from_environment()
except Exception:
    S02_REGISTERED_SOURCES, S02_CONTROLLED_OBJECTS = (), ()
    S02_CONFIGURATION_ERROR = "S02 source registry configuration is invalid"

S01_CONFIGURATION_ERROR: str | None = None
S05_EXCEPTION_APPROVER_CREDENTIAL = os.environ.get(
    "TASK4_S05_EXCEPTION_APPROVER_CREDENTIAL", ""
).strip()
S05_EXCEPTION_APPROVER_SUBJECT = (
    os.environ.get("TASK4_S05_EXCEPTION_APPROVER_SUBJECT", "").strip()
    or "c-demo-exception-approver"
)
try:
    _s01_state_value = os.environ.get("TASK4_S01_STATE_PATH", "").strip()
    if not _s01_state_value:
        raise ValueError("TASK4_S01_STATE_PATH is required")
    _s01_state_path = Path(_s01_state_value)
    if not _s01_state_path.is_absolute():
        raise ValueError("TASK4_S01_STATE_PATH must be absolute")
    S01_SERVICE: ControlledScenarioService | None = ControlledScenarioService(
        fixture_root=FIXTURES,
        rules_path=DEFAULT_RULES,
        state_path=_s01_state_path,
        audit_available=_s01_demo_flag("TASK4_S01_AUDIT_AVAILABLE", default=True),
        storage_available=_s01_demo_flag("TASK4_S01_STORAGE_AVAILABLE", default=True),
        registered_sources=S02_REGISTERED_SOURCES,
        controlled_objects=S02_CONTROLLED_OBJECTS,
        exception_approver_subject=S05_EXCEPTION_APPROVER_SUBJECT,
    )
except Exception as error:
    S01_SERVICE = None
    S01_CONFIGURATION_ERROR = str(error)
S01_TEST_DRIVER: ControlledScenarioTestDriver | None = None
S01_BACKGROUND_ENABLED = _s01_demo_flag(
    "TASK4_S01_BACKGROUND_ENABLED", default=True
)
S01_REQUIRE_CONFIGURED_STARTUP = True
S01_SESSION_COOKIE = "s01_session"
S01_SESSION_TTL_SECONDS = 15 * 60
S01_SESSION_CLOCK: Callable[[], float] = time.time
S01_DEMO_CREDENTIAL = os.environ.get("TASK4_S01_DEMO_CREDENTIAL", "").strip()
S01_DEMO_SUBJECT = os.environ.get("TASK4_S01_DEMO_SUBJECT", "").strip()
S01_OPERATOR_CREDENTIAL = os.environ.get("TASK4_S01_OPERATOR_CREDENTIAL", "").strip()
S01_OPERATOR_SUBJECT = os.environ.get("TASK4_S01_OPERATOR_SUBJECT", "").strip()
S01_AUDITOR_CREDENTIAL = os.environ.get("TASK4_S01_AUDITOR_CREDENTIAL", "").strip()
S01_AUDITOR_SUBJECT = os.environ.get("TASK4_S01_AUDITOR_SUBJECT", "").strip()
S02_SESSION_COOKIE = "s02_session"
S02_SESSION_TTL_SECONDS = 15 * 60
S02_MAX_COMMAND_BYTES = 256 * 1024
S02_CREDENTIAL = os.environ.get("TASK4_S02_CREDENTIAL", "").strip()
S02_SUBJECT = os.environ.get("TASK4_S02_SUBJECT", "").strip()
S02_TENANT_ID = os.environ.get("TASK4_S02_TENANT_ID", "").strip()
S02_SOURCE_SYSTEM_ID = os.environ.get("TASK4_S02_SOURCE_SYSTEM_ID", "").strip()
S02_CONFIGURED = bool(
    S02_REGISTERED_SOURCES
    and S02_CREDENTIAL
    and S02_SUBJECT
    and S02_TENANT_ID
    and S02_SOURCE_SYSTEM_ID
    and any(
        source.tenant_id == S02_TENANT_ID
        and source.source_system_id == S02_SOURCE_SYSTEM_ID
        for source in S02_REGISTERED_SOURCES
    )
)


@dataclass(frozen=True)
class S01Principal:
    subject: str
    roles: frozenset[str]
    scope: str
    expires_at: float


# ARCH Round16 W1: serialize put/reset; no concurrent half-writes
RULES_WRITE_LOCK = threading.Lock()


class S01BackgroundRuntime:
    """Independently consume durable S01 work and projection outbox facts."""

    _WORKER_IDENTITY = "s01-background-runtime"
    _SOURCE_ID = "s01-target-worker"

    def __init__(self, service: ControlledScenarioService) -> None:
        self._service = service
        self._principal = S01CommandPrincipal(
            subject=self._WORKER_IDENTITY,
            role="operator",
            scope="C-DEMO",
            source_id=self._SOURCE_ID,
        )
        self._stop = threading.Event()
        self._health_lock = threading.Lock()
        self._health = {"status": "created", "reason_code": ""}
        self._thread = threading.Thread(
            target=self._run,
            name=self._WORKER_IDENTITY,
            daemon=True,
        )

    def start(self) -> None:
        with self._health_lock:
            self._health = {"status": "running", "reason_code": ""}
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        with self._health_lock:
            if self._health["status"] == "running":
                self._health = {"status": "stopped", "reason_code": "S01_RUNTIME_STOPPED"}

    def health(self) -> dict[str, str]:
        with self._health_lock:
            return dict(self._health)

    def _mark_unhealthy(self, reason_code: str) -> None:
        with self._health_lock:
            self._health = {"status": "unhealthy", "reason_code": reason_code}

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                expire_due = getattr(
                    self._service, "expire_due_supplement_requests", None
                )
                if expire_due is not None:
                    expiry_result = expire_due(principal=self._principal)
                    if expiry_result.get("status") != "accepted":
                        raise RuntimeError("supplement deadline sweep failed")
                worker_result = self._service.process_next_job()
                projection_result = self._service.refresh_projection()
            except Exception:
                reason_code = "S01_BACKGROUND_RUNTIME_EXCEPTION"
                self._mark_unhealthy(reason_code)
                try:
                    self._service.stop_new_cohort(
                        reason_code="S01_RUNTIME_UNHEALTHY",
                        failure_reason_code=reason_code,
                        principal=self._principal,
                    )
                except Exception:
                    pass
                self._stop.set()
                return
            if worker_result.status == "stopped":
                self._mark_unhealthy(
                    worker_result.reason_code or "S01_BACKGROUND_RUNTIME_UNHEALTHY"
                )
                self._stop.set()
                return
            if worker_result.status == "failed" and worker_result.retry_after_seconds:
                self._stop.wait(worker_result.retry_after_seconds)
                continue
            if worker_result.status == "idle" and projection_result["updated"] == 0:
                self._stop.wait(0.02)


@asynccontextmanager
async def _lifespan(application: FastAPI):
    runtime: S01BackgroundRuntime | None = None
    if S01_REQUIRE_CONFIGURED_STARTUP and S01_SERVICE is None:
        raise RuntimeError(
            S01_CONFIGURATION_ERROR or "S01 target authority is not configured"
        )
    if S01_BACKGROUND_ENABLED and S01_SERVICE is not None:
        runtime = S01BackgroundRuntime(S01_SERVICE)
        runtime.start()
    application.state.s01_background_runtime = runtime
    try:
        yield
    finally:
        if runtime is not None:
            runtime.stop()


app = FastAPI(title="Task4 Consistency Demo", version="1.0.0", lifespan=_lifespan)


def _sanitized_validation_detail(
    errors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validation detail items never reflect rejected input, context,
    credentials, or oversized payload content back to the client."""
    return [
        {
            "loc": list(error.get("loc", ())),
            "msg": str(error.get("msg", "")),
            "type": str(error.get("type", "")),
        }
        for error in errors
    ]


@app.exception_handler(RequestValidationError)
async def _sanitized_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Validation 422s never reflect rejected input, context, credentials,
    or oversized payload content back to the client."""
    return JSONResponse(
        status_code=422,
        content={"detail": _sanitized_validation_detail(exc.errors())},
    )


class _ImmutableHashedAssets(StaticFiles):
    """Serve content-hashed React assets with long-lived immutable caching."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


react_assets = STATIC / "react" / "assets"
app.mount(
    "/static/react/assets",
    _ImmutableHashedAssets(directory=str(react_assets), check_dir=False),
    name="react-assets",
)
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


class ReactShellCachePolicy(BaseHTTPMiddleware):
    """Keep the built React shell no-store even when fetched from /static."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        if (
            request.url.path == "/static/react/index.html"
            and response.status_code == 200
        ):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response


class OptionalTokenAuth(BaseHTTPMiddleware):
    """If TASK4_WEB_TOKEN set: require Authorization: Bearer <token> or X-Task4-Token.
    Unset token → open demo mode (no auth).
    """

    _PUBLIC_PREFIXES = ("/static",)
    _PUBLIC_EXACT = {"/api/health"}
    _OWN_AUTH_PREFIXES = ("/controlled/s01", "/controlled/s02")

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        token = os.environ.get("TASK4_WEB_TOKEN", "").strip()
        if not token:
            return await call_next(request)
        path = request.url.path
        if path in self._PUBLIC_EXACT or any(path.startswith(p) for p in self._PUBLIC_PREFIXES):
            return await call_next(request)
        if any(path.startswith(prefix) for prefix in self._OWN_AUTH_PREFIXES):
            return await call_next(request)
        # UI shell open; APIs protected (except health)
        if path == "/":
            return await call_next(request)
        provided = request.headers.get("X-Task4-Token") or ""
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
        if provided != token:
            write_audit(
                "auth_denied",
                actor="web",
                ok=False,
                detail={"path": path, "method": request.method},
            )
            return JSONResponse(
                status_code=401,
                content={
                    "detail": {
                        "error": "unauthorized",
                        "message": "需要有效 TASK4_WEB_TOKEN",
                        "hint": "Header: Authorization: Bearer <token> 或 X-Task4-Token",
                    }
                },
            )
        return await call_next(request)


app.add_middleware(OptionalTokenAuth)
app.add_middleware(ReactShellCachePolicy)


class S01ResponsePolicy(BaseHTTPMiddleware):
    """Apply the controlled-slice cache and bounded-error policy centrally."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        if not path.startswith(("/controlled/s01", "/controlled/s02")):
            return await call_next(request)
        slice_id = "S02" if path.startswith("/controlled/s02") else "S01"
        try:
            response = await call_next(request)
        except Exception:
            response = JSONResponse(
                status_code=500,
                content={
                    "detail": {
                        "error": f"{slice_id}_INTERNAL_ERROR",
                        "message": f"Controlled {slice_id} request failed",
                    }
                },
            )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response


app.add_middleware(S01ResponsePolicy)


def _quarantine_bad_runtime(err: Exception) -> None:
    """ADV-W1 self-heal: move poisoned runtime aside so default package reactivates."""
    if not RUNTIME_RULES.exists():
        return
    bad = RUNTIME_RULES.with_suffix(".yaml.bad")
    try:
        if bad.exists():
            bad.unlink()
        RUNTIME_RULES.replace(bad)
    except OSError:
        try:
            RUNTIME_RULES.unlink()
        except OSError:
            pass
    write_audit(
        "rules_auto_heal",
        ok=True,
        detail={"error": str(err), "quarantined": _rel_to_root(bad) if bad.exists() else None},
    )


def _active_rules_path() -> Path:
    if RUNTIME_RULES.exists():
        try:
            load_rules(RUNTIME_RULES)
            return RUNTIME_RULES
        except Exception as e:
            _quarantine_bad_runtime(e)
    return DEFAULT_RULES


def _rel_to_root(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        return str(path)


def _engine() -> RuleEngine:
    return RuleEngine(load_rules(_active_rules_path()))


def _parse_rules_payload(body: "RulesBody") -> tuple[dict[str, Any], str]:
    """Return (data, yaml_text). Raises HTTPException 400 with clear tip."""
    if body.yaml_text is not None:
        text = body.yaml_text
        if not str(text).strip():
            raise HTTPException(
                400,
                detail={
                    "error": "empty_yaml",
                    "message": "规则 YAML 为空，请粘贴完整规则包后再保存",
                    "hint": "至少包含 package / version / rules 列表",
                },
            )
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            raise HTTPException(
                400,
                detail={
                    "error": "invalid_yaml",
                    "message": f"YAML 语法错误: {e}",
                    "hint": "检查缩进、冒号、引号；可用在线 YAML 校验",
                },
            ) from e
        if not isinstance(data, dict):
            raise HTTPException(
                400,
                detail={
                    "error": "yaml_not_mapping",
                    "message": "规则根节点必须是 mapping/object",
                    "hint": "顶层应是 package: ... rules: [...] 结构",
                },
            )
        return data, text
    if body.content is not None:
        if not isinstance(body.content, dict):
            raise HTTPException(
                400,
                detail={
                    "error": "content_not_object",
                    "message": "content 必须是 JSON 对象",
                },
            )
        data = body.content
        yaml_text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
        return data, yaml_text
    raise HTTPException(
        400,
        detail={
            "error": "missing_body",
            "message": "需要 content 或 yaml_text",
            "hint": "PUT /api/rules  body: {\"yaml_text\": \"...\"}",
        },
    )


def _validate_rules_yaml(yaml_text: str) -> Any:
    """Load rules via temp file; never touch runtime path.

    load_rules already runs schema + package policy + critical fingerprints.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(yaml_text)
        tmp = Path(fh.name)
    try:
        cfg = load_rules(tmp)
        # explicit re-assert (load_rules already enforces; keep for clarity)
        enforce_critical_fingerprints(cfg)
        return cfg
    except CriticalGuardError as e:
        raise HTTPException(
            400,
            detail={
                "error": e.error,
                "message": str(e),
                "hint": "critical 三剑客 R_VIN_CROSS/R_ENGINE_CROSS/R_ID_EXACT 语义指纹不可改（见 CONFIG_GUIDE）",
            },
        ) from e
    except Exception as e:
        err = "rules_schema_invalid"
        msg = str(e)
        if "ADV-W" in msg or "rel_tol" in msg or "field_aliases" in msg:
            err = "rules_policy_invalid"
        raise HTTPException(
            400,
            detail={
                "error": err,
                "message": f"规则校验失败: {e}",
                "hint": "检查 rules[].id/type/field；critical 指纹与 policy 护栏",
            },
        ) from e
    finally:
        tmp.unlink(missing_ok=True)


class CheckBody(BaseModel):
    application: dict[str, Any]
    rules_path: str | None = None


class RulesBody(BaseModel):
    content: dict[str, Any] | None = None
    yaml_text: str | None = None


class KBItem(BaseModel):
    section: str = Field(description="address_aliases | org_aliases | plate_prefixes")
    key: str
    value: str

    @field_validator("section")
    @classmethod
    def _section_ok(cls, v: str) -> str:
        s = str(v).strip()
        if s not in _KB_SECTIONS:
            raise ValueError(
                f"section 必须是 {sorted(_KB_SECTIONS)} 之一，收到: {v!r}"
            )
        return s

    @field_validator("key", "value")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        s = str(v).strip()
        if not s:
            raise ValueError("key/value 不能为空")
        return s


class S01SubmitBody(BaseModel):
    scenario_id: str
    idempotency_key: str


class S01ProcessBody(BaseModel):
    worker_id: str = "s01-http-worker"
    now: int = 0
    crash: bool = False
    partial: bool = False
    stale: bool = False
    cas_fault: str | None = None
    duplicate: bool = False


class S01RecoveryBody(BaseModel):
    expected_failure_reason_code: str


class S07VerifyRecoveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_lifecycle_revision: int = Field(ge=1, strict=True)
    expected_criterion_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        strict=True,
    )
    idempotency_key: str = Field(min_length=1, max_length=200, strict=True)


class S01QueueBlocker(BaseModel):
    finding_id: str
    rule_id: str
    reason_code: str
    severity: str


class S01QueueManualItem(BaseModel):
    application_id: str
    work_item_id: str
    assigned_subject: str
    claim_fence: int
    claim_expires_at: int
    phase: str
    route: str
    evidence_ready: bool
    mandatory_blockers: list[S01QueueBlocker]
    lifecycle_revision: int
    evidence_revision: int
    projection_watermark: int


class S01RecoveryQueueItem(BaseModel):
    recovery_work_id: str
    application_id: str
    status: str
    phase: str
    primary_reason_code: str
    responsible_party: str
    lifecycle_revision: int
    projection_watermark: int


class S01QueueResponse(BaseModel):
    items: list[S01QueueManualItem]
    recovery_items: list[S01RecoveryQueueItem]
    projection_watermark: int
    access_ended: bool | None = None


class S01RecoveryAttempt(BaseModel):
    attempt: int
    classification: str
    status: str
    started_at: int
    retry_not_before: int | None = None


class S01RecoveryCriterionCondition(BaseModel):
    condition_id: str
    reason_code: str


class S01RecoveryCriterion(BaseModel):
    id: str
    version: str
    operation: str
    dependency: str
    required_conditions: list[str]
    trusted_verifier: str
    evidence_kind: str
    conditions: list[S01RecoveryCriterionCondition]
    digest: str


class S01RetryPolicy(BaseModel):
    id: str
    max_attempts: int
    retry_offsets_seconds: list[int]
    jitter: bool


class S01RecoveryWorkResponse(BaseModel):
    schema_version: str
    recovery_work_id: str
    status: str
    application_id: str
    cycle: int
    phase: str
    route: str
    lifecycle_revision: int
    evidence_revision: int
    primary_reason_code: str
    related_reason_codes: list[str]
    operation: str
    dependency: str
    logical_operation_id: str
    attempts: list[S01RecoveryAttempt]
    responsible_party: str
    recovery_action: str
    recovery_target: str
    criterion: S01RecoveryCriterion
    retry_policy: S01RetryPolicy
    outcome_known: bool
    retryable: bool
    recovery_fact_count: int
    resolution_count: int
    job_status: str
    delivery_semantics: str
    protected_business_revision: int
    current_run_id: str | None = None
    projection_watermark: int
    can_verify: bool


class S01VerifyRoutingContext(BaseModel):
    cycle: int
    lifecycle_revision: int
    evidence_revision: int
    run_id: str | None = None
    request_id: str
    decision_id: str
    current_context: str


class S01VerifyRecoveryResult(BaseModel):
    status: str
    replayed: bool
    recovery_work_id: str
    recovery_fact_id: str
    application_id: str
    phase: str
    lifecycle_revision: int
    evidence_revision: int
    successor_job_id: str
    successor_fence: int
    routing_context: S01VerifyRoutingContext | None = None


class S01ErrorDetail(BaseModel):
    error: str
    reason_code: str | None = None
    message: str | None = None
    hint: str | None = None


class S01ErrorResponse(BaseModel):
    detail: S01ErrorDetail


class S01ValidationErrorItem(BaseModel):
    loc: list[str | int]
    msg: str
    type: str


class S01VerifyErrorResponse(BaseModel):
    detail: S01ErrorDetail | list[S01ValidationErrorItem]


class S01CurrentRouteFailure(BaseModel):
    reason_code: str
    responsible_party: str
    recovery_action: str
    recovery_target: str


class S01CurrentRouteResponse(BaseModel):
    schema_version: str
    application_id: str
    phase: str
    route: str
    current_run_id: str | None = None
    cycle: int
    lifecycle_revision: int
    evidence_revision: int
    evidence_snapshot_id: str | None = None
    evidence_snapshot_digest: str | None = None
    release_id: str | None = None
    release_digest: str | None = None
    checker_build: str | None = None
    currentness_reason: str
    completion_basis: str | None = None
    exception_id: str | None = None
    exception_decision_id: str | None = None
    exception_expires_at: int | None = None
    failure: S01CurrentRouteFailure | None = None


class S02SubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str
    submission: dict[str, Any]


class S03ClaimBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_context: dict[str, Any]


class S03FencedBody(S03ClaimBody):
    expected_fence: int = Field(ge=0, strict=True)
    idempotency_key: str


class S03SubmitBody(S03FencedBody):
    idempotency_key: str
    verification: dict[str, Any]


class S04CorrectionBody(S03FencedBody):
    application_id: str = Field(min_length=1, max_length=200, strict=True)
    correction: dict[str, Any]


class S04RevealBody(S03ClaimBody):
    application_id: str = Field(min_length=1, max_length=200, strict=True)
    observation_id: str = Field(min_length=1, max_length=200, strict=True)
    expected_fence: int = Field(ge=1, strict=True)
    idempotency_key: str


class S05RequestBody(S03FencedBody):
    finding_id: str = Field(min_length=1, max_length=200, strict=True)
    reason_code: str = Field(min_length=1, max_length=100, strict=True)
    predecessor_request_id: str | None = Field(
        default=None, min_length=1, max_length=200, strict=True
    )
    expected_fence: int = Field(ge=1, strict=True)


class S05DecisionBody(S03FencedBody):
    work_item_id: str = Field(min_length=1, max_length=200, strict=True)
    decision: str = Field(min_length=1, max_length=20, strict=True)
    reason_code: str = Field(min_length=1, max_length=100, strict=True)
    expected_fence: int = Field(ge=1, strict=True)


class S05ContextBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_context: dict[str, Any]
    idempotency_key: str


class S05InvalidationBody(S05ContextBody):
    reason_code: str = Field(min_length=1, max_length=100, strict=True)


class S05OperationsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=200, strict=True)


class S03BatchPreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[dict[str, Any]]


class S03BatchSubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str
    plan: dict[str, Any]


def _s01_principal(request: Request) -> S01Principal | None:
    token = request.cookies.get(S01_SESSION_COOKIE, "")
    if not token:
        return None
    service = S01_SERVICE
    if service is None:
        return None
    resolved = service.resolve_session(token, now=S01_SESSION_CLOCK())
    if resolved is None:
        request.state.s01_access_ended = True
        return None
    principal = S01Principal(
        subject=str(resolved["subject"]),
        roles=frozenset(str(role) for role in resolved["roles"]),
        scope=str(resolved["scope"]),
        expires_at=float(resolved["expires_at"]),
    )
    return principal


def _s01_has_credential(request: Request, expected: str) -> bool:
    scheme, separator, supplied = request.headers.get("Authorization", "").partition(" ")
    return bool(
        expected
        and separator
        and scheme.lower() == "bearer"
        and supplied
        and hmac.compare_digest(supplied, expected)
    )


def _issue_s01_session(request: Request, response: Response) -> None:
    if not S01_DEMO_SUBJECT or not _s01_has_credential(request, S01_DEMO_CREDENTIAL):
        raise HTTPException(
            403,
            detail={"error": "S01_FORBIDDEN", "message": "Registered demo identity required"},
        )
    existing_token = request.cookies.get(S01_SESSION_COOKIE, "")
    existing = (
        _s01_service().resolve_session(existing_token, now=S01_SESSION_CLOCK())
        if existing_token
        else None
    )
    if (
        existing is not None
        and existing.get("subject") == S01_DEMO_SUBJECT
        and {"integrator", "reviewer"}.issubset(existing.get("roles", ()))
    ):
        return
    token, _ = _s01_service().issue_session(
        now=S01_SESSION_CLOCK(),
        ttl_seconds=S01_SESSION_TTL_SECONDS,
        subject=S01_DEMO_SUBJECT,
        roles=("integrator", "reviewer"),
    )
    response.set_cookie(
        S01_SESSION_COOKIE,
        token,
        max_age=S01_SESSION_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        secure=False,
    )


def _s01_require_role(request: Request, expected: str) -> S01Principal:
    principal = _s01_principal(request)
    if (
        principal is None
        or expected not in principal.roles
        or not ControlledScenarioService.is_c_demo_scope(principal.scope)
    ):
        raise HTTPException(
            403,
            detail={"error": "S01_FORBIDDEN", "message": "S01 scope or role is not allowed"},
        )
    return principal


def _s01_require_operator(request: Request) -> S01Principal:
    if not S01_OPERATOR_SUBJECT or not _s01_has_credential(
        request, S01_OPERATOR_CREDENTIAL
    ):
        raise HTTPException(
            403,
            detail={"error": "S01_FORBIDDEN", "message": "Registered operator required"},
        )
    return S01Principal(
        subject=S01_OPERATOR_SUBJECT,
        roles=frozenset({"operator"}),
        scope="C-DEMO",
        expires_at=float("inf"),
    )


def _s01_require_auditor(request: Request) -> S01Principal:
    if not S01_AUDITOR_SUBJECT or not _s01_has_credential(
        request, S01_AUDITOR_CREDENTIAL
    ):
        raise HTTPException(
            403,
            detail={"error": "S01_FORBIDDEN", "message": "Registered auditor required"},
        )
    return S01Principal(
        subject=S01_AUDITOR_SUBJECT,
        roles=frozenset({"auditor"}),
        scope="C-DEMO",
        expires_at=float("inf"),
    )


def _s01_service() -> ControlledScenarioService:
    if S01_SERVICE is None:
        raise HTTPException(
            503,
            detail={
                "error": "S01_UNAVAILABLE",
                "message": "Controlled S01 is unavailable",
            },
        )
    return S01_SERVICE


def _s01_admission_json(result: Any) -> dict[str, Any]:
    payload = asdict(result)
    payload["disposition"] = result.disposition.value
    payload.update({"track": "C-DEMO", "capability_gate": "G1"})
    return payload


def _s01_workbench_admission_json(result: Any) -> dict[str, Any]:
    return {
        "disposition": result.disposition.value,
        "reason_code": result.reason_code,
        "application_id": result.application_id,
        "receipt_id": result.receipt_id,
        "lifecycle_revision": result.lifecycle_revision,
        "evidence_revision": result.evidence_revision,
        "replayed": result.replayed,
        "track": "C-DEMO",
        "capability_gate": "G1",
    }


def _s01_worker_json(result: Any) -> dict[str, Any]:
    payload = asdict(result)
    for optional in ("recovery_work_id", "reconciliation"):
        if payload.get(optional) is None:
            payload.pop(optional, None)
    return payload


def _s01_disable_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _s02_service() -> ControlledScenarioService:
    if S01_SERVICE is None or not S02_CONFIGURED:
        raise HTTPException(
            503,
            detail={"error": "S02_UNAVAILABLE", "message": "Controlled S02 is unavailable"},
        )
    return S01_SERVICE


def _s02_principal(request: Request) -> S01Principal | None:
    token = request.cookies.get(S02_SESSION_COOKIE, "")
    if not token or S01_SERVICE is None:
        return None
    resolved = S01_SERVICE.resolve_session(token, now=S01_SESSION_CLOCK())
    expected_scope = f"R-OBSERVED/{S02_TENANT_ID}"
    if resolved is None or resolved.get("scope") != expected_scope:
        request.state.s02_access_ended = True
        return None
    return S01Principal(
        subject=str(resolved["subject"]),
        roles=frozenset(str(role) for role in resolved["roles"]),
        scope=str(resolved["scope"]),
        expires_at=float(resolved["expires_at"]),
    )


def _issue_s02_session(request: Request, response: Response) -> None:
    service = _s02_service()
    if not S02_SUBJECT or not _s01_has_credential(request, S02_CREDENTIAL):
        raise HTTPException(
            403,
            detail={"error": "S02_FORBIDDEN", "message": "Registered source identity required"},
        )
    token, _ = service.issue_session(
        now=S01_SESSION_CLOCK(),
        ttl_seconds=S02_SESSION_TTL_SECONDS,
        subject=S02_SUBJECT,
        roles=("integrator", "reviewer"),
        scope=f"R-OBSERVED/{S02_TENANT_ID}",
    )
    response.set_cookie(
        S02_SESSION_COOKIE,
        token,
        max_age=S02_SESSION_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        secure=False,
    )


def _s02_require_role(request: Request, role: str) -> S01Principal:
    principal = _s02_principal(request)
    if (
        principal is None
        or role not in principal.roles
        or not ControlledScenarioService.is_registered_scope(principal.scope)
    ):
        raise HTTPException(
            403,
            detail={"error": "S02_FORBIDDEN", "message": "S02 scope or role is not allowed"},
        )
    return principal


def _s02_admission_json(result: Any) -> dict[str, Any]:
    return {
        "disposition": result.disposition.value,
        "reason_code": result.reason_code,
        "responsible_party": result.responsible_party,
        "recovery_action": result.recovery_action,
        "retryable": result.retryable,
        "application_id": result.application_id,
        "receipt_id": result.receipt_id,
        "job_id": result.job_id,
        "lifecycle_revision": result.lifecycle_revision,
        "evidence_revision": result.evidence_revision,
        "replayed": result.replayed,
        "envelope_version": result.envelope_version,
        "schema_version": result.schema_version,
        "semantic_version": result.semantic_version,
        "envelope_id": result.envelope_id,
        "stream_id": result.stream_id,
        "source_revision": result.source_revision,
        "source_revision_id": result.source_revision_id,
        "envelope_fingerprint": result.envelope_fingerprint,
        "adapter_id": result.adapter_id,
        "adapter_version": result.adapter_version,
        "source_registration_digest": result.source_registration_digest,
        "artifact_manifest_digest": result.artifact_manifest_digest,
        "fact_counts": result.fact_counts,
        "gate_results": result.gate_results,
        "tenant_id": S02_TENANT_ID,
        "source_system_id": S02_SOURCE_SYSTEM_ID,
        "claim_label": result.claim_label,
        "real_cross_document_opportunities": result.real_cross_document_opportunities,
        "performance_status": result.performance_status,
        "request_id": result.request_id,
        "request_status": result.request_status,
        "batch_id": result.batch_id,
        "batch_closed": result.batch_closed,
        "request_progress_revision": result.request_progress_revision,
        "attachment_id": result.attachment_id,
        "attachment_version": result.attachment_version,
        "supersedes_attachment_id": result.supersedes_attachment_id,
        "fulfilled": result.fulfilled,
        "phase": result.phase,
        "route": result.route,
        "recovery_target": result.recovery_target,
    }


def _s03_reviewer_principal(request: Request) -> S01CommandPrincipal:
    principal = _s02_principal(request)
    if (
        principal is None
        or "reviewer" not in principal.roles
        or not ControlledScenarioService.is_registered_scope(principal.scope)
    ):
        raise HTTPException(404, detail={"error": "S03_NOT_FOUND"})
    return S01CommandPrincipal(
        subject=principal.subject,
        role="reviewer",
        scope=principal.scope,
        source_id=S02_SOURCE_SYSTEM_ID,
        expires_at=principal.expires_at,
    )


def _s04_demo_reviewer_principal(request: Request) -> S01CommandPrincipal:
    principal = _s01_principal(request)
    if (
        principal is None
        or "reviewer" not in principal.roles
        or not ControlledScenarioService.is_c_demo_scope(principal.scope)
    ):
        raise HTTPException(404, detail={"error": "S03_NOT_FOUND"})
    return S01CommandPrincipal(
        subject=principal.subject,
        role="reviewer",
        scope=principal.scope,
        source_id="c-demo-review-console",
        expires_at=principal.expires_at,
    )


def _s05_exception_approver_principal(request: Request) -> S01CommandPrincipal:
    if not S05_EXCEPTION_APPROVER_SUBJECT or not _s01_has_credential(
        request, S05_EXCEPTION_APPROVER_CREDENTIAL
    ):
        raise HTTPException(404, detail={"error": "S05_NOT_FOUND"})
    return S01CommandPrincipal(
        subject=S05_EXCEPTION_APPROVER_SUBJECT,
        role="exception_approver",
        scope="C-DEMO",
        source_id="c-demo-exception-approver",
    )


def _s05_router_principal(request: Request) -> S01CommandPrincipal:
    principal = _s01_require_operator(request)
    return S01CommandPrincipal(
        subject=principal.subject,
        role="operator",
        scope=principal.scope,
        source_id="c-demo-exception-router",
    )


async def _s03_command_body(request: Request, model: type[BaseModel]) -> BaseModel:
    declared_length = request.headers.get("content-length")
    try:
        if declared_length is not None and (
            int(declared_length) < 0 or int(declared_length) > S02_MAX_COMMAND_BYTES
        ):
            raise HTTPException(
                413,
                detail={
                    "error": "S03_COMMAND_TOO_LARGE",
                    "message": "S03 command exceeds the allowed size",
                },
            )
    except ValueError as error:
        raise HTTPException(
            422,
            detail={"error": "S03_INVALID_COMMAND", "message": "Invalid command"},
        ) from error
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > S02_MAX_COMMAND_BYTES:
            raise HTTPException(
                413,
                detail={
                    "error": "S03_COMMAND_TOO_LARGE",
                    "message": "S03 command exceeds the allowed size",
                },
            )
        chunks.append(chunk)
    try:
        return model.model_validate_json(b"".join(chunks))
    except ValidationError as error:
        raise HTTPException(
            422,
            detail={
                "error": "S03_INVALID_COMMAND",
                "message": "S03 command does not match the registered contract",
            },
        ) from error


def _s03_command_result(result: dict[str, Any]) -> dict[str, Any]:
    status = result.get("status")
    if status in {"claimed", "renewed", "released", "accepted"}:
        return result
    mapping = {
        "stale": (409, "S03_STALE"),
        "conflict": (409, "S03_CONFLICT"),
        "completed": (409, "S03_COMPLETED"),
        "rejected": (409, "S03_REJECTED"),
        "stopped": (503, "S03_STOPPED"),
        "unavailable": (503, "S03_UNAVAILABLE"),
    }
    mapped = mapping.get(status)
    if mapped is None:
        raise RuntimeError("S03 domain returned an unsupported status")
    status_code, error_code = mapped
    detail = {"error": error_code}
    reason_code = result.get("reason_code")
    if isinstance(reason_code, str):
        detail["reason_code"] = reason_code
    raise HTTPException(status_code, detail=detail)


def _s03_not_found(error: QueryNotFound) -> HTTPException:
    return HTTPException(404, detail={"error": "S03_NOT_FOUND"})


def _s03_invalid_command(error: ValueError) -> HTTPException:
    return HTTPException(
        422,
        detail={
            "error": "S03_INVALID_COMMAND",
            "message": "S03 command does not match the registered contract",
        },
    )


def _s05_command_result(result: dict[str, Any]) -> dict[str, Any]:
    status = result.get("status")
    if status in {"accepted", "claimed"}:
        return result
    mapping = {
        "stale": (409, "S05_STALE"),
        "conflict": (409, "S05_CONFLICT"),
        "already_decided": (409, "S05_ALREADY_DECIDED"),
        "already_inactive": (409, "S05_ALREADY_INACTIVE"),
        "not_due": (409, "S05_NOT_DUE"),
        "sealed": (409, "S05_SEALED"),
        "rejected": (409, "S05_REJECTED"),
        "stopped": (503, "S05_STOPPED"),
        "unavailable": (503, "S05_UNAVAILABLE"),
    }
    mapped = mapping.get(status)
    if mapped is None:
        raise RuntimeError("S05 domain returned an unsupported status")
    status_code, error_code = mapped
    detail = {"error": error_code}
    reason_code = result.get("reason_code")
    if isinstance(reason_code, str):
        detail["reason_code"] = reason_code
    raise HTTPException(status_code, detail=detail)


def _s05_not_found(error: QueryNotFound) -> HTTPException:
    return HTTPException(404, detail={"error": "S05_NOT_FOUND"})


def _s05_invalid_command(error: ValueError) -> HTTPException:
    return HTTPException(
        422,
        detail={
            "error": "S05_INVALID_COMMAND",
            "message": "S05 command does not match the registered contract",
        },
    )


def _s07_command_result(result: dict[str, Any]) -> dict[str, Any]:
    status = result.get("status")
    if status == "accepted":
        return result
    mapping = {
        "stale": (409, "S07_STALE"),
        "conflict": (409, "S07_CONFLICT"),
        "rejected": (409, "S07_REJECTED"),
        "unavailable": (503, "S07_UNAVAILABLE"),
    }
    mapped = mapping.get(status)
    if mapped is None:
        raise RuntimeError("S07 domain returned an unsupported status")
    status_code, error_code = mapped
    detail = {"error": error_code}
    reason_code = result.get("reason_code")
    if isinstance(reason_code, str):
        detail["reason_code"] = reason_code
    raise HTTPException(status_code, detail=detail)


def _s07_not_found() -> HTTPException:
    return HTTPException(404, detail={"error": "S07_NOT_FOUND"})


_S07_MEDIA_POLICY = email.policy.default
_S07_JSON_MEDIA_TYPE_PATTERN = re.compile(
    r"""
    (?P<essence>application/json)
    (?:
        [\t ]* ; [\t ]*
        [!#$%&'*+\-.^_`|~0-9A-Za-z]+
        =
        (?:
            [!#$%&'*+\-.^_`|~0-9A-Za-z]+
            |
            "
            (?:
                [\t \x21\x23-\x5b\x5d-\x7e\x80-\xff]
                | \\[\t \x21-\x7e\x80-\xff]
            )*
            "
        )
    )*
    """,
    re.ASCII | re.IGNORECASE | re.VERBOSE,
)


def _s07_request_is_json(content_type: str | None) -> bool:
    """True only when the request Content-Type media-type essence is exactly
    ``application/json`` with well-formed parameters.

    The whole-value matcher enforces HTTP token/quoted-string parameter grammar.
    The stdlib structured parser then validates and normalizes the matched
    essence without applying its different MIME parameter grammar.  Only
    SP/HTAB OWS around semicolons is allowed.  Valid quoted values retain spaces,
    escaped characters, and opaque Latin-1 ``obs-text``.
    """
    if not content_type:
        return False
    media_type = _S07_JSON_MEDIA_TYPE_PATTERN.fullmatch(content_type)
    if media_type is None:
        return False
    message = Message(policy=_S07_MEDIA_POLICY)
    try:
        message["content-type"] = media_type.group("essence")
    except (TypeError, ValueError):
        return False
    header = message["Content-Type"]
    if header is None or header.defects:
        return False
    return message.get_content_type() == "application/json"


def _s07_invalid_command() -> HTTPException:
    return HTTPException(
        422,
        detail={
            "error": "S07_INVALID_COMMAND",
            "message": "VerifyRecovery command does not match the contract",
        },
    )


def _s07_command_too_large() -> HTTPException:
    return HTTPException(
        413,
        detail={
            "error": "S07_INVALID_COMMAND",
            "message": "VerifyRecovery command exceeds the allowed size",
        },
    )


async def _s07_verify_command_body(request: Request) -> S07VerifyRecoveryBody:
    """Authorized-only bounded manual VerifyRecovery body parse.

    This runs only after ``_s07_operator_principal`` has solved, so raw or
    malformed wire bytes never reach it for anonymous or Reviewer callers.
    The exact ``application/json`` media-type essence is required before any
    body byte is read; reads are bounded, JSON is decoded without reflecting
    rejected input, and schema violations surface as the same sanitized
    ``loc``/``msg``/``type`` detail items as the application-wide validation
    handler.
    """
    if not _s07_request_is_json(request.headers.get("content-type")):
        raise HTTPException(
            422,
            detail=[
                {
                    "loc": ["body"],
                    "msg": "expected application/json request content type",
                    "type": "content_type",
                }
            ],
        )
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            if (
                int(declared_length) < 0
                or int(declared_length) > S02_MAX_COMMAND_BYTES
            ):
                raise _s07_command_too_large()
        except ValueError:
            raise _s07_invalid_command() from None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > S02_MAX_COMMAND_BYTES:
            raise _s07_command_too_large()
        chunks.append(chunk)
    try:
        return S07VerifyRecoveryBody.model_validate_json(b"".join(chunks))
    except ValidationError as error:
        raise HTTPException(422, detail=_sanitized_validation_detail(error.errors())) from error


def _s07_operator_principal(request: Request) -> S01CommandPrincipal:
    if not S01_OPERATOR_SUBJECT or not _s01_has_credential(
        request, S01_OPERATOR_CREDENTIAL
    ):
        raise _s07_not_found()
    return S01CommandPrincipal(
        subject=S01_OPERATOR_SUBJECT,
        role="operator",
        scope="C-DEMO",
        source_id="c-demo-recovery-console",
    )


def _s07_recovery_query_principal(request: Request) -> S01CommandPrincipal:
    if _s01_has_credential(request, S01_OPERATOR_CREDENTIAL):
        return _s07_operator_principal(request)
    principal = _s01_principal(request)
    if (
        principal is None
        or "reviewer" not in principal.roles
        or not ControlledScenarioService.is_c_demo_scope(principal.scope)
    ):
        raise _s07_not_found()
    return S01CommandPrincipal(
        subject=principal.subject,
        role="reviewer",
        scope=principal.scope,
        source_id="c-demo-review-console",
        expires_at=principal.expires_at,
    )


@app.get("/controlled/s02", response_class=HTMLResponse)
def controlled_s02_page(request: Request) -> HTMLResponse:
    _s02_service()
    if not S02_TEMPLATE.is_file():
        raise HTTPException(500, detail={"error": "S02_PAGE_UNAVAILABLE"})
    response = HTMLResponse(S02_TEMPLATE.read_text(encoding="utf-8"))
    _issue_s02_session(request, response)
    _s01_disable_cache(response)
    return response


@app.post("/controlled/s02/api/session", status_code=204)
def controlled_s02_session(request: Request, response: Response) -> Response:
    _issue_s02_session(request, response)
    _s01_disable_cache(response)
    response.status_code = 204
    return response


@app.post("/controlled/s02/api/commands/submit")
async def controlled_s02_submit(
    request: Request, response: Response
) -> dict[str, Any]:
    principal = _s02_require_role(request, "integrator")
    _s01_disable_cache(response)
    declared_length = request.headers.get("content-length")
    try:
        if declared_length is not None and (
            int(declared_length) < 0 or int(declared_length) > S02_MAX_COMMAND_BYTES
        ):
            raise HTTPException(
                413,
                detail={
                    "error": "S02_COMMAND_TOO_LARGE",
                    "message": "S02 command exceeds the allowed size",
                },
            )
    except ValueError as error:
        raise HTTPException(
            400,
            detail={"error": "S02_INVALID_COMMAND", "message": "Invalid content length"},
        ) from error
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > S02_MAX_COMMAND_BYTES:
            raise HTTPException(
                413,
                detail={
                    "error": "S02_COMMAND_TOO_LARGE",
                    "message": "S02 command exceeds the allowed size",
                },
            )
        chunks.append(chunk)
    try:
        body = S02SubmitBody.model_validate_json(b"".join(chunks))
    except ValidationError as error:
        raise HTTPException(
            422,
            detail={
                "error": "S02_INVALID_COMMAND",
                "message": "S02 command does not match the registered contract",
            },
        ) from error
    result = _s02_service().submit_registered(
        submission=body.submission,
        idempotency_key=body.idempotency_key,
        principal=S01CommandPrincipal(
            subject=principal.subject,
            role="integrator",
            scope=principal.scope,
            source_id=S02_SOURCE_SYSTEM_ID,
        ),
    )
    return _s02_admission_json(result)


@app.post("/controlled/s02/api/commands/submit-attachment-version")
async def controlled_s06_submit_attachment_version(
    request: Request,
    response: Response,
) -> dict[str, Any]:
    principal = _s02_require_role(request, "integrator")
    _s01_disable_cache(response)
    body = await _s03_command_body(request, S02SubmitBody)
    assert isinstance(body, S02SubmitBody)
    result = _s02_service().submit_attachment_version(
        submission=body.submission,
        idempotency_key=body.idempotency_key,
        principal=S01CommandPrincipal(
            subject=principal.subject,
            role="integrator",
            scope=principal.scope,
            source_id=S02_SOURCE_SYSTEM_ID,
            expires_at=principal.expires_at,
        ),
        now=S01_SESSION_CLOCK(),
    )
    return _s02_admission_json(result)


@app.get("/controlled/s02/api/queries/queue")
def controlled_s02_queue(request: Request, response: Response) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s02_principal(request)
    if (
        principal is None
        or "reviewer" not in principal.roles
        or not ControlledScenarioService.is_registered_scope(principal.scope)
    ):
        if getattr(request.state, "s02_access_ended", False):
            response.headers["X-S02-Access-Ended"] = "1"
        return {"items": [], "recovery_items": [], "projection_watermark": 0}
    return _s02_service().queue_view(
        role="reviewer",
        scope=principal.scope,
        subject=principal.subject,
        now=S01_SESSION_CLOCK(),
    )


@app.get("/controlled/s02/api/queries/applications/{application_id}/workspace")
def controlled_s02_workspace(
    application_id: str,
    request: Request,
    response: Response,
    finding_id: str | None = None,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s02_principal(request)
    if (
        principal is None
        or "reviewer" not in principal.roles
        or not ControlledScenarioService.is_registered_scope(principal.scope)
    ):
        raise HTTPException(404, detail={"error": "S02_NOT_FOUND"})
    try:
        return _s02_service().workspace_view(
            application_id,
            role="reviewer",
            scope=principal.scope,
            subject=principal.subject,
            now=S01_SESSION_CLOCK(),
            finding_id=finding_id,
        )
    except QueryNotFound as error:
        raise HTTPException(404, detail={"error": "S02_NOT_FOUND"}) from error


@app.get("/controlled/s02/api/queries/review-work-items/{work_item_id}")
def controlled_s03_review_work_item(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s03_reviewer_principal(request)
    try:
        return _s02_service().review_work_item_view(
            principal=principal,
            work_item_id=work_item_id,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error


@app.get("/controlled/s02/api/queries/applications/{application_id}/current-route")
def controlled_s04_current_route(
    application_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s03_reviewer_principal(request)
    try:
        return _s02_service().current_route_view(
            principal=principal,
            application_id=application_id,
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error


@app.get("/controlled/s02/api/queries/applications/{application_id}/history")
def controlled_s04_application_history(
    application_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s03_reviewer_principal(request)
    try:
        return _s02_service().application_history_view(
            principal=principal,
            application_id=application_id,
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error


@app.post("/controlled/s02/api/commands/review-work-items/{work_item_id}/claim")
async def controlled_s03_claim_review_work_item(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s03_reviewer_principal(request)
    body = await _s03_command_body(request, S03ClaimBody)
    assert isinstance(body, S03ClaimBody)
    try:
        result = _s02_service().claim_review_work_item(
            principal=principal,
            work_item_id=work_item_id,
            expected_context=body.expected_context,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error
    return _s03_command_result(result)


@app.post("/controlled/s02/api/commands/review-work-items/{work_item_id}/renew")
async def controlled_s03_renew_review_work_item(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s03_reviewer_principal(request)
    body = await _s03_command_body(request, S03FencedBody)
    assert isinstance(body, S03FencedBody)
    try:
        result = _s02_service().renew_review_work_item(
            principal=principal,
            work_item_id=work_item_id,
            expected_fence=body.expected_fence,
            expected_context=body.expected_context,
            idempotency_key=body.idempotency_key,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error
    except ValueError as error:
        raise _s03_invalid_command(error) from error
    return _s03_command_result(result)


@app.post("/controlled/s02/api/commands/review-work-items/{work_item_id}/release")
async def controlled_s03_release_review_work_item(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s03_reviewer_principal(request)
    body = await _s03_command_body(request, S03FencedBody)
    assert isinstance(body, S03FencedBody)
    try:
        result = _s02_service().release_review_work_item(
            principal=principal,
            work_item_id=work_item_id,
            expected_fence=body.expected_fence,
            expected_context=body.expected_context,
            idempotency_key=body.idempotency_key,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error
    except ValueError as error:
        raise _s03_invalid_command(error) from error
    return _s03_command_result(result)


@app.post("/controlled/s02/api/commands/review-work-items/{work_item_id}/submit")
async def controlled_s03_submit_review_work_item(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s03_reviewer_principal(request)
    body = await _s03_command_body(request, S03SubmitBody)
    assert isinstance(body, S03SubmitBody)
    try:
        result = _s02_service().submit_review_work_item(
            principal=principal,
            work_item_id=work_item_id,
            expected_fence=body.expected_fence,
            expected_context=body.expected_context,
            idempotency_key=body.idempotency_key,
            verification=body.verification,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error
    except ValueError as error:
        raise _s03_invalid_command(error) from error
    return _s03_command_result(result)


@app.post(
    "/controlled/s02/api/commands/review-work-items/"
    "{work_item_id}/correct-field-observation"
)
async def controlled_s04_correct_field_observation(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s03_reviewer_principal(request)
    body = await _s03_command_body(request, S04CorrectionBody)
    assert isinstance(body, S04CorrectionBody)
    try:
        result = _s02_service().correct_field_observation(
            principal=principal,
            application_id=body.application_id,
            work_item_id=work_item_id,
            expected_fence=body.expected_fence,
            expected_context=body.expected_context,
            idempotency_key=body.idempotency_key,
            correction=body.correction,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error
    except ValueError as error:
        raise _s03_invalid_command(error) from error
    return _s03_command_result(result)


@app.post("/controlled/s02/api/commands/review-batches/preview")
async def controlled_s03_preview_review_batch(
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s03_reviewer_principal(request)
    body = await _s03_command_body(request, S03BatchPreviewBody)
    assert isinstance(body, S03BatchPreviewBody)
    try:
        result = _s02_service().preview_review_work_item_batch(
            principal=principal,
            items=body.items,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error
    except ValueError as error:
        raise _s03_invalid_command(error) from error
    if result.get("schema_version") == "review-batch-plan/1":
        return result
    return _s03_command_result(result)


@app.post("/controlled/s02/api/commands/review-batches/submit")
async def controlled_s03_submit_review_batch(
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s03_reviewer_principal(request)
    body = await _s03_command_body(request, S03BatchSubmitBody)
    assert isinstance(body, S03BatchSubmitBody)
    try:
        result = _s02_service().submit_review_work_item_batch(
            principal=principal,
            idempotency_key=body.idempotency_key,
            plan=body.plan,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error
    except ValueError as error:
        raise _s03_invalid_command(error) from error
    return _s03_command_result(result)


def _controlled_s01_shell_response(request: Request, html: str) -> HTMLResponse:
    response = HTMLResponse(html)
    if _s01_has_credential(request, S01_OPERATOR_CREDENTIAL):
        _s01_require_operator(request)
    else:
        _issue_s01_session(request, response)
    _s01_disable_cache(response)
    return response


_REACT_MODULE_ENTRY_TYPE = "module"


class _ReactIndexReferences(HTMLParser):
    """Order-independent, fail-closed extraction of browser-loaded
    script/stylesheet references.

    Attribute names are ASCII case-insensitive and any duplicated attribute
    is rejected as ambiguous (browsers keep the first duplicate while a
    dictionary keeps the last).  Link-type tokens are ASCII case-insensitive
    and the module-entry keyword is normalized before comparison.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.script_srcs: list[str] = []
        self.stylesheet_hrefs: list[str] = []
        self.module_entry_srcs: list[str] = []
        self.ambiguous: bool = False

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        names = [name.lower() for name, _value in attrs]
        if len(names) != len(set(names)):
            self.ambiguous = True
            return
        attributes = {name: value for name, value in attrs}
        if tag == "script":
            src = attributes.get("src")
            if isinstance(src, str) and src:
                self.script_srcs.append(src)
                script_type = attributes.get("type")
                if (
                    isinstance(script_type, str)
                    and script_type.strip().lower() == _REACT_MODULE_ENTRY_TYPE
                    and src.endswith(".js")
                ):
                    self.module_entry_srcs.append(src)
        elif tag == "link":
            rel = attributes.get("rel")
            href = attributes.get("href")
            if (
                isinstance(rel, str)
                and "stylesheet" in rel.lower().split()
                and isinstance(href, str)
                and href
            ):
                self.stylesheet_hrefs.append(href)


def _react_local_asset_path_is_clean(root: Path, url_path: str) -> bool:
    parsed = urlparse(url_path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not url_path.startswith("/static/react/")
    ):
        return False
    relative = url_path.removeprefix("/static/react/")
    if not relative or relative.startswith("/"):
        return False
    candidate = (root / relative).resolve()
    return candidate.is_relative_to(root) and candidate.is_file()


def _react_build_is_complete(index_html: str) -> bool:
    """True only for a complete local Vite production React build.

    Tags are parsed order-independently and fail closed on ambiguous
    (duplicated) attributes.  Every browser-loaded local script and
    stylesheet reference must be a clean absolute path under
    ``/static/react/`` with no scheme/netloc/query/fragment or traversal,
    must resolve to a file inside the React build root, and at least one
    unambiguous ``type="module"`` JavaScript entry must be present — absent,
    classic, or ``text/javascript`` scripts do not satisfy the entry
    requirement for the Vite production shell.
    """
    parser = _ReactIndexReferences()
    parser.feed(index_html)
    root = S01_REACT_INDEX.parent.resolve()
    if parser.ambiguous or not parser.module_entry_srcs:
        return False
    references = parser.stylesheet_hrefs + parser.script_srcs
    return all(_react_local_asset_path_is_clean(root, url) for url in references)


@app.get("/controlled/s01", response_class=HTMLResponse)
def controlled_s01_page(request: Request) -> HTMLResponse:
    _s01_service()
    if not S01_TEMPLATE.is_file():
        raise HTTPException(500, detail={"error": "S01_PAGE_UNAVAILABLE"})
    return _controlled_s01_shell_response(
        request, S01_TEMPLATE.read_text(encoding="utf-8")
    )


@app.get("/controlled/s01/react", response_class=HTMLResponse)
def controlled_s01_react_page(request: Request) -> HTMLResponse:
    _s01_service()
    if not S01_REACT_INDEX.is_file():
        raise HTTPException(
            503,
            detail={
                "error": "S01_REACT_UNAVAILABLE",
                "message": "Controlled S01 React shell is not built",
            },
        )
    index_html = S01_REACT_INDEX.read_text(encoding="utf-8")
    if not _react_build_is_complete(index_html):
        if not S01_TEMPLATE.is_file():
            raise HTTPException(
                503,
                detail={
                    "error": "S01_REACT_UNAVAILABLE",
                    "message": "Controlled S01 React shell is not built",
                },
            )
        return _controlled_s01_shell_response(
            request, S01_TEMPLATE.read_text(encoding="utf-8")
        )
    return _controlled_s01_shell_response(request, index_html)


@app.post("/controlled/s01/api/session", status_code=204)
def controlled_s01_session(request: Request, response: Response) -> Response:
    _issue_s01_session(request, response)
    _s01_disable_cache(response)
    response.status_code = 204
    return response


@app.post("/controlled/s01/api/commands/submit")
def controlled_s01_submit(
    body: S01SubmitBody, request: Request, response: Response
) -> dict[str, Any]:
    principal = _s01_require_role(request, "integrator")
    _s01_disable_cache(response)
    result = _s01_service().submit_demo(
        scenario_id=body.scenario_id,
        idempotency_key=body.idempotency_key,
        principal=S01CommandPrincipal(
            subject=principal.subject,
            role="integrator",
            scope=principal.scope,
            source_id="c-demo-web-session",
        ),
    )
    return _s01_admission_json(result)


@app.post("/controlled/s01/api/workbench/commands/submit")
def controlled_s01_workbench_submit(
    body: S01SubmitBody, request: Request, response: Response
) -> dict[str, Any]:
    principal = _s01_require_role(request, "integrator")
    _s01_disable_cache(response)
    result = _s01_service().submit_demo(
        scenario_id=body.scenario_id,
        idempotency_key=body.idempotency_key,
        principal=S01CommandPrincipal(
            subject=principal.subject,
            role="integrator",
            scope=principal.scope,
            source_id="c-demo-web-session",
        ),
    )
    return _s01_workbench_admission_json(result)


@app.post("/controlled/s01/api/_test/commands/process")
def controlled_s01_test_process(
    body: S01ProcessBody, request: Request, response: Response
) -> dict[str, Any]:
    _s01_disable_cache(response)
    if S01_TEST_DRIVER is None:
        raise HTTPException(404, detail={"error": "S01_NOT_FOUND"})
    result = S01_TEST_DRIVER.process_next_job(
        worker_id=body.worker_id,
        now=body.now,
        crash=body.crash,
        partial=body.partial,
        stale=body.stale,
        cas_fault=body.cas_fault,
        duplicate=body.duplicate,
    )
    return _s01_worker_json(result)


@app.post("/controlled/s01/api/commands/stop-new-cohort")
def controlled_s01_stop_new_cohort(request: Request, response: Response) -> dict[str, str]:
    principal = _s01_require_operator(request)
    _s01_disable_cache(response)
    return _s01_service().stop_new_cohort(
        principal=S01CommandPrincipal(
            subject=principal.subject,
            role="operator",
            scope=principal.scope,
            source_id="c-demo-operator-control-plane",
        )
    )


@app.post("/controlled/s01/api/commands/recover-runtime")
def controlled_s01_recover_runtime(
    body: S01RecoveryBody, request: Request, response: Response
) -> dict[str, str | int]:
    principal = _s01_require_operator(request)
    _s01_disable_cache(response)
    return _s01_service().recover_runtime(
        expected_failure_reason_code=body.expected_failure_reason_code,
        principal=S01CommandPrincipal(
            subject=principal.subject,
            role="operator",
            scope=principal.scope,
            source_id="c-demo-operator-control-plane",
        ),
    )


@app.post("/controlled/s01/api/_test/commands/project")
def controlled_s01_test_project(response: Response) -> dict[str, int]:
    _s01_disable_cache(response)
    if S01_TEST_DRIVER is None:
        raise HTTPException(404, detail={"error": "S01_NOT_FOUND"})
    return _s01_service().refresh_projection()


@app.get(
    "/controlled/s01/api/queries/recovery-work-items/{recovery_work_id}",
    response_model=S01RecoveryWorkResponse,
    responses={
        404: {"model": S01ErrorResponse},
    },
)
def controlled_s07_recovery_work(
    recovery_work_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s07_recovery_query_principal(request)
    try:
        return _s01_service().recovery_work_view(
            principal=principal,
            recovery_work_id=recovery_work_id,
        )
    except QueryNotFound as error:
        raise _s07_not_found() from error


@app.post(
    "/controlled/s01/api/commands/recovery-work-items/{recovery_work_id}/verify",
    response_model=S01VerifyRecoveryResult,
    response_model_exclude_none=True,
    responses={
        404: {"model": S01ErrorResponse},
        409: {"model": S01ErrorResponse},
        413: {"model": S01ErrorResponse},
        422: {"model": S01VerifyErrorResponse},
        503: {"model": S01ErrorResponse},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": S07VerifyRecoveryBody.model_json_schema(),
                }
            },
        },
    },
)
async def controlled_s07_verify_recovery(
    recovery_work_id: str,
    request: Request,
    response: Response,
    principal: S01CommandPrincipal = Depends(_s07_operator_principal),
) -> dict[str, Any]:
    _s01_disable_cache(response)
    body = await _s07_verify_command_body(request)
    try:
        result = _s01_service().verify_recovery(
            principal=principal,
            recovery_work_id=recovery_work_id,
            expected_lifecycle_revision=body.expected_lifecycle_revision,
            expected_criterion_digest=body.expected_criterion_digest,
            idempotency_key=body.idempotency_key,
        )
    except QueryNotFound as error:
        raise _s07_not_found() from error
    except ValueError as error:
        raise _s07_invalid_command() from error
    return _s07_command_result(result)


@app.get(
    "/controlled/s01/api/queries/queue",
    response_model=S01QueueResponse,
    response_model_exclude_none=True,
    responses={
        200: {
            "headers": {
                "X-S01-Access-Ended": {
                    "schema": {"type": "string", "enum": ["1"]},
                    "description": (
                        "Set to 1 when the session expired or is invalid; the "
                        "response then hides all work (empty collections)."
                    ),
                }
            }
        },
        503: {"model": S01ErrorResponse},
    },
)
def controlled_s01_queue(request: Request, response: Response) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s01_principal(request)
    if (
        principal is None
        or "reviewer" not in principal.roles
        or not ControlledScenarioService.is_c_demo_scope(principal.scope)
    ):
        access_ended = bool(getattr(request.state, "s01_access_ended", False))
        if access_ended:
            response.headers["X-S01-Access-Ended"] = "1"
        return {
            "items": [],
            "recovery_items": [],
            "projection_watermark": 0,
            "access_ended": True if access_ended else None,
        }
    try:
        return _s01_service().queue_view(
            role="reviewer",
            scope=principal.scope,
            subject=principal.subject,
            now=S01_SESSION_CLOCK(),
        )
    except _ApplicationStateAuthorityUnavailable:
        raise HTTPException(
            503,
            detail={
                "error": "S01_QUEUE_UNAVAILABLE",
                "reason_code": "recovery.authority_unavailable",
            },
        ) from None


@app.get("/controlled/s01/api/queries/applications/{application_id}/workspace")
def controlled_s01_workspace(
    application_id: str,
    request: Request,
    response: Response,
    finding_id: str | None = None,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s01_principal(request)
    if (
        principal is None
        or "reviewer" not in principal.roles
        or not ControlledScenarioService.is_c_demo_scope(principal.scope)
    ):
        raise HTTPException(404, detail={"error": "S01_NOT_FOUND"})
    try:
        return _s01_service().workspace_view(
            application_id,
            role="reviewer",
            scope=principal.scope,
            subject=principal.subject,
            now=S01_SESSION_CLOCK(),
            finding_id=finding_id,
        )
    except QueryNotFound as error:
        raise HTTPException(404, detail={"error": "S01_NOT_FOUND"}) from error


@app.get("/controlled/s01/api/queries/review-work-items/{work_item_id}")
def controlled_s04_demo_review_work_item(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    try:
        return _s01_service().review_work_item_view(
            principal=principal,
            work_item_id=work_item_id,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error


@app.post("/controlled/s01/api/commands/review-work-items/{work_item_id}/claim")
async def controlled_s04_demo_claim_review_work_item(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    body = await _s03_command_body(request, S03ClaimBody)
    assert isinstance(body, S03ClaimBody)
    try:
        result = _s01_service().claim_review_work_item(
            principal=principal,
            work_item_id=work_item_id,
            expected_context=body.expected_context,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error
    return _s03_command_result(result)


@app.post(
    "/controlled/s01/api/commands/review-work-items/"
    "{work_item_id}/reveal-field-observation"
)
async def controlled_s04_demo_reveal_field_observation(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    body = await _s03_command_body(request, S04RevealBody)
    assert isinstance(body, S04RevealBody)
    try:
        result = _s01_service().reveal_field_observation(
            principal=principal,
            application_id=body.application_id,
            work_item_id=work_item_id,
            observation_id=body.observation_id,
            expected_fence=body.expected_fence,
            expected_context=body.expected_context,
            idempotency_key=body.idempotency_key,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error
    except ValueError as error:
        raise _s03_invalid_command(error) from error
    if result.get("status") == "revealed":
        return result
    return _s03_command_result(result)


@app.post(
    "/controlled/s01/api/commands/review-work-items/"
    "{work_item_id}/correct-field-observation"
)
async def controlled_s04_demo_correct_field_observation(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    body = await _s03_command_body(request, S04CorrectionBody)
    assert isinstance(body, S04CorrectionBody)
    try:
        result = _s01_service().correct_field_observation(
            principal=principal,
            application_id=body.application_id,
            work_item_id=work_item_id,
            expected_fence=body.expected_fence,
            expected_context=body.expected_context,
            idempotency_key=body.idempotency_key,
            correction=body.correction,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error
    except ValueError as error:
        raise _s03_invalid_command(error) from error
    return _s03_command_result(result)


@app.post(
    "/controlled/s01/api/commands/review-work-items/"
    "{work_item_id}/supplement"
)
async def controlled_s06_request_supplement(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    body = await _s03_command_body(request, S05RequestBody)
    assert isinstance(body, S05RequestBody)
    try:
        result = _s01_service().request_supplement(
            principal=principal,
            work_item_id=work_item_id,
            finding_id=body.finding_id,
            reason_code=body.reason_code,
            predecessor_request_id=body.predecessor_request_id,
            expected_fence=body.expected_fence,
            expected_context=body.expected_context,
            idempotency_key=body.idempotency_key,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error
    except ValueError as error:
        raise _s03_invalid_command(error) from error
    return _s03_command_result(result)


@app.get("/controlled/s01/api/queries/supplement-requests/{request_id}")
def controlled_s06_supplement_request_view(
    request_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    try:
        return _s01_service().supplement_request_view(
            principal=principal,
            request_id=request_id,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error


@app.post(
    "/controlled/s01/api/commands/review-work-items/"
    "{work_item_id}/business-exceptions"
)
async def controlled_s05_request_business_exception(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    body = await _s03_command_body(request, S05RequestBody)
    assert isinstance(body, S05RequestBody)
    try:
        result = _s01_service().request_business_exception(
            principal=principal,
            work_item_id=work_item_id,
            finding_id=body.finding_id,
            reason_code=body.reason_code,
            predecessor_request_id=body.predecessor_request_id,
            expected_fence=body.expected_fence,
            expected_context=body.expected_context,
            idempotency_key=body.idempotency_key,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s05_not_found(error) from error
    except ValueError as error:
        raise _s05_invalid_command(error) from error
    return _s05_command_result(result)


@app.get(
    "/controlled/s01/api/queries/business-exceptions/{request_id}"
)
def controlled_s05_business_exception_view(
    request_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s05_exception_approver_principal(request)
    try:
        return _s01_service().business_exception_view(
            principal=principal,
            request_id=request_id,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s05_not_found(error) from error


@app.post(
    "/controlled/s01/api/commands/exception-work-items/{work_item_id}/claim"
)
async def controlled_s05_claim_exception_work_item(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s05_exception_approver_principal(request)
    body = await _s03_command_body(request, S03ClaimBody)
    assert isinstance(body, S03ClaimBody)
    try:
        result = _s01_service().claim_exception_work_item(
            principal=principal,
            work_item_id=work_item_id,
            expected_context=body.expected_context,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s05_not_found(error) from error
    return _s05_command_result(result)


@app.post(
    "/controlled/s01/api/commands/business-exceptions/{request_id}/decide"
)
async def controlled_s05_decide_business_exception(
    request_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s05_exception_approver_principal(request)
    body = await _s03_command_body(request, S05DecisionBody)
    assert isinstance(body, S05DecisionBody)
    try:
        result = _s01_service().decide_business_exception(
            principal=principal,
            request_id=request_id,
            work_item_id=body.work_item_id,
            decision=body.decision,
            reason_code=body.reason_code,
            expected_fence=body.expected_fence,
            expected_context=body.expected_context,
            idempotency_key=body.idempotency_key,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s05_not_found(error) from error
    except ValueError as error:
        raise _s05_invalid_command(error) from error
    return _s05_command_result(result)


@app.post(
    "/controlled/s01/api/commands/business-exceptions/{request_id}/route"
)
async def controlled_s05_route_business_exception(
    request_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s05_router_principal(request)
    body = await _s03_command_body(request, S05ContextBody)
    assert isinstance(body, S05ContextBody)
    try:
        result = _s01_service().determine_business_exception_route(
            principal=principal,
            request_id=request_id,
            expected_context=body.expected_context,
            idempotency_key=body.idempotency_key,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s05_not_found(error) from error
    except ValueError as error:
        raise _s05_invalid_command(error) from error
    return _s05_command_result(result)


@app.post(
    "/controlled/s01/api/commands/business-exceptions/{request_id}/expire"
)
async def controlled_s05_expire_business_exception(
    request_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s05_router_principal(request)
    body = await _s03_command_body(request, S05ContextBody)
    assert isinstance(body, S05ContextBody)
    try:
        result = _s01_service().expire_business_exception(
            principal=principal,
            request_id=request_id,
            expected_context=body.expected_context,
            idempotency_key=body.idempotency_key,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s05_not_found(error) from error
    except ValueError as error:
        raise _s05_invalid_command(error) from error
    return _s05_command_result(result)


@app.post(
    "/controlled/s01/api/commands/business-exceptions/{request_id}/invalidate"
)
async def controlled_s05_invalidate_business_exception(
    request_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s05_router_principal(request)
    body = await _s03_command_body(request, S05InvalidationBody)
    assert isinstance(body, S05InvalidationBody)
    try:
        result = _s01_service().invalidate_business_exception(
            principal=principal,
            request_id=request_id,
            reason_code=body.reason_code,
            expected_context=body.expected_context,
            idempotency_key=body.idempotency_key,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s05_not_found(error) from error
    except ValueError as error:
        raise _s05_invalid_command(error) from error
    return _s05_command_result(result)


@app.get(
    "/controlled/s01/api/queries/business-exception-operations"
)
def controlled_s05_business_exception_operations_status(
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s05_router_principal(request)
    try:
        return _s01_service().business_exception_operations_status(
            principal=principal,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s05_not_found(error) from error


@app.post(
    "/controlled/s01/api/commands/business-exception-operations/close"
)
async def controlled_s05_close_business_exception_operations(
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s05_router_principal(request)
    body = await _s03_command_body(request, S05OperationsBody)
    assert isinstance(body, S05OperationsBody)
    try:
        result = _s01_service().close_business_exception_operations(
            principal=principal,
            idempotency_key=body.idempotency_key,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s05_not_found(error) from error
    except ValueError as error:
        raise _s05_invalid_command(error) from error
    return _s05_command_result(result)


@app.post(
    "/controlled/s01/api/commands/business-exception-operations/resume"
)
async def controlled_s05_resume_business_exception_operations(
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s05_router_principal(request)
    body = await _s03_command_body(request, S05OperationsBody)
    assert isinstance(body, S05OperationsBody)
    try:
        result = _s01_service().resume_business_exception_operations(
            principal=principal,
            idempotency_key=body.idempotency_key,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s05_not_found(error) from error
    except ValueError as error:
        raise _s05_invalid_command(error) from error
    return _s05_command_result(result)


@app.get(
    "/controlled/s01/api/queries/applications/{application_id}/current-route",
    response_model=S01CurrentRouteResponse,
    responses={
        404: {"model": S01ErrorResponse},
    },
)
def controlled_s04_demo_current_route(
    application_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    try:
        return _s01_service().current_route_view(
            principal=principal,
            application_id=application_id,
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error


@app.get("/controlled/s01/api/queries/applications/{application_id}/history")
def controlled_s04_demo_application_history(
    application_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    try:
        return _s01_service().application_history_view(
            principal=principal,
            application_id=application_id,
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error


@app.get(
    "/controlled/s01/api/queries/applications/{application_id}/audit-timeline"
)
def controlled_s01_audit_timeline(
    application_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s01_require_auditor(request)
    try:
        return _s01_service().audit_timeline(
            principal=S01CommandPrincipal(
                subject=principal.subject,
                role="auditor",
                scope=principal.scope,
                source_id="c-demo-audit-console",
            ),
            application_id=application_id,
        )
    except QueryNotFound as error:
        raise HTTPException(404, detail={"error": "S01_NOT_FOUND"}) from error
    except RuntimeError as error:
        raise HTTPException(
            503, detail={"error": "S01_AUDIT_INTEGRITY_FAILED"}
        ) from error


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/health")
def health() -> dict[str, Any]:
    """Liveness + config pointers (Round22: rules_path / kb_ok / version / audit)."""
    p = _active_rules_path()
    cfg = load_rules(p)
    token_on = bool(os.environ.get("TASK4_WEB_TOKEN", "").strip())
    kb_ok = False
    kb_err: str | None = None
    try:
        kb = get_kb()
        _ = kb.list_section("org_aliases")
        kb_ok = True
    except Exception as e:
        kb_err = str(e)
    astat = audit_status()
    ok = kb_ok
    try:
        from task4_consistency import __version__ as lib_version
    except Exception:
        lib_version = "unknown"
    return {
        "ok": ok,
        "rules_path": _rel_to_root(p),
        "package": cfg.package,
        "version": cfg.version,  # rules YAML package version
        "lib_version": lib_version,  # task4_consistency / pyproject version
        "kb_ok": kb_ok,
        "kb_error": kb_err,
        "auth_required": token_on,
        "audit": {
            "path": astat["path"],
            "exists": astat["exists"],
            "size_bytes": astat["size_bytes"],
        },
        "audit_log": str(audit_log_path()),
    }


@app.get("/api/audit/recent")
def audit_recent(limit: int = 20) -> dict[str, Any]:
    """Tail audit JSONL for ops readability (demo; open unless TASK4_WEB_TOKEN)."""
    limit = max(1, min(int(limit or 20), 200))
    events = read_audit_tail(limit)
    return {
        "path": str(audit_log_path()),
        "n": len(events),
        "events": events,
    }



@app.get("/api/fixtures")
def list_fixtures() -> dict[str, Any]:
    items = []
    for fp in sorted(FIXTURES.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
            items.append(
                {
                    "file": fp.name,
                    "application_id": data.get("application_id"),
                    "label": data.get("label"),
                    "step2_sample_id": meta.get("step2_sample_id"),
                    "field_source": meta.get("field_source"),
                }
            )
        except Exception:
            items.append({"file": fp.name, "application_id": None, "label": None})
    return {"fixtures": items}


@app.get("/api/step2/samples")
def list_step2_samples() -> dict[str, Any]:
    """List competition-side page_order extractions (bboxes, no OCR text)."""
    step2_dir = ROOT / "data" / "step2"
    items: list[dict[str, Any]] = []
    if not step2_dir.is_dir():
        return {"samples": [], "note": "data/step2 missing"}
    for fp in sorted(step2_dir.glob("*_page_order.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            sid = data.get("sample_id") or fp.name.replace("_page_order.json", "")
            stats = data.get("statistics") or {}
            # fixtures linked to this sample
            linked = []
            for fx in sorted(FIXTURES.glob("*.json")):
                try:
                    app = json.loads(fx.read_text(encoding="utf-8"))
                    meta = app.get("meta") if isinstance(app.get("meta"), dict) else {}
                    if meta.get("step2_sample_id") == sid:
                        linked.append(fx.name)
                except Exception:
                    pass
            items.append(
                {
                    "sample_id": sid,
                    "file": fp.name,
                    "n_pages": len(data.get("pages") or []),
                    "page_type_counts": stats.get("page_type_counts") or {},
                    "linked_fixtures": linked[:12],
                    "n_linked_fixtures": len(linked),
                }
            )
        except Exception as e:
            items.append({"file": fp.name, "error": str(e)})
    return {
        "samples": items,
        "note": "step2 来自赛题影像的页序/检测框提取，无 OCR 文本；任务4演示用结构化字段模拟多单据交叉。",
    }


@app.get("/api/ocr_inbox")
def list_ocr_inbox() -> dict[str, Any]:
    """List step2→OCR slot manifests (raw usually null until external OCR)."""
    inbox = ROOT / "fixtures" / "ocr_inbox"
    items: list[dict[str, Any]] = []
    if inbox.is_dir():
        for fp in sorted(inbox.glob("step2_slots_*.json")):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                slots = data.get("slots") or []
                filled = sum(1 for s in slots if s.get("raw"))
                items.append(
                    {
                        "file": fp.name,
                        "sample_id": data.get("sample_id"),
                        "n_slots": data.get("n_slots") or len(slots),
                        "n_filled": filled,
                        "note": data.get("note"),
                    }
                )
            except Exception as e:
                items.append({"file": fp.name, "error": str(e)})
    return {
        "items": items,
        "note": "raw 为空表示待外部 OCR；见 docs/STEP2_TO_TASK4_PIPELINE.md",
    }


@app.get("/api/step2/{sample_id}")
def get_step2_sample(sample_id: str) -> dict[str, Any]:
    if "/" in sample_id or ".." in sample_id:
        raise HTTPException(400, "invalid sample_id")
    fp = ROOT / "data" / "step2" / f"{sample_id}_page_order.json"
    if not fp.exists():
        raise HTTPException(404, "step2 sample not found")
    data = json.loads(fp.read_text(encoding="utf-8"))
    # compact detections for UI
    pages_out = []
    for p in data.get("pages") or []:
        classes = sorted(
            {
                d.get("class_name_cn")
                for d in (p.get("detections") or [])
                if d.get("class_name_cn")
            }
        )
        pages_out.append(
            {
                "order": p.get("order"),
                "filename": p.get("filename"),
                "page_type": p.get("page_type"),
                "page_numbers": p.get("page_numbers"),
                "detected_fields": classes,
            }
        )
    return {
        "sample_id": data.get("sample_id") or sample_id,
        "pages": pages_out,
        "statistics": data.get("statistics"),
        "note": "检测框类别可用于理解登记证上有哪些字段区域；跨单据一致性仍需多源结构化值。",
    }


@app.get("/api/fixtures/{name}")
def get_fixture(name: str) -> dict[str, Any]:
    if "/" in name or ".." in name:
        raise HTTPException(400, "invalid name")
    fp = FIXTURES / name
    if not fp.exists():
        raise HTTPException(404, "fixture not found")
    return json.loads(fp.read_text(encoding="utf-8"))


@app.post("/api/check")
def api_check(body: CheckBody) -> dict[str, Any]:
    if not isinstance(body.application, dict):
        raise HTTPException(
            400,
            detail={
                "error": "invalid_application_type",
                "message": "application 必须是 JSON 对象",
                "hint": "顶层字段: application_id, documents[], 可选 expected_verdicts",
            },
        )
    if "documents" not in body.application:
        raise HTTPException(
            400,
            detail={
                "error": "missing_documents",
                "message": "application.documents 缺失",
                "hint": "documents 为单据数组，每项含 doc_type 与 fields",
            },
        )
    try:
        app_obj = Application.from_dict(body.application)
    except Exception as e:
        raise HTTPException(
            400,
            detail={
                "error": "invalid_application",
                "message": f"申请单解析失败: {e}",
                "hint": "检查 documents[].fields 值是否为 {{raw, confidence}} 或纯字符串",
            },
        ) from e
    rules_path = Path(body.rules_path) if body.rules_path else _active_rules_path()
    if not rules_path.is_absolute():
        rules_path = ROOT / rules_path
    if not rules_path.exists():
        raise HTTPException(
            400,
            detail={
                "error": "rules_not_found",
                "message": f"规则文件不存在: {rules_path}",
                "hint": "省略 rules_path 使用当前激活规则包",
            },
        )
    try:
        eng = RuleEngine(load_rules(rules_path))
        report = eng.run(app_obj)
    except Exception as e:
        raise HTTPException(
            500,
            detail={
                "error": "check_failed",
                "message": f"校验执行失败: {e}",
            },
        ) from e
    return {
        "report": report.to_dict(),
        "html": report_to_html(report),
        "rules_path": _rel_to_root(rules_path),
    }


# Demo batch check soft cap (Arch Round26: no job queue / no async evaluate batch)
BATCH_CHECK_MAX_N = 50


class BatchCheckBody(BaseModel):
    """Run check on multiple applications (inline or fixture filenames).

    Soft limit: BATCH_CHECK_MAX_N (default 50). For full labeled metrics use CLI
    ``evaluate --suite main`` — there is no ``/api/evaluate/batch`` or job queue.
    """

    applications: list[dict[str, Any]] | None = None
    fixture_files: list[str] | None = None


@app.post("/api/check/batch")
def api_check_batch(body: BatchCheckBody) -> dict[str, Any]:
    apps: list[dict[str, Any]] = list(body.applications or [])
    for name in body.fixture_files or []:
        if "/" in name or ".." in name:
            raise HTTPException(400, f"invalid fixture name: {name}")
        fp = FIXTURES / name
        if not fp.exists():
            raise HTTPException(404, f"fixture not found: {name}")
        apps.append(json.loads(fp.read_text(encoding="utf-8")))
    if not apps:
        raise HTTPException(400, "applications or fixture_files required")
    if len(apps) > BATCH_CHECK_MAX_N:
        raise HTTPException(
            400,
            detail={
                "error": "batch_too_large",
                "message": f"batch size {len(apps)} exceeds max {BATCH_CHECK_MAX_N}",
                "hint": (
                    f"拆分批次（建议 ≤{BATCH_CHECK_MAX_N}）；"
                    "全量带标签评估请用 CLI: "
                    "python -m task4_consistency evaluate --suite main -c configs/rules_auto_lease.yaml"
                    "（无 /api/evaluate/batch，无异步 job 队列）"
                ),
                "max_n": BATCH_CHECK_MAX_N,
            },
        )

    eng = _engine()
    results = []
    tot = {"consistent": 0, "inconsistent": 0, "uncertain": 0, "skipped": 0}
    for raw in apps:
        try:
            app_obj = Application.from_dict(raw)
            report = eng.run(app_obj)
            s = report.summary
            tot["consistent"] += s.consistent
            tot["inconsistent"] += s.inconsistent
            tot["uncertain"] += s.uncertain
            tot["skipped"] += s.skipped
            fails = [
                {
                    "rule_id": c.rule_id,
                    "verdict": c.verdict.value,
                    "message": c.message,
                    "reason_codes": list(c.reason_codes or []),
                }
                for c in report.checks
                if c.verdict.value in ("inconsistent", "uncertain")
            ]
            results.append(
                {
                    "application_id": report.application_id,
                    "summary": s.to_dict(),
                    "issues": fails,
                }
            )
        except Exception as e:
            results.append(
                {
                    "application_id": raw.get("application_id"),
                    "error": str(e),
                }
            )
    return {
        "n": len(results),
        "totals": tot,
        "results": results,
        "rules_path": _rel_to_root(_active_rules_path()),
    }


@app.get("/api/evaluate/summary")
def api_evaluate_summary(suite: str = "main") -> dict[str, Any]:
    """Run evaluate by suite (default main). Round19/20 honesty: only main is delivery."""
    from task4_consistency.evaluate import evaluate_suite, metrics_to_html

    suite = (suite or "main").lower()
    if suite not in {"main", "semi", "all"}:
        raise HTTPException(
            400,
            detail={
                "error": "bad_suite",
                "message": f"suite must be main|semi|all, got {suite!r}",
                "hint": "交付数字只用 suite=main",
            },
        )
    metrics = evaluate_suite(suite, _active_rules_path())
    return {
        "metrics": metrics.to_dict(),
        "html": metrics_to_html(metrics),
        "rules_path": _rel_to_root(_active_rules_path()),
        "suite": suite,
    }


@app.get("/api/rules")
def get_rules() -> dict[str, Any]:
    path = _active_rules_path()
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    return {
        "path": _rel_to_root(path),
        "is_runtime": path == RUNTIME_RULES,
        "yaml_text": text,
        "content": data,
    }


@app.post("/api/rules/validate")
def validate_rules(body: RulesBody) -> dict[str, Any]:
    """Dry-run rule package validation without writing runtime_rules.yaml."""
    _data, yaml_text = _parse_rules_payload(body)
    cfg = _validate_rules_yaml(yaml_text)
    return {
        "ok": True,
        "package": cfg.package,
        "version": cfg.version,
        "n_rules": len(cfg.rules),
        "critical_fingerprints": fingerprints_as_dicts(),
        "message": "规则校验通过（未写入磁盘）",
    }


@app.put("/api/rules")
def put_rules(body: RulesBody) -> dict[str, Any]:
    """Atomic save: validate+fingerprint first; then lock → tmp+fsync+replace.

    ARCH Round16 W1: on any failure **never touch** active runtime_rules.yaml
    (no write_text rollback).
    """
    _data, yaml_text = _parse_rules_payload(body)
    # full validation BEFORE lock / BEFORE any write near active path
    cfg = _validate_rules_yaml(yaml_text)

    RUNTIME_RULES.parent.mkdir(parents=True, exist_ok=True)
    if not RUNTIME_RULES.exists() and DEFAULT_RULES.exists():
        bak = ROOT / "configs" / "rules_auto_lease.yaml.bak"
        if not bak.exists():
            shutil.copy2(DEFAULT_RULES, bak)

    tmp_path = RUNTIME_RULES.with_suffix(".yaml.tmp")
    with RULES_WRITE_LOCK:
        try:
            # write only to sibling tmp; fsync; then atomic replace
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(yaml_text)
                fh.flush()
                os.fsync(fh.fileno())
            # re-validate from the bytes about to become active
            cfg2 = load_rules(tmp_path)
            enforce_critical_fingerprints(cfg2)
            os.replace(tmp_path, RUNTIME_RULES)
            cfg = cfg2
        except HTTPException:
            tmp_path.unlink(missing_ok=True)
            raise
        except CriticalGuardError as e:
            tmp_path.unlink(missing_ok=True)
            write_audit(
                "rules_save",
                ok=False,
                detail={"error": e.error, "message": str(e)},
            )
            raise HTTPException(
                400,
                detail={
                    "error": e.error,
                    "message": str(e),
                    "hint": "critical 指纹未通过；active runtime 未改动",
                },
            ) from e
        except Exception as e:
            # failure: zero touch active (delete tmp only)
            tmp_path.unlink(missing_ok=True)
            write_audit(
                "rules_save",
                ok=False,
                detail={"error": "rules_save_failed", "message": str(e)},
            )
            raise HTTPException(
                400,
                detail={
                    "error": "rules_save_failed",
                    "message": f"规则保存失败（active 未改动）: {e}",
                    "hint": "修复 YAML 后重试；当前仍使用上一版 runtime 或默认包",
                },
            ) from e

    write_audit(
        "rules_save",
        ok=True,
        detail={
            "path": _rel_to_root(RUNTIME_RULES),
            "package": cfg.package,
            "version": cfg.version,
            "n_rules": len(cfg.rules),
        },
    )
    return {
        "ok": True,
        "path": _rel_to_root(RUNTIME_RULES),
        "package": cfg.package,
        "version": cfg.version,
        "n_rules": len(cfg.rules),
        "message": "规则已保存并通过校验与 critical 指纹",
    }


@app.post("/api/rules/reset")
def reset_rules() -> dict[str, Any]:
    with RULES_WRITE_LOCK:
        existed = RUNTIME_RULES.exists()
        if existed:
            RUNTIME_RULES.unlink()
    write_audit("rules_reset", ok=True, detail={"had_runtime": existed})
    return {"ok": True, "active": _rel_to_root(_active_rules_path())}


@app.get("/api/kb/graph")
def kb_graph() -> dict[str, Any]:
    """Lightweight entity graph (synonym/part_of) for demo + future linking."""
    data = get_kb().to_dict()
    g = data.get("graph") if isinstance(data, dict) else None
    if not g:
        return {"graph": {"nodes": [], "edges": []}, "note": "no graph section"}
    return {"graph": g, "note": "same_as 边会投影到 address/org 别名供 normalize 使用"}


@app.get("/api/kb")
def kb_get() -> dict[str, Any]:
    return get_kb().to_dict()


@app.post("/api/kb")
def kb_add(item: KBItem) -> dict[str, Any]:
    try:
        get_kb().add_alias(item.section, item.key, item.value)
    except KeyError as e:
        raise HTTPException(
            400,
            detail={
                "error": "unknown_section",
                "message": str(e),
                "hint": f"section 仅支持 {sorted(_KB_SECTIONS)}",
            },
        ) from e
    except ValueError as e:
        raise HTTPException(
            400,
            detail={
                "error": "invalid_kb_item",
                "message": str(e),
                "hint": "key 与 value 均需非空字符串",
            },
        ) from e
    reload_kb()
    write_audit(
        "kb_add",
        ok=True,
        detail={"section": item.section, "key": item.key, "value": item.value},
    )
    return {"ok": True, "kb": get_kb().to_dict(), "message": "别名已添加"}


@app.delete("/api/kb/{section}/{key}")
def kb_delete(section: str, key: str) -> dict[str, Any]:
    if section not in _KB_SECTIONS:
        raise HTTPException(
            400,
            detail={
                "error": "unknown_section",
                "message": f"未知 section: {section}",
                "hint": f"section 仅支持 {sorted(_KB_SECTIONS)}",
            },
        )
    ok = get_kb().remove_alias(section, key)
    if not ok:
        write_audit(
            "kb_delete",
            ok=False,
            detail={"section": section, "key": key, "error": "not_found"},
        )
        raise HTTPException(
            404,
            detail={
                "error": "kb_key_not_found",
                "message": f"未找到 {section}/{key}",
                "hint": "先 GET /api/kb 查看现有别名",
            },
        )
    reload_kb()
    write_audit("kb_delete", ok=True, detail={"section": section, "key": key})
    return {"ok": True, "kb": get_kb().to_dict(), "message": "别名已删除"}


@app.post("/api/kb/reload")
def kb_reload() -> dict[str, Any]:
    kb = reload_kb()
    return {"ok": True, "kb": kb.to_dict()}


def create_s01_test_app() -> FastAPI:
    """Build explicit S01 test wiring without changing the trusted default app."""
    global S01_BACKGROUND_ENABLED, S01_REQUIRE_CONFIGURED_STARTUP
    global S01_SERVICE, S01_TEST_DRIVER

    fixture_value = os.environ.get("TASK4_S01_TEST_FIXTURE_ROOT", "").strip()
    rules_value = os.environ.get("TASK4_S01_TEST_RULES_PATH", "").strip()
    state_value = os.environ.get("TASK4_S01_TEST_STATE_PATH", "").strip()
    scenario_value = os.environ.get("TASK4_S01_TEST_SCENARIO_ID", "").strip()
    S01_BACKGROUND_ENABLED = _s01_demo_flag(
        "TASK4_S01_TEST_BACKGROUND_ENABLED", default=True
    )
    S01_REQUIRE_CONFIGURED_STARTUP = False
    try:
        S01_SERVICE = ControlledScenarioService(
            fixture_root=Path(fixture_value).resolve() if fixture_value else FIXTURES,
            rules_path=Path(rules_value).resolve() if rules_value else DEFAULT_RULES,
            state_path=(
                Path(state_value).resolve()
                if state_value
                else Path(tempfile.mkdtemp(prefix="xiaopeng-s01-test-"))
                / "target.sqlite3"
            ),
            audit_available=_s01_demo_flag(
                "TASK4_S01_TEST_AUDIT_AVAILABLE", default=True
            ),
            storage_available=_s01_demo_flag(
                "TASK4_S01_TEST_STORAGE_AVAILABLE", default=True
            ),
            worker_identity="s01-test-server-worker",
            clock=lambda: int(S01_SESSION_CLOCK()),
            registered_sources=S02_REGISTERED_SOURCES,
            controlled_objects=S02_CONTROLLED_OBJECTS,
            scenario_id=scenario_value or "app_r53_bad_engine.json",
            exception_approver_subject=S05_EXCEPTION_APPROVER_SUBJECT,
        )
        S01_TEST_DRIVER = ControlledScenarioTestDriver(S01_SERVICE)
    except Exception:
        S01_SERVICE = None
        S01_TEST_DRIVER = None
    return app


def create_s02_test_app() -> FastAPI:
    """Build explicit S02 test wiring without weakening production startup."""
    global S01_BACKGROUND_ENABLED, S01_REQUIRE_CONFIGURED_STARTUP
    global S01_SERVICE, S01_TEST_DRIVER
    global S02_REGISTERED_SOURCES, S02_CONTROLLED_OBJECTS
    global S02_CONFIGURATION_ERROR, S02_CONFIGURED
    global S02_CREDENTIAL, S02_SUBJECT, S02_TENANT_ID, S02_SOURCE_SYSTEM_ID

    S02_CONFIGURATION_ERROR = None
    try:
        S02_REGISTERED_SOURCES, S02_CONTROLLED_OBJECTS = (
            _s02_registry_from_environment(test=True)
        )
    except Exception:
        S02_REGISTERED_SOURCES, S02_CONTROLLED_OBJECTS = (), ()
        S02_CONFIGURATION_ERROR = "S02 source registry configuration is invalid"

    S02_CREDENTIAL = os.environ.get("TASK4_S02_CREDENTIAL", "").strip()
    S02_SUBJECT = os.environ.get("TASK4_S02_SUBJECT", "").strip()
    S02_TENANT_ID = os.environ.get("TASK4_S02_TENANT_ID", "").strip()
    S02_SOURCE_SYSTEM_ID = os.environ.get("TASK4_S02_SOURCE_SYSTEM_ID", "").strip()
    S02_CONFIGURED = bool(
        S02_CONFIGURATION_ERROR is None
        and S02_REGISTERED_SOURCES
        and S02_CREDENTIAL
        and S02_SUBJECT
        and S02_TENANT_ID
        and S02_SOURCE_SYSTEM_ID
        and any(
            source.tenant_id == S02_TENANT_ID
            and source.source_system_id == S02_SOURCE_SYSTEM_ID
            for source in S02_REGISTERED_SOURCES
        )
    )

    state_value = os.environ.get("TASK4_S02_TEST_STATE_PATH", "").strip()
    scenario_value = os.environ.get("TASK4_S02_TEST_SCENARIO_ID", "").strip()
    state_path = (
        Path(state_value)
        if state_value and Path(state_value).is_absolute()
        else Path(tempfile.mkdtemp(prefix="xiaopeng-s02-test-")) / "target.sqlite3"
    )
    S01_BACKGROUND_ENABLED = _s01_demo_flag(
        "TASK4_S02_TEST_BACKGROUND_ENABLED", default=True
    )
    S01_REQUIRE_CONFIGURED_STARTUP = False
    s03_fault_point = os.environ.get("TASK4_S03_TEST_FAULT_POINT", "").strip()
    if s03_fault_point not in {"review.audit", "review.source_read"}:
        s03_fault_point = ""

    def inject_s03_test_fault(write_point: str) -> None:
        if write_point == s03_fault_point:
            raise OSError("injected S03 test fault")

    try:
        S01_SERVICE = ControlledScenarioService(
            fixture_root=FIXTURES,
            rules_path=DEFAULT_RULES,
            state_path=state_path,
            worker_identity="s02-test-server-worker",
            clock=lambda: int(S01_SESSION_CLOCK()),
            fault_injector=inject_s03_test_fault if s03_fault_point else None,
            registered_sources=S02_REGISTERED_SOURCES,
            controlled_objects=S02_CONTROLLED_OBJECTS,
            scenario_id=scenario_value or "app_r53_bad_engine.json",
        )
        S01_TEST_DRIVER = None
    except Exception:
        S01_SERVICE = None
        S01_TEST_DRIVER = None
        S02_CONFIGURED = False
    return app


def create_app() -> FastAPI:
    return app
