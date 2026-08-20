"""S12 isolated evaluation HTTP adapter (Ticket #28).

The typed operator surface for the S12 evaluation plane: freeze a plan,
start/cancel/process/query a durable job, rerun a published job, and query
an immutable content-addressed bundle.  The module owns no process-level
authority construction: ``web/app.py`` builds ``S12_SERVICE`` and registers
the router through ``register_router`` on the shared FastAPI application.
Missing S12 configuration leaves S01-S11 startup and routes available while
every S12 route reports scoped unavailability (``S12_UNAVAILABLE``).

DTOs are closed (``extra="forbid"``): generated clients can never invent a
field the authority will reject, and the bundle/job/plan envelopes mirror
the registered contract instead of an open dictionary.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from task4_consistency.controlled.s12 import (
    EvaluationService,
    S12IntegrityError,
    S12Unavailable,
)

s12_router = APIRouter()

# The private FastAPI ``app.state`` key holding the application module bound
# by ``register_router``; requests resolve their authority per app.
_S12_APP_MODULE_STATE_KEY = "_s12_application_module"


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
# Closed typed DTOs
# ---------------------------------------------------------------------------


class S12Opportunity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opportunity_id: str
    track: Literal["R", "C"]
    cluster: str
    application_id: str
    cycle: int
    check_id: str
    target_scope: str
    evidence_snapshot_id: str
    label: Literal["consistent", "inconsistent", "indeterminate", "not_applicable"]


class S12Cluster(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster_id: str
    stratum: str
    applications: list[str]
    usage: Literal["development", "calibration", "acceptance_holdout"]


class S12Membership(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opportunities: list[str]


class S12FreezePlanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["s12-plan-command/1"]
    plan_id: str
    scope: str
    seed: int
    budget: dict[str, int]
    stop_rule: str
    split: dict[str, Any]
    environment: dict[str, Any]
    release: dict[str, Any]
    checker_artifact: dict[str, Any]
    run_specs: dict[str, Any]
    clusters: list[S12Cluster]
    tracks: dict[str, S12Membership]
    views: dict[str, S12Membership]
    opportunities: list[S12Opportunity]


class S12PlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["s12-evaluation-plan/1"]
    plan_id: str
    scope: str
    seed: int
    budget: dict[str, int]
    stop_rule: str
    split: dict[str, Any]
    environment: dict[str, Any]
    release: dict[str, Any]
    checker_artifact: dict[str, Any]
    run_specs: dict[str, Any]
    clusters: list[dict[str, Any]]
    tracks: dict[str, dict[str, Any]]
    views: dict[str, dict[str, Any]]
    opportunities: list[dict[str, Any]]
    frozen_at: int
    plan_digest: str


class S12StartJobBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    worker_id: str


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
    rerun_of_bundle_id: str | None
    result: dict[str, Any] | None = None
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
    scope: str
    status_reasons: list[str]
    tracks: dict[str, Any]
    views: dict[str, Any]
    predictions: dict[str, str]
    prediction_alphabet: list[str]
    gold_alphabet: list[str]
    errors: list[dict[str, str]]
    missing_opportunities: list[str]
    release: dict[str, Any]
    environment: dict[str, Any]
    stop_rule_satisfied: bool
    split: dict[str, Any]
    evidence_snapshot_ids: list[str]
    seed: int
    budget: dict[str, int]
    stop_rule: str
    business_deltas: dict[str, Any]
    command: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@s12_router.post("/controlled/s12/plans/freeze", response_model=S12PlanResponse)
def s12_freeze_plan(body: S12FreezePlanBody, request: Request) -> dict[str, Any]:
    """Freeze one evaluation plan: cohorts, clusters, splits, opportunities,
    independent gold labels, evidence snapshots, release/checker/build
    identities, seed, budget and stop rule become one immutable plan."""
    _s12_require_operator(request)
    try:
        return _s12_service(request).freeze_plan(body.model_dump())
    except ValueError:
        raise _s12_invalid_command()
    except (S12IntegrityError, S12Unavailable):
        raise _s12_closed_failure()


@s12_router.post("/controlled/s12/jobs/start", response_model=S12JobResponse)
def s12_start_job(body: S12StartJobBody, request: Request) -> dict[str, Any]:
    """Start one durable evaluation job for a frozen plan."""
    _s12_require_operator(request)
    try:
        return _s12_service(request).start_job(body.plan_id, body.worker_id)
    except ValueError:
        raise _s12_not_found()
    except (S12IntegrityError, S12Unavailable):
        raise _s12_closed_failure()


@s12_router.post(
    "/controlled/s12/jobs/{job_id}/process", response_model=S12ProcessResponse
)
def s12_process_job(job_id: str, request: Request) -> dict[str, Any]:
    """Execute one claimed durable job: restricted runner, parent
    materialization, statistics, status selection, bundle publication."""
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
    """Start a linked rerun job: its bundle will reference the source
    bundle via ``rerun_of_bundle_id`` and never overwrite it."""
    _s12_require_operator(request)
    try:
        source = _s12_service(request).query_job(job_id)
        return _s12_service(request).rerun_job(job_id, source["worker_id"])
    except ValueError:
        raise _s12_not_found()
    except (S12IntegrityError, S12Unavailable):
        raise _s12_closed_failure()


@s12_router.get(
    "/controlled/s12/bundles/{bundle_id}", response_model=S12BundleResponse
)
def s12_query_bundle(bundle_id: str, request: Request) -> dict[str, Any]:
    """Query one immutable content-addressed evaluation bundle."""
    _s12_require_operator(request)
    try:
        return _s12_service(request).query_bundle(bundle_id)
    except ValueError:
        raise _s12_not_found()
    except (S12IntegrityError, S12Unavailable):
        raise _s12_closed_failure()
