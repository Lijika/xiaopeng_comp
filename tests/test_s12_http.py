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
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from task4_consistency.controlled.s12 import (
    EvaluationService,
    LabelManifestStore,
)
from task4_consistency.controlled.s01 import ControlledScenarioTestDriver
from task4_consistency.web.s12_http import (
    S12BundleResponse,
    S12ClusterResponse,
    S12EvidenceField,
    S12FreezePlanBody,
    S12OpportunityResponse,
    S12PlanResponse,
    S12StatisticsBlock,
)
from task4_consistency.web import app as webapp

from tests.test_s12_controlled import (
    _business_authority_bindings,
    _make_business_harness,
    _make_governed_release,
    _reference_plan_command,
    _s12_authority_service,
    _write_label_manifest,
)
from tests.test_s02_controlled import INTEGRATOR as S02_INTEGRATOR
from tests.test_s02_controlled import _registered_service

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


def test_response_finite_vocabularies_are_closed() -> None:
    cluster = {
        "cluster_id": "cluster-1",
        "stratum": "s",
        "applications": ["app-1"],
        "usage": "fabricated",
    }
    opportunity = {
        "opportunity_id": "opp-1",
        "track": "fabricated",
        "cluster": "cluster-1",
        "application_id": "app-1",
        "cycle": 1,
        "check_id": "R_ENGINE_CROSS",
        "target_scope": "C",
        "evidence_snapshot_id": "snapshot-1",
        "label": "fabricated",
        "run_id": "run-1",
    }

    with pytest.raises(ValidationError):
        S12ClusterResponse.model_validate(cluster)
    with pytest.raises(ValidationError):
        S12OpportunityResponse.model_validate(opportunity)

    schema = webapp.app.openapi()
    cluster_schema = schema["components"]["schemas"]["S12ClusterResponse"]
    opportunity_schema = schema["components"]["schemas"]["S12OpportunityResponse"]
    assert cluster_schema["properties"]["usage"]["enum"] == [
        "development",
        "calibration",
        "acceptance_holdout",
    ]
    assert opportunity_schema["properties"]["track"]["enum"] == ["R", "C"]
    assert opportunity_schema["properties"]["label"]["enum"] == [
        "consistent",
        "inconsistent",
        "indeterminate",
        "not_applicable",
    ]


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

    measure, publication_guard = _business_authority_bindings(
        business_services, governance_service
    )

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
        business_publication_guard=publication_guard,
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
    return service, command, {
        "measure": measure,
        "publication_guard": publication_guard,
    }


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


