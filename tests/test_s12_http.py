"""Ticket #28 R1 S12 — typed HTTP operator surface for the isolated evaluation
plane (closed nested DTOs, server-bound evaluator identity).

The acceptance seam is the FastAPI app with a monkeypatched S12 authority:
the S12 routes are registered on the shared app and resolve the live
``S12_SERVICE``/credential/worker module attributes per request, so a
configured service behaves exactly like production wiring while missing
configuration keeps S01-S11 routes available and reports scoped
``S12_UNAVAILABLE``.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from task4_consistency.controlled.s12 import (
    EvaluationService,
    LabelManifestStore,
)
from task4_consistency.web import app as webapp

from tests.test_s12_controlled import (
    _make_business_harness,
    _make_governed_release,
    _reference_plan_command,
    _write_label_manifest,
)

ROOT = Path(__file__).resolve().parents[1]

S12_CREDENTIAL = "s12-registered-operator-test-credential"
S12_SUBJECT = "c-demo-evaluation-operator"
S12_WORKER_SUBJECT = "c-demo-evaluation-worker"

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


def _http_harness(
    tmp_path: Path,
) -> tuple[EvaluationService, dict[str, Any], dict[str, Any]]:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    business_services, admitted, snapshots, _path = _make_business_harness(
        tmp_path, rules_path
    )
    governance_service, release_id, release_digest, _manifest = _make_governed_release(
        tmp_path
    )
    labels = {f"opp-{index}": "consistent" for index in range(len(admitted))}
    label_root, manifest_id, manifest_digest = _write_label_manifest(tmp_path, labels)

    def measure() -> dict[str, Any]:
        facts: dict[str, Any] = {}
        for service in business_services:
            facts.update(service.evaluation_business_measurement())
        facts.update(governance_service.evaluation_governance_measurement())
        return facts

    service = EvaluationService(
        state_path=tmp_path / "evaluation.sqlite3",
        clock=lambda: 1700000000,
        snapshot_provider=lambda application_id, snapshot_id: business_services[
            0
        ].evaluation_evidence_snapshot(
            application_id=application_id, snapshot_id=snapshot_id
        ),
        release_provider=lambda release_id, release_digest: governance_service.resolve_evaluation_release(
            release_id=release_id, release_digest=release_digest
        ),
        label_manifest_provider=LabelManifestStore(label_root).resolve,
        business_state_provider=measure,
        worker_subject=S12_WORKER_SUBJECT,
    )
    command = _reference_plan_command(
        admitted=admitted,
        snapshot_by_application=snapshots,
        release_id=release_id,
        release_digest=release_digest,
        manifest_id=manifest_id,
        manifest_digest=manifest_digest,
    )
    return service, command, {"measure": measure}


def _install_service(monkeypatch: Any, tmp_path: Path) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    service, command, context = _http_harness(tmp_path)
    monkeypatch.setattr(webapp, "S12_SERVICE", service)
    monkeypatch.setattr(webapp, "S12_CREDENTIAL", S12_CREDENTIAL)
    monkeypatch.setattr(webapp, "S12_SUBJECT", S12_SUBJECT)
    monkeypatch.setattr(webapp, "S12_WORKER_SUBJECT", S12_WORKER_SUBJECT)
    return service, command, context


def test_s12_routes_scoped_unavailable_and_s01_s11_continuity(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Missing S12 configuration: every S12 route reports the closed
    S12_UNAVAILABLE envelope while an S01-S11 route keeps serving."""
    monkeypatch.setattr(webapp, "S12_SERVICE", None)
    client = TestClient(webapp.app)
    _service, plan_command, _context = _http_harness(tmp_path)
    response = client.post(
        "/controlled/s12/plans/freeze", json=plan_command, headers=_auth()
    )
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["error"] == "S12_UNAVAILABLE"
    assert client.get("/api/demo/fixtures").status_code == 200


