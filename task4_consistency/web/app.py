"""FastAPI demo: check applications, validate rule packages, maintain entity KB reads."""

from __future__ import annotations

import email.policy
import hmac
import json
import os
import re
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from typing import Annotated, Any, Callable, Literal
from urllib.parse import urlparse

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_serializer,
)
from starlette.middleware.base import BaseHTTPMiddleware

from task4_consistency.audit import audit_log_path, audit_status, read_audit_tail, write_audit
from task4_consistency.controlled.s01 import (
    ControlledScenarioService,
    ControlledScenarioTestDriver,
    QueryNotFound,
    S01CommandPrincipal,
    _ApplicationStateAuthorityUnavailable,
)
from task4_consistency.controlled.s08 import PolicyGovernanceService
from task4_consistency.controlled.s12 import EvaluationService
from task4_consistency.controlled.s13 import (
    DownstreamRecipientRegistration,
    build_c_demo_registry,
)
from task4_consistency.controlled.s02 import (
    ControlledObject,
    RegisteredSource,
    load_runtime_registry,
)
from task4_consistency.kb.store import get_kb

from task4_consistency.models import Application
from task4_consistency.report import report_to_html
from task4_consistency.rules.critical_guard import (
    CriticalGuardError,
    enforce_critical_fingerprints,
    fingerprints_as_dicts,
)
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules

from task4_consistency.web.observation import (
    ObservationMiddleware,
    app_family_table,
    recorder_from_env,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES = ROOT / "configs" / "rules_auto_lease.yaml"
RUNTIME_RULES = ROOT / "configs" / "runtime_rules.yaml"
FIXTURES = ROOT / "fixtures" / "applications"
STATIC = Path(__file__).resolve().parent / "static"
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
S08_ADMIN_CREDENTIAL = os.environ.get("TASK4_S08_ADMIN_CREDENTIAL", "").strip()
S08_ADMIN_SUBJECT = os.environ.get("TASK4_S08_ADMIN_SUBJECT", "").strip()
S08_APPROVER_CREDENTIAL = os.environ.get("TASK4_S08_APPROVER_CREDENTIAL", "").strip()
S08_APPROVER_SUBJECT = os.environ.get("TASK4_S08_APPROVER_SUBJECT", "").strip()
S08_OPERATOR_CREDENTIAL = os.environ.get("TASK4_S08_OPERATOR_CREDENTIAL", "").strip()
S08_OPERATOR_SUBJECT = os.environ.get("TASK4_S08_OPERATOR_SUBJECT", "").strip()
# S09 least-privilege diagnostic identities: one separate credential and
# subject per namespace (reproduction replay vs counterfactual simulation),
# never shared with the activation operator.
S09_REPLAY_CREDENTIAL = os.environ.get("TASK4_S09_REPLAY_CREDENTIAL", "").strip()
S09_REPLAY_SUBJECT = os.environ.get("TASK4_S09_REPLAY_SUBJECT", "").strip()
S09_SIMULATION_CREDENTIAL = os.environ.get(
    "TASK4_S09_SIMULATION_CREDENTIAL", ""
).strip()
S09_SIMULATION_SUBJECT = os.environ.get(
    "TASK4_S09_SIMULATION_SUBJECT", ""
).strip()
S08_MIGRATION_ADMIN_SUBJECT = (
    os.environ.get("TASK4_S08_MIGRATION_ADMIN_SUBJECT", "").strip()
    or "c-demo-migration-admin"
)
# The Auditor identity is loaded beside the governance identities so every
# T09 controlled role can be checked for mutual uniqueness at configuration
# time (P-5): an Auditor aliasing any other controlled credential or subject
# disables the governed scope before authorization.
S01_AUDITOR_CREDENTIAL = os.environ.get(
    "TASK4_S01_AUDITOR_CREDENTIAL", ""
).strip()
S01_AUDITOR_SUBJECT = os.environ.get("TASK4_S01_AUDITOR_SUBJECT", "").strip()
S08_CONFIGURED = bool(
    S08_ADMIN_CREDENTIAL
    and S08_ADMIN_SUBJECT
    and S08_APPROVER_CREDENTIAL
    and S08_APPROVER_SUBJECT
    and S08_OPERATOR_CREDENTIAL
    and S08_OPERATOR_SUBJECT
    and len(
        {
            S08_ADMIN_CREDENTIAL,
            S08_APPROVER_CREDENTIAL,
            S08_OPERATOR_CREDENTIAL,
        }
    )
    == 3
    and len(
        {
            S08_ADMIN_SUBJECT,
            S08_APPROVER_SUBJECT,
            S08_OPERATOR_SUBJECT,
        }
    )
    == 3
)

def _s09_identities_configuration_valid() -> bool:
    """The single six-identity T09 configuration gate (F-SPEC-1).

    Every credential and subject must be present, and all six controlled
    identities (admin, approver, activation operator, the Auditor, replay
    operator, simulation operator) must be mutually unique in both fields.
    Missing or aliased configuration disables the whole governed T09 scope
    (workspace, React shell, mutation and diagnostic commands) before
    authorization; it never changes S08 command authorization, which is
    gated by ``S08_CONFIGURED`` alone."""
    credentials = (
        S08_ADMIN_CREDENTIAL,
        S08_APPROVER_CREDENTIAL,
        S08_OPERATOR_CREDENTIAL,
        S01_AUDITOR_CREDENTIAL,
        S09_REPLAY_CREDENTIAL,
        S09_SIMULATION_CREDENTIAL,
    )
    subjects = (
        S08_ADMIN_SUBJECT,
        S08_APPROVER_SUBJECT,
        S08_OPERATOR_SUBJECT,
        S01_AUDITOR_SUBJECT,
        S09_REPLAY_SUBJECT,
        S09_SIMULATION_SUBJECT,
    )
    return bool(
        all(credentials)
        and all(subjects)
        and len(set(credentials)) == 6
        and len(set(subjects)) == 6
    )


# The T09 governance scope gate: all six T09 identities (admin, approver,
# operator, auditor, replay operator, simulation operator) must each carry
# their own credential and subject.  Any alias or missing identity disables
# the whole governed scope before authorization; S08 command authorization
# is unaffected and gated by ``S08_CONFIGURED`` alone.
S09_GOVERNANCE_CONFIGURED = bool(
    S08_CONFIGURED and _s09_identities_configuration_valid()
)


def _s09_diagnostic_configuration_valid() -> bool:
    """The S09 configuration gate for replay/simulation identities: the
    single six-identity predicate shared with the governance scope."""
    return _s09_identities_configuration_valid()


S08_SERVICE: PolicyGovernanceService | None = None
S08_DEFAULT_KB_PATH = ROOT / "configs" / "kb" / "entity_kb.json"


def _s09_lifecycle_impact_snapshot(
    owner: Any, final_impact_digest: str | None = None
) -> dict[str, Any]:
    """The S09 cross-owner read seam: Governance asks the Lifecycle to
    build the read-only impact snapshot over the same physical store
    snapshot Governance already reloaded.  Resolves the live S01 service at
    call time because the Governance service is constructed first."""
    if S01_SERVICE is None:
        raise RuntimeError("S01 lifecycle authority is not configured")
    return S01_SERVICE.build_policy_impact_snapshot(owner, final_impact_digest)


def _s09_lifecycle_diagnostic_snapshot(
    owner: Any, application_id: str
) -> dict[str, Any]:
    if S01_SERVICE is None:
        raise RuntimeError("S01 lifecycle authority is not configured")
    return S01_SERVICE.build_policy_diagnostic_snapshot(owner, application_id)


def _s08_policy_service(
    *,
    state_path: Path,
    rules_path: Path,
    audit_available: bool,
    storage_available: bool,
    clock: Callable[[], int],
    corpus_root: Path | None = None,
    fault_injector: Callable[[str], None] | None = None,
) -> PolicyGovernanceService | None:
    """S08 is gated on complete independent identities: without every
    Admin/Approver/Operator credential and subject the scope stays closed
    and no default shared subject or automatic bootstrap may run."""
    if not S08_CONFIGURED:
        return None
    try:
        return PolicyGovernanceService(
            state_path=state_path,
            audit_available=audit_available,
            storage_available=storage_available,
            migration_admin_subject=S08_MIGRATION_ADMIN_SUBJECT,
            admin_subject=S08_ADMIN_SUBJECT,
            approver_subject=S08_APPROVER_SUBJECT,
            operator_subject=S08_OPERATOR_SUBJECT,
            source_rules_path=rules_path,
            source_kb_path=S08_DEFAULT_KB_PATH,
            corpus_root=corpus_root,
            clock=clock,
            fault_injector=fault_injector,
            lifecycle_snapshot_provider=_s09_lifecycle_impact_snapshot,
            diagnostic_snapshot_provider=_s09_lifecycle_diagnostic_snapshot,
        )
    except Exception:
        return None
try:
    _s01_state_value = os.environ.get("TASK4_S01_STATE_PATH", "").strip()
    if not _s01_state_value:
        raise ValueError("TASK4_S01_STATE_PATH is required")
    _s01_state_path = Path(_s01_state_value)
    if not _s01_state_path.is_absolute():
        raise ValueError("TASK4_S01_STATE_PATH must be absolute")
    S08_SERVICE = _s08_policy_service(
        state_path=_s01_state_path,
        rules_path=DEFAULT_RULES,
        audit_available=_s01_demo_flag("TASK4_S01_AUDIT_AVAILABLE", default=True),
        storage_available=_s01_demo_flag("TASK4_S01_STORAGE_AVAILABLE", default=True),
        clock=lambda: int(time.time()),
        corpus_root=FIXTURES,
    )
    S01_SERVICE: ControlledScenarioService | None = ControlledScenarioService(
        fixture_root=FIXTURES,
        rules_path=DEFAULT_RULES,
        state_path=_s01_state_path,
        audit_available=_s01_demo_flag("TASK4_S01_AUDIT_AVAILABLE", default=True),
        storage_available=_s01_demo_flag("TASK4_S01_STORAGE_AVAILABLE", default=True),
        registered_sources=S02_REGISTERED_SOURCES,
        controlled_objects=S02_CONTROLLED_OBJECTS,
        exception_approver_subject=S05_EXCEPTION_APPROVER_SUBJECT,
        policy_governance=S08_SERVICE,
    )
    if S08_SERVICE is not None:
        S08_SERVICE.bootstrap_once()
except Exception as error:
    S01_SERVICE = None
    S01_CONFIGURATION_ERROR = str(error)
S01_DEMO_CREDENTIAL = os.environ.get("TASK4_S01_DEMO_CREDENTIAL", "").strip()
S01_DEMO_SUBJECT = os.environ.get("TASK4_S01_DEMO_SUBJECT", "").strip()
S01_OPERATOR_CREDENTIAL = os.environ.get("TASK4_S01_OPERATOR_CREDENTIAL", "").strip()
S01_OPERATOR_SUBJECT = os.environ.get("TASK4_S01_OPERATOR_SUBJECT", "").strip()
S13_OPERATOR_CREDENTIAL = os.environ.get("TASK4_S13_OPERATOR_CREDENTIAL", "").strip()
S13_OPERATOR_SUBJECT = os.environ.get("TASK4_S13_OPERATOR_SUBJECT", "").strip()
S13_OPERATOR_SCOPE = os.environ.get("TASK4_S13_OPERATOR_SCOPE", "C-DEMO").strip()
S02_CREDENTIAL = os.environ.get("TASK4_S02_CREDENTIAL", "").strip()
S02_SUBJECT = os.environ.get("TASK4_S02_SUBJECT", "").strip()
# S12 isolated evaluation plane: gated on a separately configured evaluation
# SQLite state path plus a distinct operator identity.  Missing or invalid
# configuration keeps S01-S11 startup and routes available while every S12
# route reports scoped unavailability.
S12_CONFIGURATION_ERROR: str | None = None
S12_CREDENTIAL = os.environ.get("TASK4_S12_CREDENTIAL", "").strip()
S12_SUBJECT = os.environ.get("TASK4_S12_SUBJECT", "").strip()
S12_WORKER_SUBJECT = os.environ.get("TASK4_S12_WORKER_SUBJECT", "").strip()
S12_SERVICE: EvaluationService | None = None


def _s12_evaluation_service() -> EvaluationService | None:
    state_value = os.environ.get("TASK4_S12_STATE_PATH", "").strip()
    if not state_value or not S12_CREDENTIAL or not S12_SUBJECT:
        return None
    if not S12_WORKER_SUBJECT:
        # A distinct evaluator worker subject is required: without it the
        # evaluation plane must not silently alias the operator identity.
        return None
    state_path = Path(state_value)
    if not state_path.is_absolute():
        raise ValueError("TASK4_S12_STATE_PATH must be absolute")
    # P-5-style identity isolation: the S12 operator identity must not alias
    # any other controlled identity, or the evaluation plane stays closed.
    controlled_credentials = {
        globals().get(name, "")
        for name in (
            "S01_DEMO_CREDENTIAL",
            "S01_OPERATOR_CREDENTIAL",
            "S01_AUDITOR_CREDENTIAL",
            "S02_CREDENTIAL",
            "S05_EXCEPTION_APPROVER_CREDENTIAL",
            "S08_ADMIN_CREDENTIAL",
            "S08_APPROVER_CREDENTIAL",
            "S08_OPERATOR_CREDENTIAL",
            "S09_REPLAY_CREDENTIAL",
            "S09_SIMULATION_CREDENTIAL",
        )
    }
    controlled_subjects = {
        globals().get(name, "")
        for name in (
            "S01_DEMO_SUBJECT",
            "S01_OPERATOR_SUBJECT",
            "S01_AUDITOR_SUBJECT",
            "S02_SUBJECT",
            "S05_EXCEPTION_APPROVER_SUBJECT",
            "S08_ADMIN_SUBJECT",
            "S08_APPROVER_SUBJECT",
            "S08_OPERATOR_SUBJECT",
            "S09_REPLAY_SUBJECT",
            "S09_SIMULATION_SUBJECT",
        )
    }
    if S12_CREDENTIAL in controlled_credentials or S12_SUBJECT in controlled_subjects:
        raise ValueError("TASK4_S12 identity aliases a controlled identity")
    if (
        S12_WORKER_SUBJECT == S12_SUBJECT
        or S12_WORKER_SUBJECT in controlled_subjects
    ):
        raise ValueError(
            "TASK4_S12_WORKER_SUBJECT aliases the operator or a controlled "
            "subject; the evaluator worker must be distinct"
        )
    label_root_value = os.environ.get("TASK4_S12_LABEL_MANIFESTS_DIR", "").strip()
    if not label_root_value:
        raise ValueError("TASK4_S12_LABEL_MANIFESTS_DIR is required")
    from task4_consistency.controlled.s12 import LabelManifestStore

    def snapshot_provider(application_id: str, snapshot_id: str) -> dict[str, Any]:
        if S01_SERVICE is None:
            raise RuntimeError("S01 authority is not configured")
        return S01_SERVICE.evaluation_evidence_snapshot(
            application_id=application_id, snapshot_id=snapshot_id
        )

    def release_provider(release_id: str, release_digest: str) -> dict[str, Any]:
        if S08_SERVICE is None:
            raise RuntimeError("S08 authority is not configured")
        return S08_SERVICE.resolve_evaluation_release(
            release_id=release_id, release_digest=release_digest
        )

    def business_state_provider() -> dict[str, Any]:
        facts: dict[str, Any] = {}
        if S01_SERVICE is not None:
            facts.update(S01_SERVICE.evaluation_business_measurement())
        if S08_SERVICE is not None:
            facts.update(S08_SERVICE.evaluation_governance_measurement())
        return facts

    @contextmanager
    def business_publication_guard(revisions: dict[str, int]):
        if S01_SERVICE is None or S08_SERVICE is None:
            raise RuntimeError("S01/S08 authority is not configured")
        s01_revision = revisions["s01_authority_revision"]
        if s01_revision != revisions["s08_authority_revision"]:
            raise RuntimeError("S01/S08 shared authority revisions disagree")
        with S01_SERVICE.evaluation_publication_fence(s01_revision):
            yield

    return EvaluationService(
        state_path=state_path,
        clock=lambda: int(time.time()),
        snapshot_provider=snapshot_provider,
        release_provider=release_provider,
        label_manifest_provider=LabelManifestStore(label_root_value).resolve,
        business_state_provider=business_state_provider,
        business_publication_guard=business_publication_guard,
        worker_subject=S12_WORKER_SUBJECT,
    )


try:
    S12_SERVICE = _s12_evaluation_service()
except Exception as error:
    S12_SERVICE = None
    S12_CONFIGURATION_ERROR = str(error)
S01_TEST_DRIVER: ControlledScenarioTestDriver | None = None
S01_BACKGROUND_ENABLED = _s01_demo_flag(
    "TASK4_S01_BACKGROUND_ENABLED", default=True
)
S01_REQUIRE_CONFIGURED_STARTUP = True
S01_SESSION_COOKIE = "s01_session"
S01_SESSION_TTL_SECONDS = 15 * 60
S01_SESSION_CLOCK: Callable[[], float] = time.time
# ``S01_AUDITOR_CREDENTIAL`` / ``S01_AUDITOR_SUBJECT`` are loaded beside the
# governance identities above (P-5) so the T09 scope gate can verify the
# Auditor against every other controlled identity at configuration time.
S02_SESSION_COOKIE = "s02_session"
S02_SESSION_TTL_SECONDS = 15 * 60
S02_MAX_COMMAND_BYTES = 256 * 1024
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
                policy_process = getattr(
                    self._service, "process_next_policy_job", None
                )
                if policy_process is not None:
                    policy_process()
                impact_process = getattr(
                    self._service, "process_next_policy_impact", None
                )
                if impact_process is not None:
                    impact_process()
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
    application.openapi()
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
    if request.url.path == "/api/demo/check/batch":
        # T07 closed contract: every invalid batch request shape fails with
        # the exact fixed envelope registered as DemoErrorResponse, with no
        # caller-controlled reflection.
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "error": "DEMO_BATCH_INVALID",
                    "message": "批量校验请求无效",
                }
            },
        )
    if request.url.path.startswith("/controlled/s12"):
        if S12_SERVICE is None:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": {
                        "error": "S12_UNAVAILABLE",
                        "message": "Controlled S12 evaluation plane is unavailable",
                    }
                },
            )
        if not S12_SUBJECT or not _s01_has_credential(request, S12_CREDENTIAL):
            return JSONResponse(
                status_code=403,
                content={
                    "detail": {
                        "error": "S12_FORBIDDEN",
                        "message": "Registered S12 operator identity required",
                    }
                },
            )
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "error": "S12_INVALID_COMMAND",
                    "message": "S12 command does not match the registered contract",
                }
            },
        )
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
    _OWN_AUTH_PREFIXES = (
        "/controlled/s01",
        "/controlled/s02",
        "/controlled/s05",
        "/controlled/s08",
        "/controlled/s09",
        "/controlled/s12",
        "/controlled/s13",
    )

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
        if not path.startswith(
            ("/controlled/s01", "/controlled/s02", "/controlled/s05")
        ):
            return await call_next(request)
        if path.startswith("/controlled/s05"):
            slice_id = "S05"
        elif path.startswith("/controlled/s02"):
            slice_id = "S02"
        else:
            slice_id = "S01"
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


