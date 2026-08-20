"""Ticket #28 S12 — typed HTTP operator surface for the isolated evaluation
plane.

The acceptance seam is the FastAPI app with a monkeypatched S12 authority:
the S12 routes are registered on the shared app and resolve the live
``S12_SERVICE``/credential module attributes per request, so a configured
service behaves exactly like production wiring while missing configuration
keeps S01-S11 routes available and reports scoped ``S12_UNAVAILABLE``.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from task4_consistency.controlled.s01_checker import TargetRelease
from task4_consistency.controlled.s12 import EvaluationService
from task4_consistency.kb.store import get_kb
from task4_consistency.rules.loader import load_rules
from task4_consistency.web import app as webapp

from tests.test_s12_controlled import (
    _complete_run_spec,
    _plate_documents,
    _small_c_plan_command,
)

ROOT = Path(__file__).resolve().parents[1]

S12_CREDENTIAL = "s12-registered-operator-test-credential"
S12_SUBJECT = "c-demo-evaluation-operator"

_ZERO_BUSINESS_DELTAS = {
    "lifecycle_revision": 0,
    "evidence_rows": 0,
    "evidence_digest": None,
    "current_run_pointer": 0,
    "policy_revision": 0,
    "governance_revision": 0,
}


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {S12_CREDENTIAL}"}


def _s12_fixture_release() -> TargetRelease:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    return TargetRelease.compile(
        load_rules(rules_path),
        hashlib.sha256(rules_path.read_bytes()).hexdigest(),
        knowledge=get_kb().to_dict(),
    )


def _s12_plan_command() -> tuple[TargetRelease, dict[str, Any], dict[str, Any]]:
    release = _s12_fixture_release()
    run_specs = {
        f"app-{index}": _complete_run_spec(
            release, _plate_documents("苏A92054", second_role=True), application_id=f"app-{index}"
        )
        for index in range(4)
    }
    return release, run_specs, _small_c_plan_command(release, run_specs)


def _install_service(monkeypatch: Any, tmp_path: Path) -> EvaluationService:
    service = EvaluationService(state_path=tmp_path / "evaluation.sqlite3", clock=lambda: 1700000000)
    monkeypatch.setattr(webapp, "S12_SERVICE", service)
    monkeypatch.setattr(webapp, "S12_CREDENTIAL", S12_CREDENTIAL)
    monkeypatch.setattr(webapp, "S12_SUBJECT", S12_SUBJECT)
    return service


def test_s12_routes_scoped_unavailable_and_s01_s11_continuity(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Missing S12 configuration: every S12 route reports the closed
    S12_UNAVAILABLE envelope while an S01-S11 route keeps serving."""
    monkeypatch.setattr(webapp, "S12_SERVICE", None)
    client = TestClient(webapp.app)
    _release, _run_specs, plan_command = _s12_plan_command()
    response = client.post(
        "/controlled/s12/plans/freeze",
        json=plan_command,
        headers=_auth(),
    )
    assert response.status_code == 503, response.text
    assert response.json() == {
        "detail": {
            "error": "S12_UNAVAILABLE",
            "message": "Controlled S12 evaluation plane is unavailable",
        }
    }
    assert client.get("/api/demo/fixtures").status_code == 200