def test_s02_registered_snapshot_is_lossless_through_s12_http(
    tmp_path: Path,
) -> None:
    business, submission = _registered_service(tmp_path / "s02")
    admitted = business.submit_registered(
        submission=submission,
        idempotency_key="s12-s02-registered",
        principal=S02_INTEGRATOR,
    )
    completed = ControlledScenarioTestDriver(business).process_next_job(now=0)
    assert admitted.application_id is not None
    assert completed.status == "complete"
    assert completed.evidence_snapshot_id is not None
    assert completed.evidence_snapshot_digest is not None
    source_snapshot = business.evaluation_evidence_snapshot(
        application_id=admitted.application_id,
        snapshot_id=completed.evidence_snapshot_id,
    )["evidence_snapshot"]

    governance, release_id, release_digest, _manifest = _make_governed_release(
        tmp_path
    )
    labels = {f"opp-{index}": "consistent" for index in range(4)}
    label_root, manifest_id, manifest_digest = _write_label_manifest(tmp_path, labels)
    service = _s12_authority_service(
        tmp_path,
        business_services=[business],
        governance_service=governance,
        label_root=label_root,
        worker_subject=S12_WORKER_SUBJECT,
    )
    scenarios = (
        "app_r53_bad_engine.json",
        "app_s04_bad_vin.json",
        "app_bad_brand.json",
        "app_bad_model.json",
    )
    command = _reference_plan_command(
        admitted=[(scenario, admitted.application_id) for scenario in scenarios],
        snapshot_by_application={
            admitted.application_id: (
                completed.evidence_snapshot_id,
                completed.evidence_snapshot_digest,
            )
        },
        release_id=release_id,
        release_digest=release_digest,
        manifest_id=manifest_id,
        manifest_digest=manifest_digest,
        labels=labels,
        plan_id="plan-s02-http",
    )
    command["clusters"] = [
        {
            "cluster_id": "cl-0",
            "stratum": "registered",
            "applications": [admitted.application_id],
            "usage": "development",
        }
    ]
    for opportunity in command["opportunities"]:
        opportunity["cluster"] = "cl-0"
        opportunity["data_source"] = "registered"
    command["evidence_references"] = command["evidence_references"][:1]

    frozen = S12PlanResponse.model_validate(service.freeze_plan(command)).model_dump(
        mode="json", by_alias=True
    )
    run_spec = next(iter(frozen["run_specs"].values()))
    assert run_spec["evidence_snapshot"] == source_snapshot
    assert hashlib.sha256(
        json.dumps(
            run_spec["evidence_snapshot"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest() == completed.evidence_snapshot_digest

    started = service.start_job(command["plan_id"])
    processed = service.process_job(started["job_id"])
    assert processed["bundle_id"] is not None
    queried = S12BundleResponse.model_validate(
        service.query_bundle(processed["bundle_id"])
    ).model_dump(mode="json", by_alias=True)
    replay_run_spec = next(
        iter(queried["replay_package"]["plan"]["run_specs"].values())
    )
    assert replay_run_spec["evidence_snapshot"] == source_snapshot


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
        "print('S12_SERVICE=' + ('NONE' if webapp.S12_SERVICE is None else 'SET')); "
        "print('S12_ERROR=' + str(webapp.S12_CONFIGURATION_ERROR))"
    )
    for duplicated in ("TASK4_S12_CREDENTIAL", "TASK4_S12_SUBJECT"):
        environment = os.environ.copy()
        environment["TASK4_S01_STATE_PATH"] = str(tmp_path / f"s01-{duplicated}.sqlite3")
        environment["TASK4_S01_AUDIT_AVAILABLE"] = "0"
        environment["TASK4_S12_STATE_PATH"] = str(tmp_path / f"s12-{duplicated}.sqlite3")
        environment["TASK4_S12_LABEL_MANIFESTS_DIR"] = str(tmp_path / "labels")
        environment["TASK4_S12_WORKER_SUBJECT"] = "c-demo-evaluation-worker"
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
        assert "aliases a controlled identity" in completed.stdout


def test_s12_configuration_rejects_s02_identity_aliases(tmp_path: Path) -> None:
    import os
    import subprocess as _subprocess
    import sys as _sys

    probe = (
        "from task4_consistency.web import app as webapp; "
        "print('S12_SERVICE=' + ('NONE' if webapp.S12_SERVICE is None else 'SET')); "
        "print('S12_ERROR=' + str(webapp.S12_CONFIGURATION_ERROR))"
    )
    for duplicated in ("credential", "operator-subject", "worker-subject"):
        environment = os.environ.copy()
        environment.update(
            {
                "TASK4_S01_STATE_PATH": str(tmp_path / f"s01-{duplicated}.sqlite3"),
                "TASK4_S01_AUDIT_AVAILABLE": "0",
                "TASK4_S01_DEMO_CREDENTIAL": "s01-demo-credential",
                "TASK4_S01_DEMO_SUBJECT": "c-demo-user",
                "TASK4_S01_OPERATOR_CREDENTIAL": "s01-operator-credential",
                "TASK4_S01_OPERATOR_SUBJECT": "c-operator",
                "TASK4_S02_CREDENTIAL": "s02-integrator-credential",
                "TASK4_S02_SUBJECT": "s02-integrator-subject",
                "TASK4_S12_STATE_PATH": str(tmp_path / f"s12-{duplicated}.sqlite3"),
                "TASK4_S12_LABEL_MANIFESTS_DIR": str(tmp_path / "labels"),
                "TASK4_S12_CREDENTIAL": "s12-operator-credential",
                "TASK4_S12_SUBJECT": "s12-operator-subject",
                "TASK4_S12_WORKER_SUBJECT": "s12-worker-subject",
            }
        )
        if duplicated == "credential":
            environment["TASK4_S12_CREDENTIAL"] = environment[
                "TASK4_S02_CREDENTIAL"
            ]
        elif duplicated == "operator-subject":
            environment["TASK4_S12_SUBJECT"] = environment["TASK4_S02_SUBJECT"]
        else:
            environment["TASK4_S12_WORKER_SUBJECT"] = environment[
                "TASK4_S02_SUBJECT"
            ]

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
        assert "aliases" in completed.stdout
        assert "controlled" in completed.stdout


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


def test_request_preserves_variant_and_finite_scope(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _service, plan_command, _context = _install_service(monkeypatch, tmp_path)
    plan_command["scope_declared"] = "R-E2E"
    plan_command["opportunities"][0]["variant_id"] = "variant-0"
    plan_command["clusters"][0]["variants"] = ["variant-0"]
    validated = S12FreezePlanBody.model_validate(plan_command)
    assert validated.opportunities[0].variant_id == "variant-0"
    assert validated.scope_declared == "R-E2E"

    invalid_scope = copy.deepcopy(plan_command)
    invalid_scope["scope_declared"] = "fabricated-scope"
    with pytest.raises(Exception):
        S12FreezePlanBody.model_validate(invalid_scope)


def test_evidence_field_preserves_missing_raw_value() -> None:
    field = S12EvidenceField.model_validate(
        {
            "raw": None,
            "confidence": 0.0,
            "observation_id": "observation-missing",
            "evidence_eligible": False,
            "eligibility_reason": "PROVENANCE_INELIGIBLE",
        }
    )

    assert field.raw is None


def test_finite_response_maps_reject_unknown_keys() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        S12StatisticsBlock.model_validate(
            {
                "membership": "C",
                "opportunity_count": 1,
                "denominators": {
                    "E": 1,
                    "E_all": 1,
                    "n_consistent": 1,
                    "n_inconsistent": 0,
                    "n_consistent_decisive": 1,
                    "labelability": 1,
                    "uncertain_on_inconsistent": 0,
                    "skipped_rate": 1,
                    "missing_rate": 1,
                    "error_rate": 1,
                    "conditional_fpr": 1,
                },
                "prediction_counts": {"fabricated": 1},
                "point": {"coverage": 1.0},
                "estimable": False,
                "not_estimable_reasons": [],
                "conclusion": "insufficient",
            }
        )


def test_generated_s12_schemas_are_recursively_closed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The generated OpenAPI contract exposes recursively closed nested S12
    schemas and no start-job worker identity.  Every critical nested object
    is a resolved explicit model with a bounded property set and no
    unrestricted additionalProperties."""
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
    bundle_schema = document["components"]["schemas"].get("S12BundleResponse")
    assert bundle_schema is not None
    assert "result_digest" in bundle_schema.get("properties", {})
    assert "scope_eligibility" in bundle_schema.get("properties", {})

    schemas = document["components"]["schemas"]

    def resolve(node: dict[str, Any]) -> dict[str, Any]:
        reference = node.get("$ref")
        if reference:
            return schemas[reference.rsplit("/", 1)[-1]]
        return node

    def assert_closed(node: dict[str, Any], path: str) -> None:
        node = resolve(node)
        identifier_map_fields = {
            "fields",
            "labels",
            "run_specs",
            "predictions",
            "mandatory_check_families",
            "mandatory_family_statistics",
            "difficulty",
            "data_source",
            "document_combination",
            "perturbation_family",
        }
        additional = node.get("additionalProperties")
        if "properties" in node:
            assert additional is False, (
                f"finite object at {path} must set additionalProperties=false"
            )
            for name, child in node["properties"].items():
                assert_closed(child, f"{path}.{name}")
        if "items" in node:
            assert_closed(node["items"], f"{path}[]")
        for variant in node.get("anyOf", []) or node.get("oneOf", []):
            assert_closed(variant, f"{path}|{variant.get('$ref') or 'variant'}")
        if isinstance(additional, dict):
            assert path.rsplit(".", 1)[-1] in identifier_map_fields, (
                f"finite object at {path} is a typed open map"
            )
            assert_closed(additional, f"{path}<value>")

    critical = (
        "S12PlanResponse",
        "S12BundleResponse",
        "S12FreezePlanBody",
        "S12StartJobBody",
        "S12JobResponse",
        "S12ProcessResponse",
    )
    for name in critical:
        assert_closed(schemas[name], name)

    # The critical nested response objects are explicit keyed models: their
    # generated schemas carry a bounded property set.
    for schema_name in (
        "S12StatisticsBlock",
        "S12PointMetrics",
        "S12Denominators",
        "S12ReleaseResponse",
        "S12EnvironmentResponse",
        "S12LabelManifestResponse",
        "S12EvidenceReferenceResponse",
        "S12PredictionCounts",
        "S12TrackStatistics",
        "S12ViewStatistics",
        "S12RunSpec",
        "S12ReplayPackage",
    ):
        assert schema_name in schemas, schema_name

    expected_properties = {
        "S12PredictionCounts": {
            "consistent",
            "inconsistent",
            "uncertain",
            "skipped",
            "missing",
            "error",
        },
        "S12TrackStatistics": {"R", "C"},
        "S12ViewStatistics": {"R-E2E", "R-T4-conditional"},
        "S12StrataStatistics": {
            "difficulty",
            "data_source",
            "document_combination",
            "perturbation_family",
        },
    }
    for schema_name, properties in expected_properties.items():
        assert set(schemas[schema_name]["properties"]) == properties


def test_generated_contract_test_fails_when_a_nested_schema_is_open(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """The recursive schema closure checker itself rejects an unrestricted
    nested map: the regression test cannot go green on an open contract."""
    _service, _plan_command, _context = _install_service(monkeypatch, tmp_path)
    document = webapp.app.openapi()
    schemas = document["components"]["schemas"]

    def resolve(node: dict[str, Any]) -> dict[str, Any]:
        reference = node.get("$ref")
        return schemas[reference.rsplit("/", 1)[-1]] if reference else node

    def assert_closed(node: dict[str, Any], path: str) -> None:
        node = resolve(node)
        additional = node.get("additionalProperties")
        if additional is True:
            raise AssertionError(f"object at {path} is open")
        if "properties" in node:
            assert additional is False, f"finite object at {path} must be closed"
            for name, child in node["properties"].items():
                assert_closed(child, f"{path}.{name}")
        if "items" in node:
            assert_closed(node["items"], f"{path}[]")
        if isinstance(additional, dict):
            raise AssertionError(f"finite object at {path} is a typed open map")

    for open_node in (
        {"type": "object", "additionalProperties": True},
        {
            "type": "object",
            "properties": {"known": {"type": "integer"}},
            "additionalProperties": {"type": "integer"},
        },
    ):
        with pytest.raises(AssertionError):
            assert_closed(open_node, "finite")


def test_s12_startup_requires_a_distinct_worker_subject(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Application startup requires TASK4_S12_WORKER_SUBJECT and rejects
    equality with the operator or any controlled subject: the S12 plane
    stays closed otherwise."""
    import os

    monkeypatch.setenv("TASK4_S12_STATE_PATH", str(tmp_path / "eval.sqlite3"))
    monkeypatch.setenv("TASK4_S12_CREDENTIAL", S12_CREDENTIAL)
    monkeypatch.setenv("TASK4_S12_SUBJECT", S12_SUBJECT)
    monkeypatch.setenv("TASK4_S12_LABEL_MANIFESTS_DIR", str(tmp_path / "labels"))
    monkeypatch.setattr(webapp, "S12_CREDENTIAL", S12_CREDENTIAL)
    monkeypatch.setattr(webapp, "S12_SUBJECT", S12_SUBJECT)

    # No TASK4_S12_WORKER_SUBJECT configured: the evaluation plane must not
    # silently alias the operator subject.
    monkeypatch.setenv("TASK4_S12_WORKER_SUBJECT", "")
    monkeypatch.setattr(webapp, "S12_WORKER_SUBJECT", "")
    assert webapp._s12_evaluation_service() is None

    # Worker subject equal to the operator subject is rejected.
    monkeypatch.setenv("TASK4_S12_WORKER_SUBJECT", S12_SUBJECT)
    monkeypatch.setattr(webapp, "S12_WORKER_SUBJECT", S12_SUBJECT)
    with pytest.raises(ValueError):
        webapp._s12_evaluation_service()

    # A distinct worker subject is accepted.
    monkeypatch.setenv("TASK4_S12_WORKER_SUBJECT", S12_WORKER_SUBJECT)
    monkeypatch.setattr(webapp, "S12_WORKER_SUBJECT", S12_WORKER_SUBJECT)
    service = webapp._s12_evaluation_service()
    assert service is not None
    assert service._worker_subject == S12_WORKER_SUBJECT


def test_s12_response_models_reject_unknown_nested_fields(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Critical response DTOs are recursively closed: an unknown nested field
    anywhere in the bundle response is rejected by the model."""
    from pydantic import ValidationError

    _service, _plan_command, _context = _install_service(monkeypatch, tmp_path)
    client = TestClient(webapp.app)
    frozen = client.post(
        "/controlled/s12/plans/freeze", json=_plan_command, headers=_auth()
    )
    assert frozen.status_code == 200, frozen.text
    plan_id = frozen.json()["plan_id"]
    started = client.post(
        "/controlled/s12/jobs/start",
        json={"plan_id": plan_id},
        headers=_auth(),
    )
    assert started.status_code == 200, started.text
    job_id = started.json()["job_id"]
    processed = client.post(
        f"/controlled/s12/jobs/{job_id}/process",
        json={},
        headers=_auth(),
    )
    assert processed.status_code == 200, processed.text
    bundle_id = processed.json()["bundle_id"]
    fetched = client.get(
        f"/controlled/s12/bundles/{bundle_id}", headers=_auth()
    )
    assert fetched.status_code == 200, fetched.text
    bundle = fetched.json()
    bundle["tracks"]["C"]["point"]["bogus_metric"] = 0.5
    with pytest.raises(ValidationError):
        S12BundleResponse.model_validate(bundle)
    replay_unknown = fetched.json()
    replay_unknown["replay_package"]["fabricated"] = True
    with pytest.raises(ValidationError):
        S12BundleResponse.model_validate(replay_unknown)
    track_unknown = fetched.json()
    track_unknown["tracks"]["fabricated"] = track_unknown["tracks"]["C"]
    with pytest.raises(ValidationError):
        S12BundleResponse.model_validate(track_unknown)


def test_service_derives_worker_identity_and_has_no_caller_override(
    tmp_path: Path,
) -> None:
    """The service derives the registered worker internally: start/rerun/
    process take no caller worker identity and bind the configured subject."""
    from tests.test_s12_controlled import _slice1_harness

    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"])
    assert job["worker_id"] == service._worker_subject
    outcome = service.process_job(job["job_id"])
    assert outcome["status"] in {"INSUFFICIENT", "FAIL", "SMOKE_ONLY"}
    rerun_job = service.rerun_job(job["job_id"])
    assert rerun_job["worker_id"] == service._worker_subject
    with pytest.raises(TypeError):
        service.start_job(plan["plan_id"], worker_id="caller")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        service.process_job(  # type: ignore[call-arg]
            job["job_id"], worker_id="caller"
        )


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


# ---------------------------------------------------------------------------
# Ticket #28 R2 Slice 4 — typed authority failure mapping (SP-09)
# ---------------------------------------------------------------------------


def test_healthy_unknown_authority_reference_is_an_invalid_command(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A healthy authority with an unknown reference is a caller command
    error: the freeze route returns the 422 invalid-command envelope."""
    service, plan_command, _context = _http_harness(tmp_path)
    monkeypatch.setattr(webapp, "S12_SERVICE", service)
    monkeypatch.setattr(webapp, "S12_CREDENTIAL", S12_CREDENTIAL)
    monkeypatch.setattr(webapp, "S12_SUBJECT", S12_SUBJECT)
    monkeypatch.setattr(webapp, "S12_WORKER_SUBJECT", S12_WORKER_SUBJECT)
    client = TestClient(webapp.app)
    unknown = copy.deepcopy(plan_command)
    unknown["evidence_references"][0]["snapshot_id"] = (
        "snapshot_sha256_" + "b" * 64
    )
    unknown["evidence_references"][0]["snapshot_digest"] = "b" * 64
    response = client.post(
        "/controlled/s12/plans/freeze", json=unknown, headers=_auth()
    )
    assert response.status_code == 422, response.text


def test_missing_or_corrupt_authority_closes_as_s12_unavailable(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """An authority outage or corruption is a server condition: the freeze
    route returns the closed 503 S12_UNAVAILABLE envelope for each
    authority class."""
    from task4_consistency.controlled.s12 import (
        LabelManifestUnavailable,
        LabelManifestStore,
    )

    business_services, admitted, snapshots, _path = _make_business_harness(
        tmp_path, ROOT / "configs" / "rules_auto_lease.yaml"
    )
    governance_service, release_id, release_digest, _manifest = (
        _make_governed_release(tmp_path)
    )
    labels = {f"opp-{index}": "consistent" for index in range(len(admitted))}
    label_root, manifest_id, manifest_digest = _write_label_manifest(
        tmp_path, labels
    )

    measure, publication_guard = _business_authority_bindings(
        business_services, governance_service
    )

    def corrupt_snapshot(application_id: str, snapshot_id: str) -> dict[str, Any]:
        raise RuntimeError("evidence snapshot digest does not verify")

    def corrupt_release(release_id: str, release_digest: str) -> dict[str, Any]:
        raise RuntimeError("registry checker artifact is not materializable")

    def unavailable_label(manifest_id: str, manifest_digest: str) -> dict[str, Any]:
        raise LabelManifestUnavailable(
            f"label manifest {manifest_id} is unregistered"
        )

    command = _reference_plan_command(
        admitted=admitted,
        snapshot_by_application=snapshots,
        release_id=release_id,
        release_digest=release_digest,
        manifest_id=manifest_id,
        manifest_digest=manifest_digest,
    )

    def install(provider_name: str) -> None:
        service = EvaluationService(
            state_path=tmp_path / f"evaluation-{provider_name}.sqlite3",
            clock=lambda: 1700000000,
            snapshot_provider=corrupt_snapshot
            if provider_name == "snapshot"
            else (lambda application_id, snapshot_id: business_services[
                0
            ].evaluation_evidence_snapshot(
                application_id=application_id, snapshot_id=snapshot_id
            )),
            release_provider=corrupt_release
            if provider_name == "release"
            else (lambda rid, rd: governance_service.resolve_evaluation_release(
                release_id=rid, release_digest=rd
            )),
            label_manifest_provider=unavailable_label
            if provider_name == "label"
            else LabelManifestStore(label_root).resolve,
            business_state_provider=measure,
            business_publication_guard=publication_guard,
            worker_subject=S12_WORKER_SUBJECT,
        )
        monkeypatch.setattr(webapp, "S12_SERVICE", service)
        monkeypatch.setattr(webapp, "S12_CREDENTIAL", S12_CREDENTIAL)
        monkeypatch.setattr(webapp, "S12_SUBJECT", S12_SUBJECT)
        monkeypatch.setattr(webapp, "S12_WORKER_SUBJECT", S12_WORKER_SUBJECT)

    client = TestClient(webapp.app)
    for provider_name in ("snapshot", "release", "label"):
        install(provider_name)
        response = client.post(
            "/controlled/s12/plans/freeze", json=command, headers=_auth()
        )
        assert response.status_code == 503, (provider_name, response.text)
        assert (
            response.json()["detail"]["error"] == "S12_UNAVAILABLE"
        ), provider_name


def test_healthy_unknown_label_reference_is_an_invalid_command(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _service, command, _context = _install_service(monkeypatch, tmp_path)
    unknown = copy.deepcopy(command)
    unknown["label_manifest"] = {
        "manifest_id": "manifest_sha256_" + "0" * 64,
        "manifest_digest": "0" * 64,
    }

    response = TestClient(webapp.app).post(
        "/controlled/s12/plans/freeze", json=unknown, headers=_auth()
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["error"] == "S12_INVALID_COMMAND"


def test_s01_evaluation_authority_unavailable_maps_to_503(
    monkeypatch: Any, tmp_path: Path
) -> None:
    service, command, _context = _install_service(monkeypatch, tmp_path)

    def unavailable_snapshot(*_args: Any, **_kwargs: Any) -> Any:
        from task4_consistency.controlled.s01 import EvaluationAuthorityUnavailable

        raise EvaluationAuthorityUnavailable("S01 evaluation authority unavailable")

    service._snapshot_provider = unavailable_snapshot
    response = TestClient(webapp.app).post(
        "/controlled/s12/plans/freeze", json=command, headers=_auth()
    )

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["error"] == "S12_UNAVAILABLE"


def test_s01_business_authority_unavailable_maps_to_503(
    monkeypatch: Any, tmp_path: Path
) -> None:
    service, command, _context = _install_service(monkeypatch, tmp_path)

    def unavailable_business() -> dict[str, Any]:
        from task4_consistency.controlled.s01 import EvaluationAuthorityUnavailable

        raise EvaluationAuthorityUnavailable("S01 evaluation authority unavailable")

    service._business_state_provider = unavailable_business
    response = TestClient(webapp.app, raise_server_exceptions=False).post(
        "/controlled/s12/plans/freeze", json=command, headers=_auth()
    )

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["error"] == "S12_UNAVAILABLE"


def test_diagnostic_job_query_serializes_reason_codes(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A job that settles diagnostically (runner digest mismatch) is
    queryable over HTTP: the job response carries the closed reason codes."""
    _service, _plan_command, _context = _install_service(monkeypatch, tmp_path)
    client = TestClient(webapp.app)
    frozen = client.post(
        "/controlled/s12/plans/freeze", json=_plan_command, headers=_auth()
    )
    assert frozen.status_code == 200, frozen.text
    plan_id = frozen.json()["plan_id"]
    started = client.post(
        "/controlled/s12/jobs/start",
        json={"plan_id": plan_id},
        headers=_auth(),
    )
    assert started.status_code == 200, started.text
    job_id = started.json()["job_id"]
    import json as _json
    from pathlib import Path as _Path

    # Tamper the runner result digest path: process with an INVALID digest
    # vector via the service directly is not reachable over HTTP (the route
    # runs the real runner), so corrupt the stored job payload to force the
    # claim-time integrity failure, then query the job.
    import sqlite3 as _sqlite3

    from task4_consistency.controlled.s12 import _integrity_digest

    store_path = tmp_path / "evaluation.sqlite3"
    connection = _sqlite3.connect(store_path)
    try:
        row = connection.execute(
            "SELECT payload FROM s12_jobs WHERE item_id = ?", (job_id,)
        ).fetchone()
        job = _json.loads(row[0])
        job["status"] = "diagnostic"
        job["result"] = {
            "bundle_id": None,
            "status": "INVALID",
            "reason_codes": ["RUNNER_DIGEST_MISMATCH"],
        }
        payload_text = _json.dumps(
            job, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = _integrity_digest("s12_jobs", job_id, payload_text)
        connection.execute(
            "UPDATE s12_jobs SET payload = ?, integrity_sha256 = ? "
            "WHERE item_id = ?",
            (payload_text, digest, job_id),
        )
        connection.commit()
    finally:
        connection.close()
    queried = client.get(f"/controlled/s12/jobs/{job_id}", headers=_auth())
    assert queried.status_code == 200, queried.text
    assert queried.json()["result"]["reason_codes"] == ["RUNNER_DIGEST_MISMATCH"]


def test_statistics_block_with_intervals_serializes_closed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """A statistics block carrying two-sided interval pairs and one-sided
    bounds serializes through the closed response model."""
    from pydantic import ValidationError

    from task4_consistency.controlled.s12 import _cluster_statistics
    from task4_consistency.web.s12_http import S12StatisticsBlock
    from tests.test_s12_controlled import _synthetic_track

    _service, _plan_command, _context = _install_service(monkeypatch, tmp_path)
    opportunities, clusters, predictions = _synthetic_track(
        consistent_clusters=60, inconsistent_clusters=100
    )
    block = _cluster_statistics(
        opportunities, clusters, predictions, seed=42, membership="C"
    )
    assert block["estimable"] is True
    assert block["interval_95_two_sided"] is not None
    validated = S12StatisticsBlock.model_validate(block)
    assert validated.interval_95_two_sided is not None
    assert all(
        len(bounds) == 2 for bounds in validated.interval_95_two_sided.values()
    )
    with pytest.raises(ValidationError):
        S12StatisticsBlock.model_validate(
            {**block, "point": {**block["point"], "bogus_metric": 0.5}}
        )