@app.exception_handler(RequestValidationError)
async def _s14_or_default_validation_handler(
    request: Request, exc: RequestValidationError
) -> Response:
    path = request.url.path
    if path.startswith(
        (
            "/controlled/s01/api/commands/applications/",
            "/controlled/s01/api/commands/process-termination-notification",
        )
    ):
        message = "S14 command failed validation"
        first = exc.errors()[0] if exc.errors() else {}
        loc = first.get("loc") or []
        if loc:
            message = f"{message}: {'.'.join(str(part) for part in loc)}"
        return JSONResponse(
            status_code=422,
            content={
                "status": "rejected",
                "replayed": False,
                "reason_code": "S14_COMMAND_INVALID",
                "reason": message,
            },
        )
    # Legacy controlled-slice contract: the validation body carries only
    # loc/msg/type per item.
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {
                    "loc": item.get("loc"),
                    "msg": item.get("msg"),
                    "type": item.get("type"),
                }
                for item in exc.errors()
            ]
        },
    )


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


def _run_check(application: Application, rules_path: Path):
    """The one shared Application -> RuleEngine -> Report path; the T06 demo
    facade reuses the exact legacy /api/check execution."""
    return RuleEngine(load_rules(rules_path)).run(application)


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
            "hint": "POST /api/rules/validate body: {\"yaml_text\": \"...\"}",
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


class S14CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_lifecycle_revision: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=200, strict=True)
    reason_code: str = Field(min_length=1, max_length=200, strict=True)


class S14SettleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_lifecycle_revision: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=200, strict=True)


class S14ReopenPolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    permission_id: str = Field(
        min_length=1, max_length=200, pattern=r"^\S+$", strict=True
    )
    release_digest: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        strict=True,
    )


class S14ReopenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_lifecycle_revision: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=200, strict=True)
    target_phase: str = Field(pattern=r"^(Intake|Assembly)$", strict=True)
    reopen_policy: S14ReopenPolicyBody


class S14GrantPermissionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_lifecycle_revision: int = Field(ge=1, strict=True)
    approver_subject: str = Field(
        min_length=1, max_length=200, pattern=r"^\S+$", strict=True
    )
    permission_id: str = Field(
        min_length=1, max_length=200, pattern=r"^\S+$", strict=True
    )
    idempotency_key: str = Field(min_length=1, max_length=200, strict=True)
    ttl_seconds: int = Field(default=3600, ge=1, le=86400, strict=True)


class S14EffectItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    id: str
    detail: str | None = None
    result: str | None = None
    settled: bool | None = None


S14Status = Literal[
    "accepted",
    "replayed",
    "rejected",
    "unavailable",
    "stale",
    "outstanding",
    "terminated",
    "idle",
    "blocked",
    "claimed",
    "delivered",
    "unknown",
    "retry_scheduled",
    "compensated",
    "failed",
]


class S14CommandResult(BaseModel):
    """Typed S14 command contract: every domain outcome — accepted,
    replayed, terminated, outstanding, stale, rejected, unavailable — is a
    serializable body with stable reason codes (ADR-0008 command
    interfaces)."""

    model_config = ConfigDict(extra="forbid")

    status: S14Status
    replayed: bool = False
    track: str | None = None
    application_id: str | None = None
    reason_code: str | None = None
    cycle: int | None = None
    phase: str | None = None
    lifecycle_revision: int | None = None
    predecessor_cycle: int | None = None
    target_phase: str | None = None
    cancel_reason_code: str | None = None
    cancelled_by: str | None = None
    fenced_effects: dict[str, int] | None = None
    settled_effects: list[S14EffectItem] | None = None
    unresolved_effects: list[S14EffectItem] | None = None
    permission_id: str | None = None
    approved_by: str | None = None
    scope: str | None = None
    policy_release_id: str | None = None
    policy_release_digest: str | None = None
    source_binding: str | None = None
    granted_via_source: str | None = None
    expires_at: int | None = None
    operation_id: str | None = None
    duplicate: bool | None = None
    result: str | None = None
    reason: str | None = None


@app.exception_handler(HTTPException)
async def _s14_http_exception_handler(
    request: Request, exc: HTTPException
) -> Response:
    if request.url.path.startswith(
        (
            "/controlled/s01/api/commands/applications/",
            "/controlled/s01/api/commands/process-termination-notification",
        )
    ):
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        error = str(detail.get("error") or "S14_COMMAND_INVALID")
        message = detail.get("message")
        body = S14CommandResult.model_validate(
            {
                "status": "unavailable" if exc.status_code == 503 else "rejected",
                "replayed": False,
                "reason_code": (
                    "S14_FORBIDDEN" if exc.status_code == 403 else error
                ),
                "reason": str(message) if message is not None else None,
            }
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=jsonable_encoder(body),
            headers=exc.headers,
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )


def _s14_command_response(result: dict[str, Any]) -> Response:
    """Map the domain outcome vocabulary to stable HTTP codes."""
    validated = S14CommandResult.model_validate(result)
    status = validated.status
    code = 200
    if status == "outstanding":
        code = 202
    elif status == "stale":
        code = 409
    elif status == "unavailable":
        code = 503
    elif status == "rejected":
        reason = str(validated.reason_code or "")
        if (
            reason.startswith("lifecycle.reopen_")
            or reason == "lifecycle.cancel_forbidden"
            or reason == "FORBIDDEN"
        ):
            body = validated.model_copy(
                update={"reason_code": "S14_FORBIDDEN", "reason": reason}
            )
            return JSONResponse(status_code=403, content=jsonable_encoder(body))
        code = 409
    return JSONResponse(status_code=code, content=jsonable_encoder(validated))


S14_COMMAND_RESPONSES = {
    202: {"model": S14CommandResult},
    403: {"model": S14CommandResult},
    404: {"model": S14CommandResult},
    409: {"model": S14CommandResult},
    422: {"model": S14CommandResult},
    503: {"model": S14CommandResult},
}




class T05ErrorDetail(BaseModel):
    """The closed S05 error detail: the registered error code plus the
    optional reason/message/hint, with no arbitrary keys."""

    model_config = ConfigDict(extra="forbid")

    error: str
    reason_code: str | None = None
    message: str | None = None
    hint: str | None = None


class T05ErrorResponse(BaseModel):
    """The closed S05 error envelope registered on every S05 response."""

    model_config = ConfigDict(extra="forbid")

    detail: T05ErrorDetail


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


class S01AutomaticFinding(BaseModel):
    finding_id: str
    rule_id: str
    verdict: str
    severity: str
    reason_code: str
    membership: S01WorkspaceMembership | None = None


class S01RunAuthority(BaseModel):
    run_id: str
    status: str
    authority_digest: str


class S01ReviewCommandContext(BaseModel):
    """The closed review command context.  The domain compares it by exact
    equality (``_review_context_matches``), so a partial context is a semantic
    staleness; the schema here closes the shape so no arbitrary keys can hide
    a missing revision inside a migrated request or response."""

    model_config = ConfigDict(extra="forbid")

    lifecycle_revision: int
    evidence_revision: int
    run_id: str
    projection_watermark: int
    current_context: str


class S01FindingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str
    outcome: str


class S01ManualVerification(BaseModel):
    """The structured manual verification the Reviewer submits.  The optional
    ``note`` is allowed by the domain contract; this ticket adds no note UI."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    outcome: str
    reason_code: str
    finding_decisions: list[S01FindingDecision]
    note: str | None = None


class S01HumanDecisionCompatibilityTargetContext(BaseModel):
    run_id: str
    evidence_snapshot_id: str
    release_id: str
    source_sha256: str


class S01HumanDecisionCompatibilityFactCounts(BaseModel):
    legacy_checks: int
    target_findings: int
    checks_compared: int
    mismatches: int


class S01HumanDecisionCompatibility(BaseModel):
    schema_version: str
    differential_source: str
    intent: str
    target_reason_code: str
    conformance: str
    target_context: S01HumanDecisionCompatibilityTargetContext
    fact_counts: S01HumanDecisionCompatibilityFactCounts
    semantic_differential_digest: str


class S01NoteMetadata(BaseModel):
    present: bool
    character_count: int
    byte_count: int
    sha256: str


class S01HumanDecision(BaseModel):
    """The exposed human decision record (``review_work_item_view``).  The two
    legacy-oracle fields are serialized only when the owning record carries
    them, so the migrated C-DEMO payload keeps its exact shape."""

    model_config = ConfigDict(extra="forbid")

    decision_id: str
    schema_version: str
    outcome: str
    reason_code: str
    finding_decisions: list[S01FindingDecision]
    reviewer_subject: str
    reviewer_role: str
    reviewer_source_id: str
    assigned_subject: str
    cycle: int
    finding_ids: list[str]
    evidence_snapshot_id: str
    release_id: str
    fixed_context: S01ReviewCommandContext
    claim_fence: int
    submitted_at: int
    compatibility: S01HumanDecisionCompatibility | None = None
    note_metadata: S01NoteMetadata | None = None

    @model_serializer(mode="wrap")
    def _serialize_dropping_absent_legacy(self, handler, info):
        dumped = handler(self)
        return {
            key: value
            for key, value in dumped.items()
            if key not in {"compatibility", "note_metadata"} or value is not None
        }


class S01ReviewWorkItemResponse(BaseModel):
    status: str
    application_id: str
    work_item_id: str
    claim_subject: str | None = None
    claim_fence: int
    claim_expires_at: int
    phase: str
    route: str
    lifecycle_revision: int
    evidence_revision: int
    command_context: S01ReviewCommandContext
    automatic_findings: list[S01AutomaticFinding]
    run_authority: S01RunAuthority
    decision: S01HumanDecision | None = None
    decisions: list[S01HumanDecision]
    completed_finding_ids: list[str]


class S01EvidenceLink(BaseModel):
    document_id: str
    document_role: str
    field: str
    value_state: str
    raw_masked: str | None = None
    observation_id: str
    source_sha256: str | None = None
    provenance_manifest_digest: str | None = None
    evidence_eligible: bool
    eligibility_reason: str | None = None
    source_page: int | None = None
    source_region: str | None = None
    producer_id: str | None = None
    producer_family: str | None = None
    producer_run_id: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    source_receipt_id: str | None = None


class S01MembershipProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter_id: str
    adapter_version: str
    source_pointer: str
    source_filename: str | None = None
    fact: str | None = None
    page_type: str | None = None
    inferred: bool | None = None


class S01MembershipCandidateDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_instance_id: str
    document_role: str


class S01MembershipSourceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=200, strict=True)
    evidence_revision: int = Field(ge=1, strict=True)


class S01MembershipDecisionSourceEvidence(S01MembershipSourceEvidence):
    candidate_claim_id: str = Field(min_length=1, max_length=200, strict=True)


class S01WorkspaceMembershipCandidate(BaseModel):
    """One immutable coexisting page-membership candidate claim (S10).  No
    candidate is selected by the authority; the Reviewer decides explicitly."""

    model_config = ConfigDict(extra="forbid")

    document_instance_id: str
    document_role: str
    claim_id: str
    provenance: S01MembershipProvenance


class S01WorkspaceMembership(BaseModel):
    """The membership blocker projection (S10): the page identity, its current
    effective decision state and every coexisting candidate claim/provenance."""

    attachment_id: str
    page_source_sha256: str
    page_ordinal: int
    state: Literal["unresolved", "ambiguous"]
    candidates: list[S01WorkspaceMembershipCandidate]
    active_decision_ids: list[str] = Field(default_factory=list)
    source_evidence: S01MembershipSourceEvidence
    unassigned: bool = False


class S01EntityLinkMention(BaseModel):
    """The application-local entity mention (S11): which document field the
    mention was read from and the raw value that needs a link decision."""

    model_config = ConfigDict(extra="forbid")

    mention_id: str
    entity_type: str
    document_id: str
    document_role: str
    field: str
    raw: str


class S01EntityLinkCandidateEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str
    entity_type: str
    label: str


class S01EntityLinkProvenance(BaseModel):
    """The matcher and frozen knowledge-release provenance of one candidate
    claim (S11).  The command must reproduce it exactly; an expired or wrong
    release metadata pair is rejected with no successor, and the digests must
    equal the fixed RunSpec entity-link release pin (SP-1)."""

    model_config = ConfigDict(extra="forbid")

    matcher_id: str
    matcher_version: str
    knowledge_release_id: str
    matcher_digest: str | None = None
    knowledge_release_digest: str | None = None
    method: str | None = None
    source_pointer: str | None = None


class S01EntityLinkKnowledge(BaseModel):
    """The frozen knowledge-release facts carried by one candidate claim
    (S11): same-as projection and explicit not-same-as conflict targets."""

    model_config = ConfigDict(extra="forbid")

    same_as: list[str] = Field(default_factory=list)
    conflict_with: list[str] = Field(default_factory=list)


class S01WorkspaceEntityLinkCandidate(BaseModel):
    """One immutable coexisting entity-link candidate claim (S11).  No
    candidate is selected by the authority; the Reviewer decides explicitly."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    entity_id: str
    entity_type: str
    label: str
    confidence: float
    provenance: S01EntityLinkProvenance
    knowledge: S01EntityLinkKnowledge