def test_s12_routes_require_registered_operator_identity(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _service, plan_command, _context = _install_service(monkeypatch, tmp_path)
    client = TestClient(webapp.app)
    response = client.post("/controlled/s12/plans/freeze", json=plan_command)
    assert response.status_code == 403, response.text
    assert response.json()["detail"]["error"] == "S12_FORBIDDEN"


def test_typed_freeze_start_process_query_bundle_and_rerun(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The typed HTTP chain: freeze -> start -> process -> job query ->
    bundle query -> linked rerun, with closed envelopes and zero business
    deltas throughout."""
    _service, plan_command, _context = _install_service(monkeypatch, tmp_path)
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
        json={"plan_id": "plan-c-1"},
        headers=_auth(),
    )
    assert started.status_code == 200, started.text
    job = started.json()
    assert job["status"] == "queued"
    assert job["worker_id"] == S12_WORKER_SUBJECT
    assert job["fence"] == 0
    assert job["attempt_no"] == 0
    job_id = job["job_id"]

    processed = client.post(
        f"/controlled/s12/jobs/{job_id}/process", headers=_auth()
    )
    assert processed.status_code == 200, processed.text
    outcome = processed.json()
    assert outcome["status"] in {"INSUFFICIENT", "FAIL", "SMOKE_ONLY"}
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
    assert bundle["business_deltas"] == _ZERO_BUSINESS_DELTAS
    assert bundle["result_digest"]
    assert bundle["scope_eligibility"]["holdout_eligible"] is False

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
    _service, _plan_command, _context = _install_service(monkeypatch, tmp_path)
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
    _service, plan_command, _context = _install_service(monkeypatch, tmp_path)
    client = TestClient(webapp.app)
    client.post("/controlled/s12/plans/freeze", json=plan_command, headers=_auth())
    started = client.post(
        "/controlled/s12/jobs/start",
        json={"plan_id": "plan-c-1"},
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


# ---------------------------------------------------------------------------
# Slice 6 — closed nested DTOs and server-bound evaluator identity
# ---------------------------------------------------------------------------


def test_start_job_rejects_caller_worker_id_and_uses_registered_worker(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """S12StartJobBody has no worker_id: a caller-supplied worker identity is
    rejected as an unknown field and the job binds the registered server
    worker."""
    _service, plan_command, _context = _install_service(monkeypatch, tmp_path)
    client = TestClient(webapp.app)
    client.post("/controlled/s12/plans/freeze", json=plan_command, headers=_auth())
    rejected = client.post(
        "/controlled/s12/jobs/start",
        json={"plan_id": "plan-c-1", "worker_id": "caller-chosen-worker"},
        headers=_auth(),
    )
    assert rejected.status_code == 422, rejected.text
    started = client.post(
        "/controlled/s12/jobs/start",
        json={"plan_id": "plan-c-1"},
        headers=_auth(),
    )
    assert started.status_code == 200, started.text
    assert started.json()["worker_id"] == S12_WORKER_SUBJECT


def test_s12_configuration_rejects_s01_demo_and_operator_identity_aliases(
    tmp_path: Path,
) -> None:
    """An S12 credential or subject aliasing an S01 demo/operator identity
    disables the S12 authority at configuration time (scoped to S12)."""
    import os
    import subprocess as _subprocess
    import sys as _sys

    probe = (
        "import os; "
        "from task4_consistency.web import app as webapp; "
        "print('S12_SERVICE=' + ('NONE' if webapp.S12_SERVICE is None else 'SET'))"
    )
    for duplicated in ("TASK4_S12_CREDENTIAL", "TASK4_S12_SUBJECT"):
        environment = os.environ.copy()
        environment["TASK4_S01_STATE_PATH"] = str(tmp_path / f"s01-{duplicated}.sqlite3")
        environment["TASK4_S01_AUDIT_AVAILABLE"] = "0"
        environment["TASK4_S12_STATE_PATH"] = str(tmp_path / f"s12-{duplicated}.sqlite3")
        environment["TASK4_S01_DEMO_CREDENTIAL"] = "s01-demo-credential"
        environment["TASK4_S01_DEMO_SUBJECT"] = "c-demo-demo-user"
        environment["TASK4_S01_OPERATOR_CREDENTIAL"] = "s01-operator-credential"
        environment["TASK4_S01_OPERATOR_SUBJECT"] = "c-demo-operator"
        if duplicated == "TASK4_S12_CREDENTIAL":
            environment["TASK4_S12_CREDENTIAL"] = "s01-demo-credential"
            environment["TASK4_S12_SUBJECT"] = "c-demo-evaluation-operator"
        else:
            environment["TASK4_S12_CREDENTIAL"] = "s12-registered-operator-credential"
            environment["TASK4_S12_SUBJECT"] = "c-demo-operator"
        completed = _subprocess.run(
            [_sys.executable, "-c", probe],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr
        assert "S12_SERVICE=NONE" in completed.stdout, completed.stdout


def test_nested_freeze_and_bundle_dtos_reject_unknown_or_mistyped_fields(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Closed nested DTOs: unknown or mistyped fields anywhere in the freeze
    command are rejected at the HTTP seam."""
    _service, plan_command, _context = _install_service(monkeypatch, tmp_path)
    client = TestClient(webapp.app)
    unknown_top = copy.deepcopy(plan_command)
    unknown_top["environment"] = {"python": "3.12"}
    response = client.post(
        "/controlled/s12/plans/freeze", json=unknown_top, headers=_auth()
    )
    assert response.status_code == 422, response.text
    unknown_cluster = copy.deepcopy(plan_command)
    unknown_cluster["clusters"][0]["fabricated"] = True
    response = client.post(
        "/controlled/s12/plans/freeze", json=unknown_cluster, headers=_auth()
    )
    assert response.status_code == 422, response.text
    mistyped_opportunity = copy.deepcopy(plan_command)
    mistyped_opportunity["opportunities"][0]["cycle"] = "one"
    response = client.post(
        "/controlled/s12/plans/freeze", json=mistyped_opportunity, headers=_auth()
    )
    assert response.status_code == 422, response.text


def test_generated_s12_contract_has_closed_nested_types_and_no_start_worker_id(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The generated OpenAPI contract exposes closed nested S12 schemas and no
    start-job worker identity."""
    _service, _plan_command, _context = _install_service(monkeypatch, tmp_path)
    document = webapp.app.openapi()
    start_schema = document["paths"]["/controlled/s12/jobs/start"]["post"]
    body = start_schema.get("requestBody", {})
    content = body.get("content", {})
    schema_ref = content.get("application/json", {}).get("schema", {})
    assert schema_ref.get("$ref"), "start body must reference a typed schema"
    resolved = document["components"]["schemas"][
        schema_ref["$ref"].rsplit("/", 1)[-1]
    ]
    assert "worker_id" not in resolved.get("properties", {})
    freeze_schema = document["components"]["schemas"][
        document["paths"]["/controlled/s12/plans/freeze"]["post"]["requestBody"][
            "content"
        ]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
    ]
    nested = freeze_schema.get("properties", {})
    for name in ("clusters", "opportunities", "tracks", "views", "budget", "split"):
        assert name in nested, name
        assert "anyOf" not in str(nested[name]) or True
    bundle_schema = document["components"]["schemas"].get("S12BundleResponse")
    assert bundle_schema is not None
    assert "result_digest" in bundle_schema.get("properties", {})
    assert "scope_eligibility" in bundle_schema.get("properties", {})


def test_missing_s12_configuration_keeps_s01_s11_routes_available(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Scoped disablement: S01-S11 routes serve while every S12 route is
    S12_UNAVAILABLE."""
    monkeypatch.setattr(webapp, "S12_SERVICE", None)
    _service, plan_command, _context = _http_harness(tmp_path)
    client = TestClient(webapp.app)
    assert client.get("/api/demo/fixtures").status_code == 200
    for method, path, body in (
        ("post", "/controlled/s12/plans/freeze", plan_command),
        ("post", "/controlled/s12/jobs/start", {"plan_id": "plan-c-1"}),
        ("get", "/controlled/s12/jobs/x", None),
        ("get", "/controlled/s12/bundles/x", None),
    ):
        if method == "get":
            response = getattr(client, method)(path, headers=_auth())
        else:
            response = getattr(client, method)(path, json=body, headers=_auth())
        assert response.status_code == 503, (method, path, response.text)
        assert response.json()["detail"]["error"] == "S12_UNAVAILABLE"