def test_s12_routes_require_registered_operator_identity(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _install_service(monkeypatch, tmp_path)
    client = TestClient(webapp.app)
    _release, _run_specs, plan_command = _s12_plan_command()
    response = client.post(
        "/controlled/s12/plans/freeze",
        json=plan_command,
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == "S12_FORBIDDEN"


def test_typed_freeze_start_process_query_bundle_and_rerun(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The typed HTTP chain: freeze -> start -> process -> job query ->
    bundle query -> linked rerun, with closed envelopes and zero business
    deltas throughout."""
    _install_service(monkeypatch, tmp_path)
    release, run_specs, plan_command = _s12_plan_command()
    client = TestClient(webapp.app)

    frozen = client.post(
        "/controlled/s12/plans/freeze", json=plan_command, headers=_auth()
    )
    assert frozen.status_code == 200, frozen.text
    plan = frozen.json()
    assert plan["schema_version"] == "s12-evaluation-plan/1"
    assert plan["plan_id"] == "plan-c-1"
    assert len(plan["plan_digest"]) == 64

    started = client.post(
        "/controlled/s12/jobs/start",
        json={"plan_id": "plan-c-1", "worker_id": "s12-http-worker"},
        headers=_auth(),
    )
    assert started.status_code == 200, started.text
    job = started.json()
    assert job["status"] == "queued"
    assert job["fence"] == 0
    assert job["attempt_no"] == 0
    job_id = job["job_id"]

    processed = client.post(
        f"/controlled/s12/jobs/{job_id}/process", headers=_auth()
    )
    assert processed.status_code == 200, processed.text
    outcome = processed.json()
    assert outcome["status"] == "INSUFFICIENT"
    assert outcome["bundle_id"] is not None
    bundle_id = outcome["bundle_id"]

    queried_job = client.get(
        f"/controlled/s12/jobs/{job_id}", headers=_auth()
    )
    assert queried_job.status_code == 200, queried_job.text
    assert queried_job.json()["status"] == "complete"
    assert queried_job.json()["result"]["bundle_id"] == bundle_id

    queried_bundle = client.get(
        f"/controlled/s12/bundles/{bundle_id}", headers=_auth()
    )
    assert queried_bundle.status_code == 200, queried_bundle.text
    bundle = queried_bundle.json()
    assert bundle["schema_version"] == "s12-evaluation-bundle/1"
    assert bundle["status"] == "INSUFFICIENT"
    # The real runner evaluated every application: app-3 is consistent while
    # its gold is inconsistent, which stays in the denominator as a miss.
    assert bundle["predictions"]["opp-3"] == "consistent"
    assert bundle["tracks"]["C"]["denominators"]["E"] == 4
    assert bundle["business_deltas"] == _ZERO_BUSINESS_DELTAS

    rerun = client.post(
        f"/controlled/s12/jobs/{job_id}/rerun", headers=_auth()
    )
    assert rerun.status_code == 200, rerun.text
    rerun_job = rerun.json()
    assert rerun_job["rerun_of_bundle_id"] == bundle_id
    rerun_processed = client.post(
        f"/controlled/s12/jobs/{rerun_job['job_id']}/process", headers=_auth()
    )
    assert rerun_processed.status_code == 200, rerun_processed.text
    rerun_bundle_id = rerun_processed.json()["bundle_id"]
    assert rerun_bundle_id != bundle_id
    rerun_bundle = client.get(
        f"/controlled/s12/bundles/{rerun_bundle_id}", headers=_auth()
    ).json()
    assert rerun_bundle["rerun_of_bundle_id"] == bundle_id
    # The source bundle stays byte-identical after the rerun.
    assert (
        client.get(f"/controlled/s12/bundles/{bundle_id}", headers=_auth()).json()
        == bundle
    )


def test_s12_http_unknown_and_invalid_commands_are_closed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _install_service(monkeypatch, tmp_path)
    client = TestClient(webapp.app)
    unknown_job = client.get(
        "/controlled/s12/jobs/does-not-exist", headers=_auth()
    )
    assert unknown_job.status_code == 404, unknown_job.text
    assert unknown_job.json()["detail"]["error"] == "S12_NOT_FOUND"
    unknown_bundle = client.get(
        "/controlled/s12/bundles/" + "0" * 64, headers=_auth()
    )
    assert unknown_bundle.status_code == 404, unknown_bundle.text
    bad_freeze = client.post(
        "/controlled/s12/plans/freeze",
        json={"schema_version": "s12-plan-command/9", "plan_id": "p"},
        headers=_auth(),
    )
    assert bad_freeze.status_code == 422, bad_freeze.text


def test_s12_http_process_cancelled_job_returns_closed_envelope(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A cancelled job processed over HTTP returns the closed failed envelope
    (not a 500): the claim-failure path validates against S12ProcessResponse
    defaults and the job stays cancelled with no bundle."""
    _install_service(monkeypatch, tmp_path)
    _release, _run_specs, plan_command = _s12_plan_command()
    client = TestClient(webapp.app)
    client.post("/controlled/s12/plans/freeze", json=plan_command, headers=_auth())
    started = client.post(
        "/controlled/s12/jobs/start",
        json={"plan_id": "plan-c-1", "worker_id": "s12-http-worker"},
        headers=_auth(),
    ).json()
    cancelled = client.post(
        f"/controlled/s12/jobs/{started['job_id']}/cancel", headers=_auth()
    )
    assert cancelled.status_code == 200, cancelled.text
    processed = client.post(
        f"/controlled/s12/jobs/{started['job_id']}/process", headers=_auth()
    )
    assert processed.status_code == 200, processed.text
    outcome = processed.json()
    assert outcome["status"] == "failed"
    assert outcome["reason_code"] == "JOB_CANCELLED"
    assert outcome["bundle_id"] is None