class S01WorkspaceEntityLink(BaseModel):
    """The entity-link blocker projection (S11): the mention identity, its
    current effective decision state and every coexisting candidate claim
    with confidence, provenance and frozen knowledge facts."""

    mention_id: str
    mention: S01EntityLinkMention
    state: Literal["unresolved", "ambiguous", "conflict"]
    candidates: list[S01WorkspaceEntityLinkCandidate]
    active_decision_ids: list[str] = Field(default_factory=list)
    source_evidence: S01MembershipSourceEvidence
    low_confidence: bool = False


class S01WorkspaceFinding(BaseModel):
    finding_id: str
    run_id: str
    rule_id: str
    verdict: str
    severity: str
    reason_code: str
    mandatory: bool
    evidence_links: list[S01EvidenceLink]
    membership: S01WorkspaceMembership | None = None
    entity_link: S01WorkspaceEntityLink | None = None


class S01BusinessExceptionEligibility(BaseModel):
    """The server-owned closed request-eligibility projection.  All four keys
    are always serialized with explicit nulls so the client never invents a
    reason, an ineligible code, or a predecessor."""

    model_config = ConfigDict(extra="forbid")

    eligible: bool
    request_reason: str | None
    ineligible_reason_code: str | None
    predecessor_request_id: str | None

    @model_serializer(mode="wrap")
    def _always_emit_four_keys(self, handler, info):
        # ``exclude_none`` filtering happens inside ``handler(self)``, so the
        # dumped dict may already lack the null keys; the closed shape reads
        # the model fields directly and re-emits all four keys.
        handler(self)
        return {
            "eligible": self.eligible,
            "request_reason": self.request_reason,
            "ineligible_reason_code": self.ineligible_reason_code,
            "predecessor_request_id": self.predecessor_request_id,
        }


class S01WorkspaceMembershipDecision(BaseModel):
    record_kind: Literal["accepted", "unassigned"]
    decision_id: str
    document_instance_id: str | None = None
    document_role: str | None = None
    actor: str
    reason_code: str
    time: int
    cycle: int
    source_evidence: S01MembershipDecisionSourceEvidence
    supersedes: list[str]
    status: Literal["active", "superseded"]


class S01WorkspaceMembershipPage(BaseModel):
    attachment_id: str
    page_source_sha256: str
    page_ordinal: int
    state: Literal["unresolved", "ambiguous", "selected", "unassigned"]
    finding_id: str | None = None
    active_decision_ids: list[str] = Field(default_factory=list)
    source_evidence: S01MembershipSourceEvidence
    candidates: list[S01WorkspaceMembershipCandidate]
    decisions: list[S01WorkspaceMembershipDecision]


class S01WorkspaceEntityLinkDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_kind: Literal["accepted"]
    decision_id: str
    link_id: str | None = None
    finding_id: str | None = None
    candidate_entity: S01EntityLinkCandidateEntity
    relationship: str
    actor: str
    reason_code: str
    time: int
    cycle: int
    source_evidence: S01MembershipDecisionSourceEvidence
    supersedes: list[str]
    status: Literal["active", "superseded"]
    matcher_id: str | None = None
    matcher_version: str | None = None
    matcher_digest: str | None = None
    knowledge_release_id: str | None = None
    knowledge_release_digest: str | None = None
    release_id: str | None = None
    release_digest: str | None = None


class S01WorkspaceEntityLinkMentionPage(BaseModel):
    mention_id: str
    mention: S01EntityLinkMention
    state: Literal["unresolved", "ambiguous", "conflict", "selected"]
    finding_id: str | None = None
    active_decision_ids: list[str] = Field(default_factory=list)
    source_evidence: S01MembershipSourceEvidence
    candidates: list[S01WorkspaceEntityLinkCandidate]
    decisions: list[S01WorkspaceEntityLinkDecision]
    low_confidence: bool = False


class S01WorkspaceResponse(BaseModel):
    application_id: str
    work_item_id: str
    assigned_subject: str
    claim_fence: int
    claim_expires_at: int
    track: str
    phase: str
    route: str
    evidence_ready: bool
    lifecycle_revision: int
    evidence_revision: int
    current_run_id: str | None = None
    evidence_snapshot_id: str | None = None
    evidence_snapshot_digest: str | None = None
    projection_watermark: int
    mandatory_blockers: list[S01WorkspaceFinding]
    selected_finding: S01WorkspaceFinding | None = None
    membership_ledger: list[S01WorkspaceMembershipPage] = Field(default_factory=list)
    entity_link_ledger: list[S01WorkspaceEntityLinkMentionPage] = Field(
        default_factory=list
    )
    business_exception_eligibility: S01BusinessExceptionEligibility | None = None
    actions: list[str]


class S01HistoryReconciliation(BaseModel):
    status: str
    logical_operation_id: str
    result_id: str | None = None
    result_digest: str | None = None
    attempt: int | None = None
    max_attempts: int | None = None


class S01HistorySourceLocation(BaseModel):
    source_sha256: str
    source_page: int | None = None
    source_region: str | None = None


class S01HistoryCorrection(BaseModel):
    correction_id: str
    superseded_observation_id: str
    successor_observation_id: str
    document_id: str
    document_role: str
    field: str
    source_location: S01HistorySourceLocation
    reason_code: str
    actor: str
    recorded_at: int
    invalidated_decision_ids: list[str]
    invalidated_exception_ids: list[str]
    evidence_revision: int


class S01HistoryBusinessException(BaseModel):
    request_id: str
    run_id: str
    finding_id: str
    rule_id: str
    machine_verdict: str
    status: str
    current: bool
    request_reason: str
    scope: str
    requested_at: int
    expires_at: int | None = None
    decision_id: str | None = None
    decision: str | None = None
    routed: bool
    route: str | None = None
    completion_basis: str | None = None


class S01HistoryAttachmentVersion(BaseModel):
    attachment_id: str
    version: int
    document_id: str
    document_role: str
    supersedes_attachment_id: str | None = None
    page_ids: list[str]
    producer_result_id: str | None = None
    evidence_revision: int
    current: bool


class S01HistoryRunComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    id: str
    digest: str


class S01HistoryMembershipDecisionPin(BaseModel):
    decision_id: str
    candidate_claim_id: str
    attachment_id: str
    page_source_sha256: str
    page_ordinal: int
    decision: Literal["accept", "unassign"]
    evidence_revision: int
    document_instance_id: str | None = None
    document_role: str | None = None


class S01HistoryEntityLinkDecisionPin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str
    candidate_claim_id: str
    mention_id: str
    entity_id: str
    entity_type: str
    label: str
    relationship: str
    evidence_revision: int
    matcher_id: str | None = None
    matcher_version: str | None = None
    matcher_digest: str | None = None
    knowledge_release_id: str | None = None
    knowledge_release_digest: str | None = None
    release_id: str | None = None
    release_digest: str | None = None


class S01HistoryRun(BaseModel):
    run_id: str
    status: str
    authority_digest: str
    current: bool
    currentness_reason: str
    cycle: int
    lifecycle_revision: int
    evidence_revision: int
    evidence_snapshot_id: str | None = None
    evidence_snapshot_digest: str | None = None
    membership_decisions: list[S01HistoryMembershipDecisionPin] = Field(
        default_factory=list
    )
    entity_link_decisions: list[S01HistoryEntityLinkDecisionPin] = Field(
        default_factory=list
    )
    evidence_document_instance_ids: list[str] = Field(default_factory=list)
    release_id: str | None = None
    release_digest: str | None = None
    checker_build: str | None = None
    policy_scope: str | None = None
    activation_event_id: str | None = None
    active_generation: int | None = None
    candidate_id: str | None = None
    manifest_id: str | None = None
    manifest_digest: str | None = None
    validation_bundle_id: str | None = None
    validation_bundle_digest: str | None = None
    approval_binding_id: str | None = None
    approval_binding_digest: str | None = None
    components: list[S01HistoryRunComponent] = Field(default_factory=list)
    finding_ids: list[str]
    cas_mismatches: list[str]
    selected_observation_ids: list[str]
    decision_ids: list[str]
    exception_ids: list[str]
    applicable_decision_ids: list[str]
    applicable_exception_ids: list[str]
    invalidated_decision_ids: list[str]
    invalidated_exception_ids: list[str]
    reconciliation: S01HistoryReconciliation | None = None


class S01ApplicationHistoryResponse(BaseModel):
    schema_version: str
    application_id: str
    current_run_id: str | None = None
    runs: list[S01HistoryRun]
    corrections: list[S01HistoryCorrection]
    business_exceptions: list[S01HistoryBusinessException]
    attachment_versions: list[S01HistoryAttachmentVersion]
    memberships: list[S01HistoryMembership] = Field(default_factory=list)
    membership_history: list[S01HistoryMembershipCorrection] = Field(
        default_factory=list
    )
    entity_links: list[S01HistoryEntityLink] = Field(default_factory=list)
    entity_link_history: list[S01HistoryEntityLinkCorrection] = Field(
        default_factory=list
    )


class S01HistoryMembershipPage(BaseModel):
    attachment_id: str
    source_sha256: str
    page_ordinal: int


class S01HistoryMembership(BaseModel):
    """One append-only ledger record of the preserved page-membership history
    (S10): candidate claims and every accepted/unassigned decision with its
    explicit status."""

    record_kind: str
    page: S01HistoryMembershipPage
    decision_id: str | None = None
    membership_id: str | None = None
    actor: str | None = None
    reason_code: str | None = None
    time: int | None = None
    cycle: int | None = None
    source_evidence: S01MembershipDecisionSourceEvidence | None = None
    supersedes: list[str] = Field(default_factory=list)
    status: str | None = None
    document_instance_id: str | None = None
    document_role: str | None = None
    claim_id: str | None = None
    candidate_document: S01MembershipCandidateDocument | None = None
    provenance: S01MembershipProvenance | None = None


class S01HistoryMembershipCorrection(BaseModel):
    """One chronologically committed page-membership correction (S10)."""

    evidence_revision: int
    event_id: str
    correction_id: str
    decision_id: str
    candidate_claim_id: str
    attachment_id: str
    page_source_sha256: str
    page_ordinal: int
    source_evidence: S01MembershipSourceEvidence
    decision: str
    document_instance_id: str | None = None
    document_role: str | None = None
    reason_code: str
    actor: str
    recorded_at: int
    cycle: int
    supersedes: list[str] = Field(default_factory=list)


class S01HistoryEntityLink(BaseModel):
    """One append-only ledger record of the preserved entity-link history
    (S11): candidate claims and every accepted decision with its explicit
    status."""

    model_config = ConfigDict(extra="forbid")

    record_kind: str
    mention: S01EntityLinkMention
    decision_id: str | None = None
    link_id: str | None = None
    candidate_entity: S01EntityLinkCandidateEntity | None = None
    relationship: str | None = None
    actor: str | None = None
    reason_code: str | None = None
    time: int | None = None
    cycle: int | None = None
    source_evidence: S01MembershipDecisionSourceEvidence | None = None
    supersedes: list[str] = Field(default_factory=list)
    status: str | None = None
    claim_id: str | None = None
    confidence: float | None = None
    provenance: S01EntityLinkProvenance | None = None
    knowledge: S01EntityLinkKnowledge | None = None
    matcher_id: str | None = None
    matcher_version: str | None = None
    matcher_digest: str | None = None
    knowledge_release_id: str | None = None
    knowledge_release_digest: str | None = None
    release_id: str | None = None
    release_digest: str | None = None


class S01HistoryEntityLinkCorrection(BaseModel):
    """One chronologically committed entity-link correction (S11)."""

    model_config = ConfigDict(extra="forbid")

    evidence_revision: int
    event_id: str
    correction_id: str
    decision_id: str
    candidate_claim_id: str
    mention_id: str
    mention: S01EntityLinkMention
    entity_id: str
    entity_type: str
    label: str
    relationship: str
    source_evidence: S01MembershipSourceEvidence
    decision: str
    matcher_id: str
    matcher_version: str
    knowledge_release_id: str
    matcher_digest: str | None = None
    knowledge_release_digest: str | None = None
    release_id: str | None = None
    release_digest: str | None = None
    reason_code: str
    actor: str
    recorded_at: int
    cycle: int
    supersedes: list[str] = Field(default_factory=list)


class S09ImpactDispositionMember(BaseModel):
    """The minimized per-member impact consumption receipt: identity,
    partition, required/current disposition, target generation and the
    single Operational Re-evaluation job reference.  No raw field value,
    OCR text, attachment locator or free text is ever exposed."""

    model_config = ConfigDict(extra="forbid")

    application_id: str
    cycle: int
    partition: str
    required_disposition: str
    disposition: str
    target_generation: int
    reevaluation_job_id: str | None = None
    reevaluation_job_count: int = 0


class S09ImpactDispositionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    final_impact_digest: str
    member_count: int
    unconsumed_count: int
    members: list[S09ImpactDispositionMember]
    projection_watermark: int = 0


class S09ImpactDispositionsSummaryResponse(BaseModel):
    """The minimized Reviewer view: aggregate digest, counts and projection
    watermark only; per-member application/job receipts stay behind the
    authorized audit/reconciliation route."""

    model_config = ConfigDict(extra="forbid")

    final_impact_digest: str
    member_count: int
    unconsumed_count: int
    projection_watermark: int = 0


class S01ClaimResult(BaseModel):
    status: str
    application_id: str
    work_item_id: str
    claim_subject: str | None = None
    claim_fence: int
    claim_expires_at: int


class S01RenewResult(BaseModel):
    status: str
    application_id: str
    work_item_id: str
    claim_subject: str
    claim_fence: int
    claim_expires_at: int
    replayed: bool


class S01ReleaseResult(BaseModel):
    status: str
    application_id: str
    work_item_id: str
    claim_fence: int
    released_at: int
    replayed: bool


class S01SubmitResult(BaseModel):
    status: str
    replayed: bool
    application_id: str
    work_item_id: str
    decision_id: str
    claim_fence: int
    lifecycle_revision: int
    evidence_revision: int
    route: str


class S01ReviewClaimBody(BaseModel):
    """The migrated S01 claim body.  Unlike the shared S03 body (whose open
    ``expected_context`` is also inherited by the S04/S05 command families),
    this closes the migrated review command contract."""

    model_config = ConfigDict(extra="forbid")

    expected_context: S01ReviewCommandContext


class S01ReviewFencedBody(S01ReviewClaimBody):
    expected_fence: int = Field(ge=0, strict=True)
    idempotency_key: str


class S01ReviewSubmitBody(S01ReviewFencedBody):
    verification: S01ManualVerification


class S01FieldCorrectionSourceLocation(BaseModel):
    """The closed source location a field correction must prove.  The domain
    compares it exactly to the projected public observation, so the schema
    mirrors the registered contract instead of an open dictionary."""

    model_config = ConfigDict(extra="forbid")

    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    source_page: int = Field(ge=1, strict=True)
    source_region: str = Field(pattern=r"^region:[0-9]+$", strict=True)


class S01FieldObservationCorrection(BaseModel):
    """The closed source-backed correction payload.  The domain rejects any
    key set or schema version other than the registered contract; this schema
    closes the shape so generated clients can never invent a field the
    authority will reject."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["field-observation-correction/1"]
    finding_id: str
    observation_id: str
    document_id: str
    document_role: str
    field: str
    raw: str
    source_location: S01FieldCorrectionSourceLocation
    reason_code: Literal["SOURCE_VALUE_MISREAD", "SOURCE_VALUE_MISSING"]


class S01ReviewRevealBody(S01ReviewFencedBody):
    """The migrated S01 reveal command.  ``expected_fence`` is bounded at 1
    because the domain rejects an unclaimed (fence 0) reveal as invalid; the
    wire status for that violation is the same 422 the domain raises today."""

    expected_fence: int = Field(ge=1, strict=True)
    application_id: str = Field(min_length=1, max_length=200, strict=True)
    observation_id: str = Field(min_length=1, max_length=200, strict=True)


class S01ReviewCorrectionBody(S01ReviewFencedBody):
    """The migrated S01 correction command.  ``expected_fence`` is bounded at
    1 because the domain rejects a fence below 1 as invalid; the wire status
    is the same 422 the domain raises today."""

    expected_fence: int = Field(ge=1, strict=True)
    application_id: str = Field(min_length=1, max_length=200, strict=True)
    correction: S01FieldObservationCorrection


class S01RevealSourceLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_sha256: str
    source_page: int
    source_region: str


class S01RevealResult(BaseModel):
    """The authorized reveal response.  ``source_text`` is restricted data:
    it exists only in this command response and the exact live panel state
    authorized to display it.  A replay returns the same reveal for the same
    principal and command."""

    model_config = ConfigDict(extra="forbid")

    status: str
    replayed: bool
    application_id: str
    work_item_id: str
    observation_id: str
    source_location: S01RevealSourceLocation
    source_text: str
    revealed_at: int


class S01CorrectionResult(BaseModel):
    """Command acceptance of an evidence correction.  Acceptance is not proof
    that the asynchronous successor run is already current; the client must
    read current-route/history for convergence."""

    model_config = ConfigDict(extra="forbid")

    status: str
    replayed: bool
    application_id: str
    work_item_id: str
    correction_id: str
    observation_id: str
    invalidated_run_id: str
    job_id: str
    phase: str
    route: str
    lifecycle_revision: int
    evidence_revision: int
    invalidated_exception_ids: list[str] | None = None


class S01PageMembershipCorrectionBase(BaseModel):
    """Fields shared by both closed S10 membership decisions."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["page-membership-correction/2"]
    finding_id: str = Field(min_length=1, max_length=200, strict=True)
    candidate_claim_id: str = Field(min_length=1, max_length=200, strict=True)
    attachment_id: str = Field(min_length=1, max_length=200, strict=True)
    page_source_sha256: str = Field(
        min_length=64, max_length=64, pattern="^[0-9a-f]{64}$", strict=True
    )
    page_ordinal: int = Field(ge=1, strict=True)
    source_evidence: S01MembershipSourceEvidence
    expected_active_decision_ids: list[str]


class S01PageMembershipAccept(S01PageMembershipCorrectionBase):
    decision: Literal["accept"]
    document_instance_id: str = Field(min_length=1, max_length=200, strict=True)
    document_role: str = Field(min_length=1, max_length=200, strict=True)
    reason_code: Literal[
        "MEMBERSHIP_SOURCE_VERIFIED",
        "MEMBERSHIP_SOURCE_MISASSIGNED",
        "MEMBERSHIP_INSTANCE_WRONG",
    ]


class S01PageMembershipUnassign(S01PageMembershipCorrectionBase):
    decision: Literal["unassign"]
    reason_code: Literal[
        "MEMBERSHIP_SOURCE_VERIFIED",
        "MEMBERSHIP_SOURCE_MISASSIGNED",
        "MEMBERSHIP_PAGE_UNASSIGNED",
    ]


class S01ReviewMembershipBody(S01ReviewFencedBody):
    """The migrated S10 page-membership command.  ``expected_fence`` is bounded
    at 1 because the domain rejects a fence below 1 as invalid."""

    expected_fence: int = Field(ge=1, strict=True)
    application_id: str = Field(min_length=1, max_length=200, strict=True)
    membership: Annotated[
        S01PageMembershipAccept | S01PageMembershipUnassign,
        Field(discriminator="decision"),
    ]


class S01MembershipCorrectionResult(BaseModel):
    """Command acceptance of a page-membership correction.  Acceptance is not
    proof the successor run is already current; the client must read
    current-route/history for convergence."""

    model_config = ConfigDict(extra="forbid")

    status: str
    replayed: bool
    application_id: str
    work_item_id: str
    correction_id: str
    membership_decision_id: str
    candidate_claim_id: str
    attachment_id: str
    page_source_sha256: str
    page_ordinal: int
    decision: str
    document_instance_id: str | None = None
    document_role: str | None = None
    cycle: int
    invalidated_run_id: str
    job_id: str
    phase: str
    route: str
    lifecycle_revision: int
    evidence_revision: int


class S01EntityLinkCorrectionBase(BaseModel):
    """Fields shared by the closed S11 entity-link decision."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["entity-link-correction/1"]
    finding_id: str = Field(min_length=1, max_length=200, strict=True)
    candidate_claim_id: str = Field(min_length=1, max_length=200, strict=True)
    mention_id: str = Field(min_length=1, max_length=200, strict=True)
    source_evidence: S01MembershipSourceEvidence
    expected_active_decision_ids: list[str]


class S01EntityLinkAccept(S01EntityLinkCorrectionBase):
    decision: Literal["accept"]
    entity_id: str = Field(min_length=1, max_length=200, strict=True)
    entity_type: str = Field(min_length=1, max_length=200, strict=True)
    label: str = Field(min_length=1, max_length=200, strict=True)
    relationship: Literal["same_as"]
    matcher_id: str = Field(min_length=1, max_length=200, strict=True)
    matcher_version: str = Field(min_length=1, max_length=200, strict=True)
    knowledge_release_id: str = Field(min_length=1, max_length=200, strict=True)
    reason_code: Literal[
        "ENTITY_LINK_SOURCE_VERIFIED",
        "ENTITY_LINK_SOURCE_MISASSIGNED",
        "ENTITY_LINK_AMBIGUITY_RESOLVED",
    ]


class S01ReviewEntityLinkBody(S01ReviewFencedBody):
    """The S11 entity-link command.  ``expected_fence`` is bounded at 1
    because the domain rejects a fence below 1 as invalid."""

    expected_fence: int = Field(ge=1, strict=True)
    application_id: str = Field(min_length=1, max_length=200, strict=True)
    entity_link: Annotated[
        S01EntityLinkAccept,
        Field(discriminator="decision"),
    ]


class S01EntityLinkCorrectionResult(BaseModel):
    """Command acceptance of an entity-link correction.  Acceptance is not
    proof the successor run is already current; the client must read
    current-route/history for convergence."""

    model_config = ConfigDict(extra="forbid")

    status: str
    replayed: bool
    application_id: str
    work_item_id: str
    correction_id: str
    entity_link_decision_id: str
    candidate_claim_id: str
    mention_id: str
    entity_id: str
    entity_type: str
    label: str
    relationship: str
    cycle: int
    invalidated_run_id: str
    job_id: str
    phase: str
    route: str
    lifecycle_revision: int
    evidence_revision: int


class S02SubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str
    submission: dict[str, Any]


class S01SupplementRequestResult(BaseModel):
    """Closed 200 result of the supplement request command.  Only the
    ``accepted`` status reaches 200; every other domain status is mapped to
    the registered S03 HTTP error before serialization."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"]
    replayed: bool
    application_id: str
    request_id: str
    work_item_id: str
    finding_id: str
    material_requirement_id: str
    phase: str
    route: str
    due_at: int
    lifecycle_revision: int
    evidence_revision: int


class S01SupplementMaterialRequirement(BaseModel):
    """The closed material requirement of one supplement request."""

    model_config = ConfigDict(extra="forbid")

    material_requirement_id: str
    document_role: str
    material_kind: str
    operation: str
    required_fact_kinds: list[str]
    responsible_party: str
    allowed_tenant_id: str
    allowed_source_system_ids: list[str]
    allowed_workload_identity_ids: list[str]
    satisfaction_policy_id: str
    batch_item_count: int
    batch_closure_required: bool
    integrity_required: bool
    provenance_required: bool
    evidence_eligibility_required: bool


class S01SupplementRequestFailure(BaseModel):
    """The closed terminal failure of a supplement request."""

    model_config = ConfigDict(extra="forbid")

    reason_code: str
    responsible_party: str
    recovery_action: str
    recovery_target: dict[str, Any] | None = None


class S01SupplementRequestView(BaseModel):
    """Closed Reviewer request view; every current wire field is enumerated."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    request_id: str
    work_item_id: str
    source_work_item_id: str
    application_id: str
    cycle: int
    run_id: str
    finding_id: str
    rule_id: str
    finding_reason_code: str
    finding_verdict: str
    requester_claim_fence: int
    requested_at: int
    due_at: int
    fixed_context: dict[str, Any]
    context_digest: str
    expected_predecessor_attachment_id: str
    expected_predecessor_attachment_version: int
    satisfaction_policy_digest: str
    status: str
    current: bool
    phase: str
    route: str
    lifecycle_revision: int
    evidence_revision: int
    projection_watermark: int
    material_requirement: S01SupplementMaterialRequirement
    failure: S01SupplementRequestFailure | None = None


class S01IntegratorMaterialRequirement(BaseModel):
    """The minimized material requirement of the Integrator projection."""

    model_config = ConfigDict(extra="forbid")

    material_requirement_id: str
    document_role: str
    material_kind: str
    operation: str
    required_fact_kinds: list[str]
    responsible_party: str
    allowed_tenant_id: str
    allowed_source_system_ids: list[str]
    allowed_workload_identity_ids: list[str]
    batch_item_count: int
    batch_closure_required: bool
    integrity_required: bool
    provenance_required: bool
    evidence_eligibility_required: bool


class S01IntegratorBatchBinding(BaseModel):
    """The server-bound batch identity the next command must reuse after the
    first accepted progress item (null before the first submission)."""

    model_config = ConfigDict(extra="forbid")

    batch_id: str | None
    manifest_digest: str | None
    stream_id: str | None


class S01IntegratorSupplementRequestView(BaseModel):
    """Closed minimized Integrator projection.  It binds exactly the next
    ``submit_attachment_version`` command for the registered source and never
    carries application, reviewer, finding, run, snapshot or policy internals."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    request_id: str
    status: str
    current: bool
    requested_at: int
    due_at: int
    context_digest: str
    upstream_application_ref: str
    material_requirement: S01IntegratorMaterialRequirement
    expected_predecessor_attachment_id: str
    expected_predecessor_attachment_version: int
    next_attachment_version: int
    next_request_progress_revision: int
    next_source_revision: int
    expected_predecessor_revision: int | None
    next_batch_item_sequence: int
    batch: S01IntegratorBatchBinding


class S01AttachmentSubmissionResponse(BaseModel):
    """Closed S02 attachment-version receipt; every current wire field of the
    S02 admission JSON is enumerated with preserved nullability."""

    model_config = ConfigDict(extra="forbid")

    disposition: str
    reason_code: str | None
    responsible_party: str | None
    recovery_action: str | None
    retryable: bool
    application_id: str | None
    receipt_id: str | None
    job_id: str | None
    lifecycle_revision: int | None
    evidence_revision: int | None
    replayed: bool
    envelope_version: str | None
    schema_version: str | None
    semantic_version: str | None
    envelope_id: str | None
    stream_id: str | None
    source_revision: int | None
    source_revision_id: str | None
    envelope_fingerprint: str | None
    adapter_id: str | None
    adapter_version: str | None
    source_registration_digest: str | None
    artifact_manifest_digest: str | None
    fact_counts: dict[str, int]
    gate_results: list[str]
    tenant_id: str
    source_system_id: str
    claim_label: str | None
    real_cross_document_opportunities: int | None
    performance_status: str | None
    request_id: str | None
    request_status: str | None
    batch_id: str | None
    batch_closed: bool | None
    request_progress_revision: int | None
    attachment_id: str | None
    attachment_version: int | None
    supersedes_attachment_id: str | None
    fulfilled: bool | None
    phase: str | None
    route: str | None
    recovery_target: dict[str, Any] | None


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


class T05ExceptionCommandContext(BaseModel):
    """The exact fixed context of the S05 claim/decide/expire/invalidate
    command surface (six keys).  The domain compares it by exact equality, so
    the closed shape prevents arbitrary keys from hiding a missing revision."""

    model_config = ConfigDict(extra="forbid")

    cycle: int
    lifecycle_revision: int
    evidence_revision: int
    run_id: str
    projection_watermark: int
    current_context: str


class T05RoutingContext(BaseModel):
    """The exact routing context of the S05 route command (seven keys)."""

    model_config = ConfigDict(extra="forbid")

    cycle: int
    lifecycle_revision: int
    evidence_revision: int
    run_id: str
    request_id: str
    decision_id: str
    current_context: str


class T05RequestCommandBody(S05RequestBody):
    """The T05 request body: the request binds the Reviewer's exact review
    context, so ``expected_context`` is the closed five-key review context."""

    idempotency_key: str = Field(min_length=1, max_length=200, strict=True)
    expected_context: S01ReviewCommandContext


class T05ClaimCommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_context: T05ExceptionCommandContext


class T05DecisionCommandBody(S05DecisionBody):
    expected_context: T05ExceptionCommandContext


class T05RouteCommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_context: T05RoutingContext
    idempotency_key: str = Field(min_length=1, max_length=200, strict=True)


class T05ExpireCommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_context: T05ExceptionCommandContext
    idempotency_key: str = Field(min_length=1, max_length=200, strict=True)


class T05InvalidationCommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_context: T05ExceptionCommandContext
    reason_code: str = Field(min_length=1, max_length=100, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=200, strict=True)


class T05BusinessExceptionRequestResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    replayed: bool
    application_id: str
    request_id: str
    work_item_id: str
    finding_id: str
    phase: str
    route: str
    expires_at: int
    lifecycle_revision: int
    evidence_revision: int


class T05EvidenceReference(BaseModel):
    """The minimized evidence reference of the approver view: metadata only,
    never raw values, OCR text, credentials, or object paths."""

    model_config = ConfigDict(extra="forbid")

    observation_id: str | None = None
    document_role: str | None = None
    field: str | None = None
    source_page: int | None = None
    source_region: str | None = None


class T05RequesterReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str
    role: str
    source_id: str


class T05ExceptionFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str
    rule_id: str
    verdict: str
    severity: str
    reason_code: str


class T05BusinessExceptionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    request_id: str
    work_item_id: str
    status: str
    current: bool
    currentness_reason: str
    application_reference: str
    finding: T05ExceptionFinding
    evidence_references: list[T05EvidenceReference]
    requester: T05RequesterReference
    request_reason: str
    scope: str
    requested_at: int
    expires_at: int
    run_id: str
    evidence_snapshot_id: str
    evidence_snapshot_digest: str
    release_id: str
    release_digest: str
    checker_build: str
    waiver_policy_id: str
    waiver_policy_digest: str
    claim_status: str
    claim_subject: str | None = None
    claim_fence: int
    claim_expires_at: int
    command_context: T05ExceptionCommandContext
    projection_watermark: int
    actions: list[str]


class T05ExceptionClaimResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    request_id: str
    work_item_id: str
    claim_subject: str | None = None
    claim_fence: int
    claim_expires_at: int


class T05ExceptionDecisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    replayed: bool
    request_id: str
    work_item_id: str
    decision_id: str
    decision: str
    phase: str
    route: str
    successor_work_item_id: str | None = None
    lifecycle_revision: int
    evidence_revision: int
    routing_context: T05RoutingContext | None = None


class T05ExceptionRouteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    replayed: bool
    application_id: str
    request_id: str
    decision_id: str
    phase: str
    route: str
    completion_basis: str | None = None
    successor_work_item_id: str | None = None
    lifecycle_revision: int
    evidence_revision: int


class T05ExceptionDeactivationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    replayed: bool
    application_id: str
    request_id: str
    phase: str
    route: str
    expires_at: int
    reason_code: str
    successor_work_item_id: str | None = None
    lifecycle_revision: int
    evidence_revision: int


class T05BusinessExceptionOperationsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    replayed: bool
    operations: str
    revision: int
    changed_at: int | None = None
    unresolved_request_count: int
    invalidated_request_ids: list[str]
    reason_code: str | None = None
    unchanged: bool | None = None


class T05BusinessExceptionOperationsStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operations: str
    revision: int
    reason_code: str | None = None
    changed_at: int | None = None
    unresolved_request_count: int


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


def _s06_integrator_query_principal(request: Request) -> S01CommandPrincipal:
    """The registered Integrator identity for the minimized request query.

    Every failure mode (missing session, expired identity, wrong role, wrong
    tenant scope) is the same sanitized 404; the domain projection then hides
    requests that do not name this exact source in their allowed policy."""
    principal = _s02_principal(request)
    if (
        principal is None
        or "integrator" not in principal.roles
        or not ControlledScenarioService.is_registered_scope(principal.scope)
    ):
        raise HTTPException(404, detail={"error": "S02_NOT_FOUND"})
    return S01CommandPrincipal(
        subject=principal.subject,
        role="integrator",
        scope=principal.scope,
        source_id=S02_SOURCE_SYSTEM_ID,
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


def _inline_openapi_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Expand a Pydantic JSON schema's ``$defs`` refs into the schema itself.

    ``openapi_extra`` request bodies are emitted inline under the path, so
    document-root ``$ref`` targets cannot address them; openapi-typescript
    rejects unresolvable refs.  Inlining keeps the closed nested shapes while
    producing a fully self-contained schema."""

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.rsplit("/", 1)[-1]
                return resolve(defs[name])
            resolved = {
                key: resolve(value)
                for key, value in node.items()
                if key != "$defs"
            }
            discriminator = resolved.get("discriminator")
            if isinstance(discriminator, dict):
                discriminator.pop("mapping", None)
            return resolved
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    defs = schema.get("$defs", {})
    return resolve(schema)


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
        "blocked": (409, "S03_BLOCKED"),
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


_S05_COMMAND_RESPONSES = {
    404: {"model": T05ErrorResponse},
    409: {"model": T05ErrorResponse},
    413: {"model": T05ErrorResponse},
    422: {"model": T05ErrorResponse},
    503: {"model": T05ErrorResponse},
}


def _s05_command_request_body(model: type[BaseModel]) -> dict[str, Any]:
    """The closed inline request body every S05 command shares: the model's
    schema inlined so the document is self-contained for the client."""

    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": _inline_openapi_schema(
                        model.model_json_schema()
                    ),
                }
            },
        },
    }


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
    # Canonical Integrator page (Issues #54/#45): the same qualified React
    # build as the /react alias with the existing S02 session issuance and
    # no-store.  A missing or incomplete build is an explicit 503.  The
    # legacy template is physically removed (#45); rollback is artifact-only
    # via the prior installed wheel.
    _s02_service()
    if not S01_REACT_INDEX.is_file():
        raise HTTPException(
            503,
            detail={
                "error": "S02_REACT_UNAVAILABLE",
                "message": "Controlled S02 React shell is not built",
            },
        )
    index_html = S01_REACT_INDEX.read_text(encoding="utf-8")
    if not _react_build_is_complete(index_html):
        raise HTTPException(
            503,
            detail={
                "error": "S02_REACT_UNAVAILABLE",
                "message": "Controlled S02 React shell is not built",
            },
        )
    response = HTMLResponse(index_html)
    _issue_s02_session(request, response)
    _s01_disable_cache(response)
    return response


@app.get("/controlled/s02/react", response_class=HTMLResponse)
def controlled_s02_react_page(request: Request) -> HTMLResponse:
    """The Integrator React shell: same built artifact as the Reviewer shell,
    issuing only the existing S02 session.  A missing or incomplete build is
    an explicit 503.  The canonical route uses this build; deployment-only
    rollback is owned by the immediately prior artifact."""
    _s02_service()
    if not S01_REACT_INDEX.is_file():
        raise HTTPException(
            503,
            detail={
                "error": "S02_REACT_UNAVAILABLE",
                "message": "Controlled S02 React shell is not built",
            },
        )
    index_html = S01_REACT_INDEX.read_text(encoding="utf-8")
    if not _react_build_is_complete(index_html):
        raise HTTPException(
            503,
            detail={
                "error": "S02_REACT_UNAVAILABLE",
                "message": "Controlled S02 React shell is not built",
            },
        )
    response = HTMLResponse(index_html)
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


@app.post(
    "/controlled/s02/api/commands/submit-attachment-version",
    response_model=S01AttachmentSubmissionResponse,
    responses={
        403: {"model": S01ErrorResponse},
        404: {"model": S01ErrorResponse},
        409: {"model": S01ErrorResponse},
        413: {"model": S01ErrorResponse},
        422: {"model": S01ErrorResponse},
        503: {"model": S01ErrorResponse},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": S02SubmitBody.model_json_schema(),
                }
            },
        },
    },
)
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


@app.get(
    "/controlled/s02/api/queries/applications/{application_id}/workspace",
    response_model=S01WorkspaceResponse,
    response_model_exclude_none=True,
    responses={404: {"model": S01ErrorResponse}},
)
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


@app.get(
    "/controlled/s02/api/queries/applications/{application_id}/history",
    response_model=S01ApplicationHistoryResponse,
    response_model_exclude_none=True,
    responses={404: {"model": S01ErrorResponse}},
)
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


def _react_shell_index_html() -> str | None:
    """The built React index HTML when the production build is complete,
    else None.  Shared by every shell route so they observe the same
    build."""

    if not S01_REACT_INDEX.is_file():
        return None
    index_html = S01_REACT_INDEX.read_text(encoding="utf-8")
    if not _react_build_is_complete(index_html):
        return None
    return index_html


@app.get("/controlled/s01", response_class=HTMLResponse)
def controlled_s01_page(request: Request) -> HTMLResponse:
    # Canonical Reviewer page (Issues #54/#45): the same qualified React
    # build as the /react alias with the existing S01 session issuance, role
    # checks and no-store.  A missing or incomplete build is an explicit 503.
    # The legacy template is physically removed (#45); rollback is
    # artifact-only via the prior installed wheel.
    _s01_service()
    index_html = _react_shell_index_html()
    if index_html is None:
        raise HTTPException(
            503,
            detail={
                "error": "S01_REACT_UNAVAILABLE",
                "message": "Controlled S01 React shell is not built",
            },
        )
    return _controlled_s01_shell_response(request, index_html)


@app.get("/controlled/s01/react", response_class=HTMLResponse)
def controlled_s01_react_page(request: Request) -> HTMLResponse:
    _s01_service()
    index_html = _react_shell_index_html()
    if index_html is None:
        raise HTTPException(
            503,
            detail={
                "error": "S01_REACT_UNAVAILABLE",
                "message": "Controlled S01 React shell is not built",
            },
        )
    return _controlled_s01_shell_response(request, index_html)


@app.get("/controlled/s05/react", response_class=HTMLResponse)
def controlled_s05_react_page(request: Request) -> HTMLResponse:
    """The Exception Approver React shell: the same built artifact as the
    Reviewer/Integrator shells, served only under the existing Exception
    Approver bearer credential and issuing no session and no authority.  The
    ``request`` query value is presentation/navigation only; the shell reads
    it and the S05 API remains the sole authority."""
    _s05_exception_approver_principal(request)
    index_html = _react_shell_index_html()
    if index_html is None:
        raise HTTPException(
            503,
            detail={
                "error": "S05_REACT_UNAVAILABLE",
                "message": "Controlled S05 React shell is not built",
            },
        )
    response = HTMLResponse(index_html)
    _s01_disable_cache(response)
    return response


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


def _s14_invalid(error: ValueError) -> HTTPException:
    return HTTPException(
        422,
        detail={"error": "S14_COMMAND_INVALID", "message": str(error)},
    )


@app.post(
    "/controlled/s01/api/commands/applications/{application_id}/cancel",
    response_model=S14CommandResult,
    responses=S14_COMMAND_RESPONSES,
)
def controlled_s14_cancel(
    application_id: str,
    body: S14CancelBody,
    request: Request,
    response: Response,
) -> Response:
    """S14 upstream cancellation: enter Terminating and fence all effects."""
    principal = _s01_require_role(request, "integrator")
    _s01_disable_cache(response)
    try:
        result = _s01_service().cancel_application(
            application_id=application_id,
            principal=S01CommandPrincipal(
                subject=principal.subject,
                role="integrator",
                scope=principal.scope,
                source_id="c-demo-web-session",
                expires_at=principal.expires_at,
            ),
            expected_lifecycle_revision=body.expected_lifecycle_revision,
            idempotency_key=body.idempotency_key,
            reason_code=body.reason_code,
        )
    except QueryNotFound as error:
        raise HTTPException(404, detail={"error": "S01_NOT_FOUND"}) from error
    except ValueError as error:
        raise _s14_invalid(error) from error
    return _s14_command_response(result)


@app.post(
    "/controlled/s01/api/commands/applications/{application_id}/settle-termination",
    response_model=S14CommandResult,
    responses=S14_COMMAND_RESPONSES,
)
def controlled_s14_settle(
    application_id: str,
    body: S14SettleBody,
    request: Request,
    response: Response,
) -> Response:
    """S14 operator settlement: seal Terminated only when effects are terminal."""
    principal = _s01_require_operator(request)
    _s01_disable_cache(response)
    try:
        result = _s01_service().settle_termination(
            application_id=application_id,
            principal=S01CommandPrincipal(
                subject=principal.subject,
                role="operator",
                scope=principal.scope,
                source_id="c-demo-operator-control-plane",
                expires_at=principal.expires_at,
            ),
            expected_lifecycle_revision=body.expected_lifecycle_revision,
            idempotency_key=body.idempotency_key,
        )
    except QueryNotFound as error:
        raise HTTPException(404, detail={"error": "S01_NOT_FOUND"}) from error
    except ValueError as error:
        raise _s14_invalid(error) from error
    return _s14_command_response(result)


@app.post(
    "/controlled/s01/api/commands/applications/{application_id}/grant-reopen-permission",
    response_model=S14CommandResult,
    responses=S14_COMMAND_RESPONSES,
)
def controlled_s14_grant_permission(
    application_id: str,
    body: S14GrantPermissionBody,
    request: Request,
    response: Response,
) -> Response:
    """Record one governed, resource-exact reopen permission fact."""
    principal = _s01_require_operator(request)
    _s01_disable_cache(response)
    try:
        result = _s01_service().grant_reopen_permission(
            application_id=application_id,
            principal=S01CommandPrincipal(
                subject=principal.subject,
                role="operator",
                scope=principal.scope,
                source_id="c-demo-operator-control-plane",
                expires_at=principal.expires_at,
            ),
            approver_subject=body.approver_subject,
            permission_id=body.permission_id,
            expected_lifecycle_revision=body.expected_lifecycle_revision,
            idempotency_key=body.idempotency_key,
            ttl_seconds=body.ttl_seconds,
        )
    except QueryNotFound as error:
        raise HTTPException(404, detail={"error": "S01_NOT_FOUND"}) from error
    except ValueError as error:
        raise _s14_invalid(error) from error
    return _s14_command_response(result)


@app.post(
    "/controlled/s01/api/commands/applications/{application_id}/reopen",
    response_model=S14CommandResult,
    responses=S14_COMMAND_RESPONSES,
)
def controlled_s14_reopen(
    application_id: str,
    body: S14ReopenBody,
    request: Request,
    response: Response,
) -> Response:
    """S14 authorized reopen: successor cycle from a Terminated application."""
    principal = _s01_require_operator(request)
    _s01_disable_cache(response)
    try:
        result = _s01_service().reopen_application(
            application_id=application_id,
            principal=S01CommandPrincipal(
                subject=principal.subject,
                role="operator",
                scope=principal.scope,
                source_id="c-demo-operator-control-plane",
                expires_at=principal.expires_at,
            ),
            expected_lifecycle_revision=body.expected_lifecycle_revision,
            idempotency_key=body.idempotency_key,
            target_phase=body.target_phase,
            reopen_policy={
                "permission_id": body.reopen_policy.permission_id,
                "release_digest": body.reopen_policy.release_digest,
            },
        )
    except QueryNotFound as error:
        raise HTTPException(404, detail={"error": "S01_NOT_FOUND"}) from error
    except ValueError as error:
        raise _s14_invalid(error) from error
    return _s14_command_response(result)


@app.post(
    "/controlled/s01/api/commands/process-termination-notification",
    response_model=S14CommandResult,
    responses=S14_COMMAND_RESPONSES,
)
def controlled_s14_process_notification(
    request: Request, response: Response
) -> Response:
    """Deliver one pending termination notification (verified terminal gate)."""
    _s01_require_operator(request)
    _s01_disable_cache(response)
    result = _s01_service().process_termination_notification()
    return _s14_command_response(result)


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


@app.get(
    "/controlled/s01/api/queries/applications/{application_id}/workspace",
    response_model=S01WorkspaceResponse,
    response_model_exclude_none=True,
    responses={
        404: {"model": S01ErrorResponse},
    },
)
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


# The unclaimed/empty work item is serialized with explicit nulls for
# ``claim_subject`` and ``decision`` (the React panel and the focused tests
# pin those nulls), so this endpoint deliberately omits
# ``response_model_exclude_none`` unlike its siblings.
@app.get(
    "/controlled/s01/api/queries/review-work-items/{work_item_id}",
    response_model=S01ReviewWorkItemResponse,
    responses={
        404: {"model": S01ErrorResponse},
    },
)
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


@app.post(
    "/controlled/s01/api/commands/review-work-items/{work_item_id}/claim",
    response_model=S01ClaimResult,
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
                    "schema": _inline_openapi_schema(S01ReviewClaimBody.model_json_schema()),
                }
            },
        },
    },
)
async def controlled_s04_demo_claim_review_work_item(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    body = await _s03_command_body(request, S01ReviewClaimBody)
    assert isinstance(body, S01ReviewClaimBody)
    try:
        result = _s01_service().claim_review_work_item(
            principal=principal,
            work_item_id=work_item_id,
            expected_context=body.expected_context.model_dump(mode="json"),
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error
    return _s03_command_result(result)


@app.post(
    "/controlled/s01/api/commands/review-work-items/{work_item_id}/renew",
    response_model=S01RenewResult,
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
                    "schema": _inline_openapi_schema(S01ReviewFencedBody.model_json_schema()),
                }
            },
        },
    },
)
async def controlled_s04_demo_renew_review_work_item(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    body = await _s03_command_body(request, S01ReviewFencedBody)
    assert isinstance(body, S01ReviewFencedBody)
    try:
        result = _s01_service().renew_review_work_item(
            principal=principal,
            work_item_id=work_item_id,
            expected_fence=body.expected_fence,
            expected_context=body.expected_context.model_dump(mode="json"),
            idempotency_key=body.idempotency_key,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error
    except ValueError as error:
        raise _s03_invalid_command(error) from error
    return _s03_command_result(result)


@app.post(
    "/controlled/s01/api/commands/review-work-items/{work_item_id}/release",
    response_model=S01ReleaseResult,
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
                    "schema": _inline_openapi_schema(S01ReviewFencedBody.model_json_schema()),
                }
            },
        },
    },
)
async def controlled_s04_demo_release_review_work_item(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    body = await _s03_command_body(request, S01ReviewFencedBody)
    assert isinstance(body, S01ReviewFencedBody)
    try:
        result = _s01_service().release_review_work_item(
            principal=principal,
            work_item_id=work_item_id,
            expected_fence=body.expected_fence,
            expected_context=body.expected_context.model_dump(mode="json"),
            idempotency_key=body.idempotency_key,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error
    except ValueError as error:
        raise _s03_invalid_command(error) from error
    return _s03_command_result(result)


@app.post(
    "/controlled/s01/api/commands/review-work-items/{work_item_id}/submit",
    response_model=S01SubmitResult,
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
                    "schema": _inline_openapi_schema(S01ReviewSubmitBody.model_json_schema()),
                }
            },
        },
    },
)
async def controlled_s04_demo_submit_review_work_item(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    body = await _s03_command_body(request, S01ReviewSubmitBody)
    assert isinstance(body, S01ReviewSubmitBody)
    try:
        result = _s01_service().submit_review_work_item(
            principal=principal,
            work_item_id=work_item_id,
            expected_fence=body.expected_fence,
            expected_context=body.expected_context.model_dump(mode="json"),
            idempotency_key=body.idempotency_key,
            verification=body.verification.model_dump(mode="json", exclude_none=True),
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error
    except ValueError as error:
        raise _s03_invalid_command(error) from error
    return _s03_command_result(result)


@app.post(
    "/controlled/s01/api/commands/review-work-items/"
    "{work_item_id}/reveal-field-observation",
    response_model=S01RevealResult,
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
                    "schema": _inline_openapi_schema(
                        S01ReviewRevealBody.model_json_schema()
                    ),
                }
            },
        },
    },
)
async def controlled_s04_demo_reveal_field_observation(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    body = await _s03_command_body(request, S01ReviewRevealBody)
    assert isinstance(body, S01ReviewRevealBody)
    try:
        result = _s01_service().reveal_field_observation(
            principal=principal,
            application_id=body.application_id,
            work_item_id=work_item_id,
            observation_id=body.observation_id,
            expected_fence=body.expected_fence,
            expected_context=body.expected_context.model_dump(mode="json"),
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
    "{work_item_id}/correct-field-observation",
    response_model=S01CorrectionResult,
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
                    "schema": _inline_openapi_schema(
                        S01ReviewCorrectionBody.model_json_schema()
                    ),
                }
            },
        },
    },
)
async def controlled_s04_demo_correct_field_observation(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    body = await _s03_command_body(request, S01ReviewCorrectionBody)
    assert isinstance(body, S01ReviewCorrectionBody)
    try:
        result = _s01_service().correct_field_observation(
            principal=principal,
            application_id=body.application_id,
            work_item_id=work_item_id,
            expected_fence=body.expected_fence,
            expected_context=body.expected_context.model_dump(mode="json"),
            idempotency_key=body.idempotency_key,
            correction=body.correction.model_dump(mode="json"),
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error
    except ValueError as error:
        raise _s03_invalid_command(error) from error
    return _s03_command_result(result)


@app.post(
    "/controlled/s01/api/commands/review-work-items/"
    "{work_item_id}/correct-page-membership",
    response_model=S01MembershipCorrectionResult,
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
                    "schema": _inline_openapi_schema(
                        S01ReviewMembershipBody.model_json_schema()
                    ),
                }
            },
        },
    },
)
async def controlled_s10_demo_correct_page_membership(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    body = await _s03_command_body(request, S01ReviewMembershipBody)
    assert isinstance(body, S01ReviewMembershipBody)
    try:
        result = _s01_service().correct_page_membership(
            principal=principal,
            application_id=body.application_id,
            work_item_id=work_item_id,
            expected_fence=body.expected_fence,
            expected_context=body.expected_context.model_dump(mode="json"),
            idempotency_key=body.idempotency_key,
            membership=body.membership.model_dump(mode="json", exclude_none=True),
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error
    except ValueError as error:
        raise _s03_invalid_command(error) from error
    return _s03_command_result(result)


@app.post(
    "/controlled/s01/api/commands/review-work-items/"
    "{work_item_id}/correct-entity-link",
    response_model=S01EntityLinkCorrectionResult,
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
                    "schema": _inline_openapi_schema(
                        S01ReviewEntityLinkBody.model_json_schema()
                    ),
                }
            },
        },
    },
)
async def controlled_s11_demo_correct_entity_link(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    body = await _s03_command_body(request, S01ReviewEntityLinkBody)
    assert isinstance(body, S01ReviewEntityLinkBody)
    try:
        result = _s01_service().correct_entity_link(
            principal=principal,
            application_id=body.application_id,
            work_item_id=work_item_id,
            expected_fence=body.expected_fence,
            expected_context=body.expected_context.model_dump(mode="json"),
            idempotency_key=body.idempotency_key,
            entity_link=body.entity_link.model_dump(
                mode="json", exclude_none=True
            ),
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error
    except ValueError as error:
        raise _s03_invalid_command(error) from error
    if (
        result.get("status") == "rejected"
        and result.get("reason_code") == "ENTITY_LINK_RELEASE_MISMATCH"
    ):
        # SP-1: unknown/expired/wrong-release candidate provenance is a
        # stable Unprocessable outcome under the existing 422 contract.  The
        # registered S03_REJECTED code plus the stable reason_code are
        # exposed; no manifest content, path or internal exception ever is.
        raise HTTPException(
            422,
            detail={
                "error": "S03_REJECTED",
                "reason_code": "ENTITY_LINK_RELEASE_MISMATCH",
            },
        )
    return _s03_command_result(result)


@app.post(
    "/controlled/s01/api/commands/review-work-items/"
    "{work_item_id}/supplement",
    response_model=S01SupplementRequestResult,
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
                    "schema": _inline_openapi_schema(S05RequestBody.model_json_schema()),
                }
            },
        },
    },
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


@app.get(
    "/controlled/s01/api/queries/supplement-requests/{request_id}",
    response_model=S01SupplementRequestView,
    response_model_exclude_none=True,
    responses={
        404: {"model": S01ErrorResponse},
    },
)
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


@app.get(
    "/controlled/s02/api/queries/supplement-requests/{request_id}",
    response_model=S01IntegratorSupplementRequestView,
    responses={
        404: {"model": S01ErrorResponse},
    },
)
def controlled_s06_integrator_supplement_request_view(
    request_id: str,
    request: Request,
    response: Response,
    principal: S01CommandPrincipal = Depends(_s06_integrator_query_principal),
) -> dict[str, Any]:
    _s01_disable_cache(response)
    try:
        return _s02_service().integrator_supplement_request_view(
            principal=principal,
            request_id=request_id,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise HTTPException(404, detail={"error": "S02_NOT_FOUND"}) from error


@app.post(
    "/controlled/s01/api/commands/review-work-items/"
    "{work_item_id}/business-exceptions",
    response_model=T05BusinessExceptionRequestResult,
    responses=_S05_COMMAND_RESPONSES,
    openapi_extra=_s05_command_request_body(T05RequestCommandBody),
)
async def controlled_s05_request_business_exception(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    body = await _s03_command_body(request, T05RequestCommandBody)
    assert isinstance(body, T05RequestCommandBody)
    try:
        result = _s01_service().request_business_exception(
            principal=principal,
            work_item_id=work_item_id,
            finding_id=body.finding_id,
            reason_code=body.reason_code,
            predecessor_request_id=body.predecessor_request_id,
            expected_fence=body.expected_fence,
            expected_context=body.expected_context.model_dump(mode="json"),
            idempotency_key=body.idempotency_key,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s05_not_found(error) from error
    except ValueError as error:
        raise _s05_invalid_command(error) from error
    return _s05_command_result(result)


@app.get(
    "/controlled/s01/api/queries/business-exceptions/{request_id}",
    response_model=T05BusinessExceptionView,
    responses={
        404: {"model": T05ErrorResponse},
        422: {"model": T05ErrorResponse},
        503: {"model": T05ErrorResponse},
    },
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
    "/controlled/s01/api/commands/exception-work-items/{work_item_id}/claim",
    response_model=T05ExceptionClaimResult,
    responses=_S05_COMMAND_RESPONSES,
    openapi_extra=_s05_command_request_body(T05ClaimCommandBody),
)
async def controlled_s05_claim_exception_work_item(
    work_item_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s05_exception_approver_principal(request)
    body = await _s03_command_body(request, T05ClaimCommandBody)
    assert isinstance(body, T05ClaimCommandBody)
    try:
        result = _s01_service().claim_exception_work_item(
            principal=principal,
            work_item_id=work_item_id,
            expected_context=body.expected_context.model_dump(mode="json"),
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s05_not_found(error) from error
    return _s05_command_result(result)


@app.post(
    "/controlled/s01/api/commands/business-exceptions/{request_id}/decide",
    response_model=T05ExceptionDecisionResult,
    responses=_S05_COMMAND_RESPONSES,
    openapi_extra=_s05_command_request_body(T05DecisionCommandBody),
)
async def controlled_s05_decide_business_exception(
    request_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s05_exception_approver_principal(request)
    body = await _s03_command_body(request, T05DecisionCommandBody)
    assert isinstance(body, T05DecisionCommandBody)
    try:
        result = _s01_service().decide_business_exception(
            principal=principal,
            request_id=request_id,
            work_item_id=body.work_item_id,
            decision=body.decision,
            reason_code=body.reason_code,
            expected_fence=body.expected_fence,
            expected_context=body.expected_context.model_dump(mode="json"),
            idempotency_key=body.idempotency_key,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s05_not_found(error) from error
    except ValueError as error:
        raise _s05_invalid_command(error) from error
    return _s05_command_result(result)


@app.post(
    "/controlled/s01/api/commands/business-exceptions/{request_id}/route",
    response_model=T05ExceptionRouteResult,
    responses=_S05_COMMAND_RESPONSES,
    openapi_extra=_s05_command_request_body(T05RouteCommandBody),
)
async def controlled_s05_route_business_exception(
    request_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s05_router_principal(request)
    body = await _s03_command_body(request, T05RouteCommandBody)
    assert isinstance(body, T05RouteCommandBody)
    try:
        result = _s01_service().determine_business_exception_route(
            principal=principal,
            request_id=request_id,
            expected_context=body.expected_context.model_dump(mode="json"),
            idempotency_key=body.idempotency_key,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s05_not_found(error) from error
    except ValueError as error:
        raise _s05_invalid_command(error) from error
    return _s05_command_result(result)


@app.post(
    "/controlled/s01/api/commands/business-exceptions/{request_id}/expire",
    response_model=T05ExceptionDeactivationResult,
    responses=_S05_COMMAND_RESPONSES,
    openapi_extra=_s05_command_request_body(T05ExpireCommandBody),
)
async def controlled_s05_expire_business_exception(
    request_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s05_router_principal(request)
    body = await _s03_command_body(request, T05ExpireCommandBody)
    assert isinstance(body, T05ExpireCommandBody)
    try:
        result = _s01_service().expire_business_exception(
            principal=principal,
            request_id=request_id,
            expected_context=body.expected_context.model_dump(mode="json"),
            idempotency_key=body.idempotency_key,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s05_not_found(error) from error
    except ValueError as error:
        raise _s05_invalid_command(error) from error
    return _s05_command_result(result)


@app.post(
    "/controlled/s01/api/commands/business-exceptions/{request_id}/invalidate",
    response_model=T05ExceptionDeactivationResult,
    responses=_S05_COMMAND_RESPONSES,
    openapi_extra=_s05_command_request_body(T05InvalidationCommandBody),
)
async def controlled_s05_invalidate_business_exception(
    request_id: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    _s01_disable_cache(response)
    principal = _s05_router_principal(request)
    body = await _s03_command_body(request, T05InvalidationCommandBody)
    assert isinstance(body, T05InvalidationCommandBody)
    try:
        result = _s01_service().invalidate_business_exception(
            principal=principal,
            request_id=request_id,
            reason_code=body.reason_code,
            expected_context=body.expected_context.model_dump(mode="json"),
            idempotency_key=body.idempotency_key,
            now=S01_SESSION_CLOCK(),
        )
    except QueryNotFound as error:
        raise _s05_not_found(error) from error
    except ValueError as error:
        raise _s05_invalid_command(error) from error
    return _s05_command_result(result)


@app.get(
    "/controlled/s01/api/queries/business-exception-operations",
    response_model=T05BusinessExceptionOperationsStatus,
    responses={
        404: {"model": T05ErrorResponse},
        503: {"model": T05ErrorResponse},
    },
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
    "/controlled/s01/api/commands/business-exception-operations/close",
    response_model=T05BusinessExceptionOperationsResult,
    responses=_S05_COMMAND_RESPONSES,
    openapi_extra=_s05_command_request_body(S05OperationsBody),
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
    "/controlled/s01/api/commands/business-exception-operations/resume",
    response_model=T05BusinessExceptionOperationsResult,
    responses=_S05_COMMAND_RESPONSES,
    openapi_extra=_s05_command_request_body(S05OperationsBody),
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


@app.get(
    "/controlled/s01/api/queries/applications/{application_id}/history",
    response_model=S01ApplicationHistoryResponse,
    response_model_exclude_none=True,
    responses={
        404: {"model": S01ErrorResponse},
    },
)
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
    "/controlled/s01/api/queries/impact-dispositions",
    response_model=S09ImpactDispositionsSummaryResponse,
    response_model_exclude_none=True,
    responses={
        404: {"model": S01ErrorResponse},
    },
)
def controlled_s09_impact_dispositions(
    request: Request,
    response: Response,
    final_impact_digest: str,
) -> dict[str, Any]:
    """The minimized Reviewer view of one final impact manifest: aggregate
    digest/count/watermark only.  Per-member receipts live behind the
    audit/reconciliation route."""
    _s01_disable_cache(response)
    principal = _s04_demo_reviewer_principal(request)
    try:
        return _s01_service().impact_dispositions_view(
            principal=principal,
            final_impact_digest=final_impact_digest,
        )
    except QueryNotFound as error:
        raise _s03_not_found(error) from error


@app.get(
    "/controlled/s01/api/queries/impact-dispositions/reconciliation",
    response_model=S09ImpactDispositionsResponse,
    response_model_exclude_none=True,
    responses={
        404: {"model": S01ErrorResponse},
    },
)
def controlled_s09_impact_dispositions_reconciliation(
    request: Request,
    response: Response,
    final_impact_digest: str,
) -> dict[str, Any]:
    """The authorized audit/reconciliation view: per-member application and
    reevaluation-job receipts for one final impact manifest, bound to the
    registered auditor credential and the C-DEMO resource scope."""
    _s01_disable_cache(response)
    principal = _s01_require_auditor(request)
    try:
        return _s01_service().impact_dispositions_view(
            principal=S01CommandPrincipal(
                subject=principal.subject,
                role="auditor",
                scope=principal.scope,
                source_id="c-demo-audit-console",
            ),
            final_impact_digest=final_impact_digest,
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
    # Canonical root (Issues #54/#45): the qualified React demo shell,
    # no-store, fail-closed when the build is missing.  The legacy demo
    # shell is physically removed (#45); rollback is artifact-only via the
    # prior installed wheel.
    index_html = _react_shell_index_html()
    if index_html is None:
        response = JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "error": "DEMO_REACT_UNAVAILABLE",
                    "message": "React demo shell is not built",
                }
            },
        )
        _s01_disable_cache(response)
        return response
    response = HTMLResponse(index_html)
    _s01_disable_cache(response)
    return response


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
        report = _run_check(app_obj, rules_path)
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
    fixture_files = list(body.fixture_files or [])
    apps: list[dict[str, Any]] = list(body.applications or [])
    if not apps and not fixture_files:
        raise HTTPException(400, "applications or fixture_files required")
    # Round26/plan invariant: the count cap is enforced before any fixture
    # read or check, so an over-cap batch is rejected without touching I/O.
    if len(apps) + len(fixture_files) > BATCH_CHECK_MAX_N:
        raise HTTPException(
            400,
            detail={
                "error": "batch_too_large",
                "message": (
                    f"batch size {len(apps) + len(fixture_files)} exceeds "
                    f"max {BATCH_CHECK_MAX_N}"
                ),
                "hint": (
                    f"拆分批次（建议 ≤{BATCH_CHECK_MAX_N}）；"
                    "全量带标签评估请用 CLI: "
                    "python -m task4_consistency evaluate --suite main -c configs/rules_auto_lease.yaml"
                    "（无 /api/evaluate/batch，无异步 job 队列）"
                ),
                "max_n": BATCH_CHECK_MAX_N,
            },
        )
    for name in fixture_files:
        if "/" in name or ".." in name:
            raise HTTPException(400, f"invalid fixture name: {name}")
        fp = FIXTURES / name
        if not fp.exists():
            raise HTTPException(404, f"fixture not found: {name}")
        apps.append(json.loads(fp.read_text(encoding="utf-8")))

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


# --- S08 governed policy HTTP adapter --------------------------------------
# The complete S08 adapter family (identity checks, closed DTOs, stable error
# mapping, command/query adapters, the /controlled/s08/* routes and the React
# shell adapter) lives in web/s08_http.py on its own APIRouter.  Process-level
# authority construction and the test factories remain here.  Registering at
# this point keeps route registration order (and with it the OpenAPI document
# and runtime matching order) identical to the former inline section; the
# live application module is bound for dynamic authority access.
from task4_consistency.web.s08_http import register_router

register_router(app, sys.modules[__name__])

# --- S12 isolated evaluation HTTP adapter --------------------------------
# The typed S12 operator surface (freeze/start/cancel/process/query/rerun)
# lives in web/s12_http.py on its own APIRouter and registers after S08 so
# route registration order stays stable.  S12_SERVICE is None unless
# TASK4_S12_STATE_PATH plus distinct TASK4_S12_CREDENTIAL/SUBJECT are
# configured; every S12 route then reports scoped S12_UNAVAILABLE without
# affecting any S01-S11 route.
from task4_consistency.web.s12_http import (
    _s12_require_operator,
    register_router as register_s12_router,
)

register_s12_router(app, sys.modules[__name__])

# --- S13 downstream-delivery HTTP adapter --------------------------------
# Verification Completed is a lifecycle fact; delivery receipt is a separate
# delivery fact.  The adapter registers after S12 so route order stays stable.
from task4_consistency.web.s13_http import register_router as register_s13_router

register_s13_router(app, sys.modules[__name__])

_S13_SHELL_ERROR_RESPONSES = {
    403: {
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/S13ErrorResponse"}
            }
        },
    },
    503: {
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/S13ErrorResponse"}
            }
        },
    },
}


def _s12_react_shell() -> Response:
    """The shared qualified React build under the S12 operator boundary, or
    the minimized closed S12 503 when the build is missing or partial.  The
    shell never creates an S01/S02 reviewer session and the existing governed
    routes stay available."""
    index_html = _react_shell_index_html()
    if index_html is None:
        response = JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "error": "S12_REACT_UNAVAILABLE",
                    "message": "Controlled S12 React shell is not built",
                }
            },
        )
        _s01_disable_cache(response)
        return response
    response = HTMLResponse(index_html)
    _s01_disable_cache(response)
    return response


def _s13_react_shell() -> Response:
    """The shared qualified React build under the S13 operator boundary, or
    the minimized closed S13 503 when the build is missing or partial.  The
    shell never creates an S01/S02 reviewer session and the existing
    governed routes stay available."""
    index_html = _react_shell_index_html()
    if index_html is None:
        response = JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "error": "S13_REACT_UNAVAILABLE",
                    "message": "Controlled S13 delivery shell is not built",
                }
            },
        )
        _s01_disable_cache(response)
        return response
    response = HTMLResponse(index_html)
    _s01_disable_cache(response)
    return response


@app.get("/controlled/s12", response_class=HTMLResponse)
def controlled_s12_page(request: Request) -> Response:
    """Canonical S12 Evaluation Operator React shell (Ticket #48/T14): the
    same shared qualified React build as the other controlled shells, served
    only to the registered S12 operator credential with no-store shell
    headers and no S01 reviewer session."""
    _s12_require_operator(request)
    return _s12_react_shell()


@app.get("/controlled/s12/react", response_class=HTMLResponse)
def controlled_s12_react_page(request: Request) -> Response:
    """The S12 /react alias: the same closed shell contract as the canonical
    route."""
    _s12_require_operator(request)
    return _s12_react_shell()


@app.get(
    "/controlled/s13",
    response_class=HTMLResponse,
    responses=_S13_SHELL_ERROR_RESPONSES,
)
def controlled_s13_page(request: Request) -> Response:
    """Canonical S13 delivery console React shell (Ticket #49/T15): the
    same shared qualified React build as the other controlled shells, served
    only to the registered S13 operator credential with no-store headers and
    no S01 reviewer session.  The application_id query value is
    presentation/navigation only; the S13 query remains the sole
    authorization/existence authority."""
    from task4_consistency.web.s13_http import _s13_require_operator

    _s13_require_operator(request)
    return _s13_react_shell()


@app.get(
    "/controlled/s13/react",
    response_class=HTMLResponse,
    responses=_S13_SHELL_ERROR_RESPONSES,
)
def controlled_s13_react_page(request: Request) -> Response:
    """The S13 /react alias: the same closed shell contract as the canonical
    route."""
    from task4_consistency.web.s13_http import _s13_require_operator

    _s13_require_operator(request)
    return _s13_react_shell()


def create_s01_test_app() -> FastAPI:
    """Build explicit S01 test wiring without changing the trusted default app."""
    global S01_BACKGROUND_ENABLED, S01_REQUIRE_CONFIGURED_STARTUP
    global S01_SERVICE, S01_TEST_DRIVER, S08_SERVICE

    fixture_value = os.environ.get("TASK4_S01_TEST_FIXTURE_ROOT", "").strip()
    rules_value = os.environ.get("TASK4_S01_TEST_RULES_PATH", "").strip()
    state_value = os.environ.get("TASK4_S01_TEST_STATE_PATH", "").strip()
    scenario_value = os.environ.get("TASK4_S01_TEST_SCENARIO_ID", "").strip()
    s08_corpus_value = os.environ.get("TASK4_S08_TEST_CORPUS_ROOT", "").strip()
    s08_fault_point = os.environ.get("TASK4_S08_TEST_FAULT_POINT", "").strip()
    S01_BACKGROUND_ENABLED = _s01_demo_flag(
        "TASK4_S01_TEST_BACKGROUND_ENABLED", default=True
    )
    S01_REQUIRE_CONFIGURED_STARTUP = False

    def inject_s08_test_fault(write_point: str) -> None:
        if write_point == s08_fault_point:
            raise OSError("injected S08 test fault")

    try:
        state_path = (
            Path(state_value).resolve()
            if state_value
            else Path(tempfile.mkdtemp(prefix="xiaopeng-s01-test-"))
            / "target.sqlite3"
        )
        rules_path = Path(rules_value).resolve() if rules_value else DEFAULT_RULES
        S08_SERVICE = _s08_policy_service(
            state_path=state_path,
            rules_path=rules_path,
            audit_available=_s01_demo_flag(
                "TASK4_S01_TEST_AUDIT_AVAILABLE", default=True
            ),
            storage_available=_s01_demo_flag(
                "TASK4_S01_TEST_STORAGE_AVAILABLE", default=True
            ),
            clock=lambda: int(S01_SESSION_CLOCK()),
            corpus_root=(
                Path(s08_corpus_value).resolve() if s08_corpus_value else FIXTURES
            ),
            fault_injector=(
                inject_s08_test_fault if s08_fault_point else None
            ),
        )
        S01_SERVICE = ControlledScenarioService(
            fixture_root=Path(fixture_value).resolve() if fixture_value else FIXTURES,
            rules_path=rules_path,
            state_path=state_path,
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
            policy_governance=S08_SERVICE,
        )
        if S08_SERVICE is not None:
            S08_SERVICE.bootstrap_once()
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
            downstream_registry=build_c_demo_registry(
                extra_registrations=(
                    [
                        DownstreamRecipientRegistration(
                            scope=f"R-OBSERVED/{S02_TENANT_ID}",
                            recipient_registration_id="c-demo-downstream-review-default",
                            recipient_id="downstream-review-desk",
                        )
                    ]
                    if S02_TENANT_ID
                    else []
                )
            ),
        )
        S01_TEST_DRIVER = None
    except Exception:
        S01_SERVICE = None
        S01_TEST_DRIVER = None
        S02_CONFIGURED = False
    return app


# --- T06 / Issue #40: closed synthetic demo facade ---------------------------

# The sole code-owned allow-list: fixture_id -> fixed basename.  A request
# value is never joined to a filesystem path.
DEMO_FIXTURES: dict[str, str] = {
    "app_demo_step2_ok": "app_demo_step2_ok.json",
    "app_demo_step2_bad_vin": "app_demo_step2_bad_vin.json",
    "app_demo_step2_fmt": "app_demo_step2_fmt.json",
}

_DEMO_EVIDENCE_LIMITATION = (
    "赛题影像页序/检测框元数据（无 OCR 文本）；字段值为合成仿真，不代表真实证据。"
)
_STEP2_SAMPLE_ID_RE = re.compile(r"[A-Za-z0-9_.-]+")
# Neutral code-owned selector copy: never sourced from fixture label,
# expected_verdicts, or outcome-bearing demo_title/demo_desc metadata.
_DEMO_NEUTRAL_TITLES = ("演示样例 1", "演示样例 2", "演示样例 3")
_DEMO_NEUTRAL_DESCRIPTION = "预置合成多单据校验样例"
# The three fixed T06 error contracts: status + exact generic message.  The
# 404 never reflects caller input; 503 never exposes a basename/locator; the
# 500 never includes exception text or internal paths.
_DEMO_ERRORS: dict[str, tuple[int, str]] = {
    "DEMO_FIXTURE_NOT_FOUND": (404, "未找到演示样例"),
    "DEMO_FIXTURE_UNAVAILABLE": (503, "演示样例暂不可用"),
    "DEMO_CHECK_FAILED": (500, "校验执行失败，请稍后重试"),
    # T07: the cap number is server-owned and fixed; the evaluation 503 is a
    # distinct closed contract from any fixture 503.
    "DEMO_BATCH_TOO_LARGE": (
        400,
        f"批量校验数量超过服务端上限 {BATCH_CHECK_MAX_N}",
    ),
    "DEMO_EVALUATION_UNAVAILABLE": (503, "评估摘要暂不可用"),
}


def _demo_error(code: str) -> HTTPException:
    status, message = _DEMO_ERRORS[code]
    return HTTPException(status, detail={"error": code, "message": message})


class DemoFixtureOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_id: str
    title: str
    description: str
    field_source: Literal["synthetic"]
    step2_sample_id: str


class DemoFixturesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixtures: list[DemoFixtureOption]
    # The server-owned batch cap (T07): React renders it but never re-derives
    # a second limit from the client.
    batch_max_n: int


class DemoCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_id: str


class DemoSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consistent: int = 0
    inconsistent: int = 0
    uncertain: int = 0
    skipped: int = 0
    coverage: float = 0.0
    total: int = 0
    total_including_skipped: int = 0


class DemoConfigInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_config_version: str | int | None = None
    rule_package: str | None = None
    rule_changelog: list[str] = Field(default_factory=list)


class DemoSnapshotItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: str
    doc_type: str
    field: str
    raw: str | None
    normalized: str | None
    confidence: float
    ocr_fix: bool | None = None
    pre_ocr: str | None = None
    notes: list[str] = Field(default_factory=list)


class DemoDiffHighlight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pos: int | None = None
    left: str | None = None
    right: str | None = None
    detail: str | None = None


class DemoCheckItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    name: str
    verdict: Literal["consistent", "inconsistent", "uncertain", "skipped"]
    severity: Literal["critical", "major", "minor", "info"]
    message: str
    snapshots: list[DemoSnapshotItem] = Field(default_factory=list)
    diff_highlight: DemoDiffHighlight | None = None
    score: float | None = None
    rule_type: str | None = None
    flags: list[str] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)


class DemoEvidenceLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["step2_sample"]
    label: str
    sample_id: str
    href: str
    limitation: str


class DemoCheckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track: Literal["C-DEMO"]
    data_scope: Literal["synthetic"]
    fixture_id: str
    application_id: str
    summary: DemoSummary
    checks: list[DemoCheckItem]
    config: DemoConfigInfo
    evidence_links: list[DemoEvidenceLink]


class DemoErrorDetail(BaseModel):
    """The closed T06 error detail: the registered code plus the exact fixed
    generic message, with no caller or internal detail."""

    model_config = ConfigDict(extra="forbid")

    error: str
    message: str


class DemoErrorResponse(BaseModel):
    """The closed T06 error envelope registered on every demo error
    response, matching the wire shape consumed by the fetch adapter."""

    model_config = ConfigDict(extra="forbid")

    detail: DemoErrorDetail


def _load_demo_fixture(fixture_id: str) -> dict[str, Any]:
    """Allow-list lookup plus fail-closed validation: the returned mapping is
    always a synthetic fixture whose verified Step2 sample file exists.  All
    failures use the fixed generic contracts; none reflect caller input."""
    basename = DEMO_FIXTURES.get(fixture_id)
    if basename is None:
        raise _demo_error("DEMO_FIXTURE_NOT_FOUND")
    fp = FIXTURES / basename
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:
        raise _demo_error("DEMO_FIXTURE_UNAVAILABLE") from e
    if not isinstance(data, dict):
        raise _demo_error("DEMO_FIXTURE_UNAVAILABLE")
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    if meta.get("field_source") != "synthetic":
        raise _demo_error("DEMO_FIXTURE_UNAVAILABLE")
    sid = meta.get("step2_sample_id")
    if not isinstance(sid, str) or not _STEP2_SAMPLE_ID_RE.fullmatch(sid):
        raise _demo_error("DEMO_FIXTURE_UNAVAILABLE")
    if not (ROOT / "data" / "step2" / f"{sid}_page_order.json").is_file():
        raise _demo_error("DEMO_FIXTURE_UNAVAILABLE")
    return data


@app.get(
    "/api/demo/fixtures",
    response_model=DemoFixturesResponse,
    responses={503: {"model": DemoErrorResponse}},
)
def demo_fixtures() -> DemoFixturesResponse:
    """The closed option list: only validated synthetic Step2-bound fixtures,
    with neutral code-owned copy that never reveals the expected outcome."""
    options: list[DemoFixtureOption] = []
    for index, fixture_id in enumerate(DEMO_FIXTURES):
        data = _load_demo_fixture(fixture_id)
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        options.append(
            DemoFixtureOption(
                fixture_id=fixture_id,
                title=_DEMO_NEUTRAL_TITLES[index],
                description=_DEMO_NEUTRAL_DESCRIPTION,
                field_source="synthetic",
                step2_sample_id=str(meta["step2_sample_id"]),
            )
        )
    return DemoFixturesResponse(
        fixtures=options, batch_max_n=BATCH_CHECK_MAX_N
    )


@app.post(
    "/api/demo/check",
    response_model=DemoCheckResponse,
    responses={
        404: {"model": DemoErrorResponse},
        500: {"model": DemoErrorResponse},
        503: {"model": DemoErrorResponse},
    },
)
def demo_check(body: DemoCheckRequest) -> DemoCheckResponse:
    """Run exactly one server-resident synthetic fixture through the active
    rules and project a typed C-DEMO report with Step2 evidence metadata."""
    data = _load_demo_fixture(body.fixture_id)
    try:
        app_obj = Application.from_dict(data)
        report = _run_check(app_obj, _active_rules_path())
    except Exception as e:
        # Bounded 500: the fixed generic message only; exception text and
        # internal paths stay in the server logs via chaining.
        raise _demo_error("DEMO_CHECK_FAILED") from e
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    sid = str(meta["step2_sample_id"])
    report_dict = report.to_dict()
    return DemoCheckResponse(
        track="C-DEMO",
        data_scope="synthetic",
        fixture_id=body.fixture_id,
        application_id=report.application_id,
        summary=DemoSummary(**report_dict["summary"]),
        checks=[DemoCheckItem(**c) for c in report_dict["checks"]],
        config=DemoConfigInfo(
            rule_config_version=report_dict["rule_config_version"],
            rule_package=report_dict.get("rule_package"),
            rule_changelog=report_dict.get("rule_changelog") or [],
        ),
        evidence_links=[
            DemoEvidenceLink(
                kind="step2_sample",
                label=f"Step2 页序样本 {sid}",
                sample_id=sid,
                href=f"/api/step2/{sid}",
                limitation=_DEMO_EVIDENCE_LIMITATION,
            )
        ],
    )


@app.get("/demo/react", response_class=HTMLResponse)
def demo_react_shell() -> HTMLResponse:
    """The exact additive demo shell route: the same built React artifact as
    the controlled shells, no-store, and fail-closed when the build is
    missing.  No catch-all route may intercept /api or controlled 404s."""
    index_html = _react_shell_index_html()
    if index_html is None:
        raise HTTPException(
            503,
            detail={
                "error": "DEMO_REACT_UNAVAILABLE",
                "message": "React demo shell is not built",
            },
        )
    response = HTMLResponse(index_html)
    _s01_disable_cache(response)
    return response


# --- T07 / Issue #41: bounded demo batch check + read-only fixed-main
# evaluation-summary projection ---------------------------------------------

# The fixed generic per-item failure text: exception text and internal paths
# stay in the server logs via chaining, never in the API payload.
_DEMO_BATCH_ITEM_FAILED = "条目校验失败，请稍后重试"

# Server-owned scope copy for the read-only evaluation summary; React renders
# it verbatim and never edits or re-derives it.
_DEMO_EVAL_SCOPE = "合成开发/回归语料（suite=main）"


class DemoBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_ids: list[str] = Field(min_length=1)


class DemoBatchTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consistent: int = 0
    inconsistent: int = 0
    uncertain: int = 0
    skipped: int = 0


class DemoBatchIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    verdict: Literal["consistent", "inconsistent", "uncertain", "skipped"]
    message: str
    reason_codes: list[str] = Field(default_factory=list)


class DemoBatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_id: str
    # Explicit terminal outcome: absence never implies success.
    outcome: Literal["completed", "failed"]
    application_id: str | None = None
    summary: DemoSummary | None = None
    issues: list[DemoBatchIssue] = Field(default_factory=list)
    error: str | None = None


class DemoBatchCheckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track: Literal["C-DEMO"]
    data_scope: Literal["synthetic"]
    requested: int
    completed: int
    failed: int
    # Enclosing outcome over all requested items: completed / partial /
    # failed, so a partially-failed run is never presented as success.
    outcome: Literal["completed", "partial", "failed"]
    totals: DemoBatchTotals
    results: list[DemoBatchItem]


@app.post(
    "/api/demo/check/batch",
    response_model=DemoBatchCheckResponse,
    responses={
        400: {"model": DemoErrorResponse},
        404: {"model": DemoErrorResponse},
        422: {"model": DemoErrorResponse},
        503: {"model": DemoErrorResponse},
    },
)
def demo_batch_check(body: DemoBatchRequest) -> DemoBatchCheckResponse:
    """One bounded synchronous run of server-resident synthetic fixtures.

    The count cap is enforced before any fixture I/O or allow-list work, and
    an unknown id fails closed before any check runs.  Each item is an
    explicit completed/failed terminal outcome; the enclosing outcome is
    completed/partial/failed.  No async queue, job, or evaluate batch exists.
    """
    ids = body.fixture_ids
    if len(ids) > BATCH_CHECK_MAX_N:
        raise _demo_error("DEMO_BATCH_TOO_LARGE")
    for fixture_id in ids:
        if fixture_id not in DEMO_FIXTURES:
            raise _demo_error("DEMO_FIXTURE_NOT_FOUND")

    # One synchronous request observes exactly one rules/engine snapshot:
    # the engine is built once and every item runs on it, so no mixed rule
    # versions can combine in one response.
    try:
        engine = _engine()
    except Exception:
        # Bounded closed failure: no engine snapshot could be built; every
        # requested item fails generically with zero completed-only totals.
        failed_items = [
            DemoBatchItem(
                fixture_id=fid,
                outcome="failed",
                error=_DEMO_BATCH_ITEM_FAILED,
            )
            for fid in ids
        ]
        return DemoBatchCheckResponse(
            track="C-DEMO",
            data_scope="synthetic",
            requested=len(ids),
            completed=0,
            failed=len(ids),
            outcome="failed",
            totals=DemoBatchTotals(),
            results=failed_items,
        )

    results: list[DemoBatchItem] = []
    totals = DemoBatchTotals()
    for fixture_id in ids:
        try:
            data = _load_demo_fixture(fixture_id)
            app_obj = Application.from_dict(data)
            report = engine.run(app_obj)
            summary = report.summary
            issues = [
                DemoBatchIssue(
                    rule_id=c.rule_id,
                    verdict=c.verdict.value,
                    message=c.message,
                    reason_codes=list(c.reason_codes or []),
                )
                for c in report.checks
                if c.verdict.value in ("inconsistent", "uncertain")
            ]
            item = DemoBatchItem(
                fixture_id=fixture_id,
                outcome="completed",
                application_id=report.application_id,
                summary=DemoSummary(**summary.to_dict()),
                issues=issues,
            )
            # Commit counts to completed-only totals only after the whole
            # item (including the issue projection) succeeded, so a late
            # failure can never leave partial counts.
            results.append(item)
            totals.consistent += summary.consistent
            totals.inconsistent += summary.inconsistent
            totals.uncertain += summary.uncertain
            totals.skipped += summary.skipped
        except Exception:
            # Bounded per-item failure: the fixed generic message only;
            # exception text and internal paths stay in the server logs.
            results.append(
                DemoBatchItem(
                    fixture_id=fixture_id,
                    outcome="failed",
                    error=_DEMO_BATCH_ITEM_FAILED,
                )
            )
    completed = sum(1 for item in results if item.outcome == "completed")
    failed = len(results) - completed
    if completed == 0:
        outcome: Literal["completed", "partial", "failed"] = "failed"
    elif failed == 0:
        outcome = "completed"
    else:
        outcome = "partial"
    return DemoBatchCheckResponse(
        track="C-DEMO",
        data_scope="synthetic",
        requested=len(ids),
        completed=completed,
        failed=failed,
        outcome=outcome,
        totals=totals,
        results=results,
    )


class DemoEvalCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n_apps_loaded: int
    n_check_ok: int
    n_check_fail: int
    total_pairs: int
    decisive_pairs: int
    true_positive: int
    true_negative: int
    false_positive: int
    false_negative: int
    uncertain_when_labeled: int
    n_inconsistent_labeled_decisive: int
    n_expected_inconsistent: int
    n_missed_inconsistent: int


class DemoEvalRates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coverage: float
    false_positive_rate: float
    false_negative_rate: float
    accuracy: float
    miss_rate: float
    uncertain_rate: float
    mean_app_coverage: float


class DemoEvaluationSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # available = labeled fixed-main computation; empty = smoke/empty corpus
    # with nullable counts/rates, never zero-valued success.
    summary_state: Literal["available", "empty"]
    suite: Literal["main"]
    # Server-owned claim metadata: development/regression scope only, and the
    # explicit gap to any formal/production evaluation.
    claim: Literal["C-DEV-REG"]
    performance_gap: Literal["UNVERIFIED"]
    scope: str
    counts: DemoEvalCounts | None = None
    rates: DemoEvalRates | None = None
    warnings: list[str] = Field(default_factory=list)
    honesty_note: str = ""


@app.get(
    "/api/demo/evaluate/summary",
    response_model=DemoEvaluationSummaryResponse,
    responses={503: {"model": DemoErrorResponse}},
)
def demo_evaluate_summary() -> DemoEvaluationSummaryResponse:
    """Read-only fixed-main evaluation summary projection.

    The computation stays on the existing evaluate_suite('main') authority;
    only summary counts/rates, warnings, the honesty note, and the
    server-owned claim metadata cross the API — no legacy HTML, pairs,
    per-application labels, pass_thresholds, or rules paths.  An empty/smoke
    corpus is an explicit empty state with nullable rates; unavailable
    evaluation is a distinct closed 503.
    """
    from task4_consistency.evaluate import evaluate_suite

    try:
        metrics = evaluate_suite("main", _active_rules_path())
    except Exception as e:
        raise _demo_error("DEMO_EVALUATION_UNAVAILABLE") from e

    warnings_list = list(metrics.warnings or [])
    if metrics.mode == "smoke" or metrics.total_pairs == 0:
        return DemoEvaluationSummaryResponse(
            summary_state="empty",
            suite="main",
            claim="C-DEV-REG",
            performance_gap="UNVERIFIED",
            scope=_DEMO_EVAL_SCOPE,
            counts=None,
            rates=None,
            warnings=warnings_list,
            honesty_note=metrics.honesty_note,
        )
    return DemoEvaluationSummaryResponse(
        summary_state="available",
        suite="main",
        claim="C-DEV-REG",
        performance_gap="UNVERIFIED",
        scope=_DEMO_EVAL_SCOPE,
        counts=DemoEvalCounts(
            n_apps_loaded=metrics.n_apps_loaded,
            n_check_ok=metrics.n_check_ok,
            n_check_fail=metrics.n_check_fail,
            total_pairs=metrics.total_pairs,
            decisive_pairs=metrics.decisive_pairs,
            true_positive=metrics.true_positive,
            true_negative=metrics.true_negative,
            false_positive=metrics.false_positive,
            false_negative=metrics.false_negative,
            uncertain_when_labeled=metrics.uncertain_when_labeled,
            n_inconsistent_labeled_decisive=metrics.n_inconsistent_labeled_decisive,
            n_expected_inconsistent=metrics.n_expected_inconsistent,
            n_missed_inconsistent=metrics.n_missed_inconsistent,
        ),
        rates=DemoEvalRates(
            coverage=metrics.coverage,
            false_positive_rate=metrics.false_positive_rate,
            false_negative_rate=metrics.false_negative_rate,
            accuracy=metrics.accuracy,
            miss_rate=metrics.miss_rate,
            uncertain_rate=metrics.uncertain_rate,
            mean_app_coverage=metrics.mean_app_coverage,
        ),
        warnings=warnings_list,
        honesty_note=metrics.honesty_note,
    )


# --- Issue #54: application-entry observation telemetry ----------------------
# One closed observation module behind the FastAPI adapter.  Registered
# OUTERMOST (the last add_middleware call wraps everything else) so it sees
# all HTTP, static mounts, auth and 404s, plus lifespan scopes, and records
# the resolved route owner and final status after response resolution.
# Ordinary runs with no observation environment stay capture-free
# (NoopRecorder).  Observation writes zero lifecycle, evidence, audit or
# business revisions and stays a separate evidence stream from the Security
# Audit ledger (ADR-0004).  A recorder failure surfaces as a failed request;
# it never silently drops evidence.
_OBSERVATION_RECORDER = recorder_from_env(
    family_table=app_family_table(app),
)
app.add_middleware(ObservationMiddleware, recorder=_OBSERVATION_RECORDER)


def create_app() -> FastAPI:
    return app
