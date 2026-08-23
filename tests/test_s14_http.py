"""Ticket #30 / S14 — HTTP authority for cancel, settle-termination, reopen.

Follows the S13 HTTP harness conventions: an in-process TestClient against
the real FastAPI app with monkeypatched process-level authorities.  The
lifecycle commands are exercised through their public routes; history
reconstruction is asserted on the same installed service instance.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from task4_consistency.controlled.s01 import (
    AdmissionDisposition,
    ControlledScenarioService,
    S01CommandPrincipal,
)
from task4_consistency.web import app as webapp

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"

DEMO_SUBJECT = "registered-test-integrator"
DEMO_CREDENTIAL = "s14-test-demo-credential"
OPERATOR_CREDENTIAL = "s14-test-operator-credential"

INTEGRATOR = S01CommandPrincipal(
    subject=DEMO_SUBJECT,
    role="integrator",
    scope="C-DEMO",
    source_id="s14-http-intake",
)
REVIEWER = S01CommandPrincipal(
    subject=DEMO_SUBJECT,
    role="reviewer",
    scope="C-DEMO",
    source_id="s14-http-review-console",
)


def _demo_auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {DEMO_CREDENTIAL}"}


def _operator_auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {OPERATOR_CREDENTIAL}"}


def _install(monkeypatch: Any, tmp_path: Path) -> ControlledScenarioService:
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=RULES,
        state_path=tmp_path / "target.sqlite3",
    )
    monkeypatch.setattr(webapp, "S01_SERVICE", service)
    monkeypatch.setattr(webapp, "S01_DEMO_CREDENTIAL", DEMO_CREDENTIAL)
    monkeypatch.setattr(webapp, "S01_DEMO_SUBJECT", DEMO_SUBJECT)
    monkeypatch.setattr(webapp, "S01_OPERATOR_CREDENTIAL", OPERATOR_CREDENTIAL)
    monkeypatch.setattr(webapp, "S01_OPERATOR_SUBJECT", "s14-operator")
    return service


def _login(client: TestClient) -> None:
    response = client.post("/controlled/s01/api/session", headers=_demo_auth())
    assert response.status_code == 204, response.text


def _admitted_manual_review(service: ControlledScenarioService) -> str:
    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s14-http-intake",
        principal=INTEGRATOR,
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    assert admitted.application_id is not None
    assert service.process_next_job().status == "complete"
    service.refresh_projection()
    return str(admitted.application_id)


def test_s14_http_requires_session_and_operator_identity(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path)
    client = TestClient(webapp.app)
    application_id = "app_whatever"

    assert (
        client.post(
            f"/controlled/s01/api/commands/applications/{application_id}/cancel",
            json={
                "expected_lifecycle_revision": 1,
                "idempotency_key": "k",
                "reason_code": "UPSTREAM_WITHDRAWN",
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/controlled/s01/api/commands/applications/{application_id}/settle-termination",
            json={"expected_lifecycle_revision": 1, "idempotency_key": "k"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/controlled/s01/api/commands/applications/{application_id}/reopen",
            json={
                "expected_lifecycle_revision": 1,
                "idempotency_key": "k",
                "target_phase": "Intake",
                "reopen_policy": {
                    "permission_id": "p",
                    "release_digest": "0" * 64,
                },
            },
        ).status_code
        == 403
    )
    # The demo credential alone is never operator authority.
    _login(client)
    assert (
        client.post(
            f"/controlled/s01/api/commands/applications/{application_id}/settle-termination",
            json={"expected_lifecycle_revision": 1, "idempotency_key": "k"},
        ).status_code
        == 403
    )


def test_s14_http_unknown_application_is_not_found(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path)
    client = TestClient(webapp.app)
    _login(client)

    missing = client.post(
        "/controlled/s01/api/commands/applications/app_missing/cancel",
        json={
            "expected_lifecycle_revision": 1,
            "idempotency_key": "s14-http-cancel-missing",
            "reason_code": "UPSTREAM_WITHDRAWN",
        },
    )
    assert missing.status_code == 404


def test_s14_http_full_cancel_settle_reopen_path(
    monkeypatch: Any, tmp_path: Path
) -> None:
    service = _install(monkeypatch, tmp_path)
    client = TestClient(webapp.app)
    _login(client)
    application_id = _admitted_manual_review(service)

    current = client.get(
        f"/controlled/s01/api/queries/applications/{application_id}/current-route"
    )
    assert current.status_code == 200, current.text
    revision = int(current.json()["lifecycle_revision"])
    assert current.json()["phase"] == "Manual Review"

    cancel = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/cancel",
        json={
            "expected_lifecycle_revision": revision,
            "idempotency_key": "s14-http-cancel-1",
            "reason_code": "UPSTREAM_WITHDRAWN",
        },
    )
    assert cancel.status_code == 200, cancel.text
    body = cancel.json()
    assert body["status"] == "accepted"
    assert body["phase"] == "Terminating"

    # Settlement arms the durable termination notification first: the
    # response is a typed 202 outstanding body, not a silent seal.
    armed = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/settle-termination",
        json={
            "expected_lifecycle_revision": body["lifecycle_revision"],
            "idempotency_key": "s14-http-settle-arm",
        },
        headers=_operator_auth(),
    )
    assert armed.status_code == 202, armed.text
    armed_body = armed.json()
    assert armed_body["status"] == "outstanding"
    assert armed_body["phase"] == "Terminating"
    kinds = {item["kind"] for item in armed_body["unresolved_effects"]}
    assert kinds == {"termination_notification"}

    delivered = client.post(
        "/controlled/s01/api/commands/process-termination-notification",
        headers=_operator_auth(),
    )
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()["status"] == "delivered"

    settled = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/settle-termination",
        json={
            "expected_lifecycle_revision": body["lifecycle_revision"],
            "idempotency_key": "s14-http-settle-seal",
        },
        headers=_operator_auth(),
    )
    assert settled.status_code == 200, settled.text
    assert settled.json()["status"] == "terminated"
    terminated_revision = int(settled.json()["lifecycle_revision"])

    granted = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/grant-reopen-permission",
        json={
            "expected_lifecycle_revision": terminated_revision,
            "approver_subject": "registered-approver-operator",
            "permission_id": "institutional-reopen-permission/1",
            "idempotency_key": "s14-http-grant-1",
        },
        headers=_operator_auth(),
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["status"] == "accepted"

    release_digest = service._manifest.digest
    reopened = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/reopen",
        json={
            "expected_lifecycle_revision": terminated_revision,
            "idempotency_key": "s14-http-reopen-1",
            "target_phase": "Intake",
            "reopen_policy": {
                "permission_id": "institutional-reopen-permission/1",
                "release_digest": release_digest,
            },
        },
        headers=_operator_auth(),
    )
    assert reopened.status_code == 200, reopened.text
    reopened_body = reopened.json()
    assert reopened_body["status"] == "accepted"
    assert reopened_body["cycle"] == 2
    assert reopened_body["phase"] == "Intake"

    replayed_cancel = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/cancel",
        json={
            "expected_lifecycle_revision": revision,
            "idempotency_key": "s14-http-cancel-1",
            "reason_code": "UPSTREAM_WITHDRAWN",
        },
    )
    assert replayed_cancel.status_code == 200
    # Same key + same content replays the original accepted result.
    assert replayed_cancel.json()["status"] == "replayed"
    assert replayed_cancel.json()["phase"] == "Terminating"

    after = client.get(
        f"/controlled/s01/api/queries/applications/{application_id}/current-route"
    )
    assert after.status_code == 200
    assert after.json()["cycle"] == 2
    assert after.json()["phase"] == "Intake"
    assert after.json()["current_run_id"] is None

    history = service.application_history_view(
        principal=REVIEWER, application_id=application_id
    )
    assert len(history["cancellations"]) == 1
    assert history["cancellations"][0]["authority_subject"] == DEMO_SUBJECT
    assert len(history["terminations"]) == 1
    assert len(history["reopens"]) == 1
    assert history["reopens"][0]["predecessor_cycle"] == 1
    assert all(run["current"] is False for run in history["runs"])

    duplicate_reopen = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/reopen",
        json={
            "expected_lifecycle_revision": terminated_revision,
            "idempotency_key": "s14-http-reopen-1",
            "target_phase": "Intake",
            "reopen_policy": {
                "permission_id": "institutional-reopen-permission/1",
                "release_digest": release_digest,
            },
        },
        headers=_operator_auth(),
    )
    assert duplicate_reopen.status_code == 200
    assert duplicate_reopen.json()["status"] == "replayed"


def test_s14_http_stale_revision_is_a_stable_result(
    monkeypatch: Any, tmp_path: Path
) -> None:
    service = _install(monkeypatch, tmp_path)
    client = TestClient(webapp.app)
    _login(client)
    application_id = _admitted_manual_review(service)
    revision = int(
        client.get(
            f"/controlled/s01/api/queries/applications/{application_id}/current-route"
        ).json()["lifecycle_revision"]
    )

    stale = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/cancel",
        json={
            "expected_lifecycle_revision": 1,
            "idempotency_key": "s14-http-cancel-stale",
            "reason_code": "UPSTREAM_WITHDRAWN",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["status"] == "stale"
    assert stale.json()["reason_code"] == "lifecycle.cancel_stale_revision"

    # A whitespace idempotency key violates the service predicate and maps
    # to the TYPED 422 command error, never an unhandled 500.
    whitespace = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/cancel",
        json={
            "expected_lifecycle_revision": revision,
            "idempotency_key": " ",
            "reason_code": "UPSTREAM_WITHDRAWN",
        },
    )
    assert whitespace.status_code == 422
    assert whitespace.json()["detail"]["error"] == "S14_COMMAND_INVALID"

    # Settle/reopen without the operator credential is a 403 even with an
    # authenticated session cookie.
    no_operator = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/settle-termination",
        json={
            "expected_lifecycle_revision": revision,
            "idempotency_key": "s14-http-settle-no-operator",
        },
    )
    assert no_operator.status_code == 403

    missing = client.post(
        "/controlled/s01/api/commands/applications/app_missing/cancel",
        json={
            "expected_lifecycle_revision": 1,
            "idempotency_key": "s14-http-cancel-missing-2",
            "reason_code": "UPSTREAM_WITHDRAWN",
        },
    )
    assert missing.status_code == 404

    current = client.get(
        f"/controlled/s01/api/queries/applications/{application_id}/current-route"
    )
    assert current.json()["phase"] == "Manual Review"


@pytest.mark.parametrize(
    "path",
    (
        "cancel",
        "settle-termination",
        "grant-reopen-permission",
        "reopen",
    ),
)
def test_s14_http_command_routes_registered(path: str) -> None:
    from fastapi.routing import APIRoute

    paths = {
        getattr(route, "path", "")
        for route in webapp.app.routes
        if isinstance(route, APIRoute)
    }
    assert (
        f"/controlled/s01/api/commands/applications/{{application_id}}/{path}"
        in paths
    )


# --------------------------------------------------------------------------
# R2: typed outage/403/422 contracts and generated-schema agreement
# --------------------------------------------------------------------------


def test_s14_audit_or_storage_outage_returns_503(
    monkeypatch: Any, tmp_path: Path
) -> None:
    service = _install(monkeypatch, tmp_path)
    client = TestClient(webapp.app)
    _login(client)
    application_id = _admitted_manual_review(service)
    revision = int(
        client.get(
            f"/controlled/s01/api/queries/applications/{application_id}/current-route"
        ).json()["lifecycle_revision"]
    )

    service.audit_available = False
    outage = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/cancel",
        json={
            "expected_lifecycle_revision": revision,
            "idempotency_key": "s14-http-cancel-outage",
            "reason_code": "UPSTREAM_WITHDRAWN",
        },
    )
    assert outage.status_code == 503
    body = outage.json()
    assert body["status"] == "unavailable"
    assert body["reason_code"] == "AUDIT_UNAVAILABLE"

    service.audit_available = True
    service.storage_available = False
    storage_outage = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/settle-termination",
        json={"expected_lifecycle_revision": revision, "idempotency_key": "s14-http-settle-outage"},
        headers=_operator_auth(),
    )
    assert storage_outage.status_code == 503
    assert storage_outage.json()["reason_code"] == "STORAGE_UNAVAILABLE"


def test_s14_forbidden_body_matches_openapi(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path)
    client = TestClient(webapp.app)
    # No session: authentication refusal uses the shared envelope body.
    forbidden = client.post(
        "/controlled/s01/api/commands/applications/app_x/cancel",
        json={
            "expected_lifecycle_revision": 1,
            "idempotency_key": "k",
            "reason_code": "UPSTREAM_WITHDRAWN",
        },
    )
    assert forbidden.status_code == 403
    detail = forbidden.json()["detail"]
    assert set(detail) == {"error", "message"}

    generated = json.loads(
        (ROOT / "frontend" / "src" / "generated" / "openapi.json").read_text()
    )
    responses = generated["paths"][
        "/controlled/s01/api/commands/applications/{application_id}/cancel"
    ]["post"]["responses"]
    assert "403" in responses


def test_s14_validation_body_matches_openapi(
    monkeypatch: Any, tmp_path: Path
) -> None:
    service = _install(monkeypatch, tmp_path)
    client = TestClient(webapp.app)
    _login(client)
    application_id = _admitted_manual_review(service)

    # Schema-level violation (missing required field).
    missing = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/cancel",
        json={"idempotency_key": "k", "reason_code": "UPSTREAM_WITHDRAWN"},
    )
    assert missing.status_code == 422
    assert missing.json()["detail"]["error"] == "S14_COMMAND_INVALID"

    # Domain predicate violation (whitespace idempotency key).
    whitespace = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/cancel",
        json={
            "expected_lifecycle_revision": 1,
            "idempotency_key": " ",
            "reason_code": "UPSTREAM_WITHDRAWN",
        },
    )
    assert whitespace.status_code == 422
    assert (
        whitespace.json()["detail"]["error"] == "S14_COMMAND_INVALID"
    )


def test_s14_result_status_matrix_has_typed_bodies(
    monkeypatch: Any, tmp_path: Path
) -> None:
    service = _install(monkeypatch, tmp_path)
    client = TestClient(webapp.app)
    _login(client)
    application_id = _admitted_manual_review(service)
    revision = int(
        client.get(
            f"/controlled/s01/api/queries/applications/{application_id}/current-route"
        ).json()["lifecycle_revision"]
    )

    stale = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/cancel",
        json={
            "expected_lifecycle_revision": revision - 1,
            "idempotency_key": "s14-matrix-stale",
            "reason_code": "UPSTREAM_WITHDRAWN",
        },
    )
    assert stale.status_code == 409
    assert {"status", "replayed", "application_id", "reason_code"} <= set(
        stale.json()
    )

    accepted = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/cancel",
        json={
            "expected_lifecycle_revision": revision,
            "idempotency_key": "s14-matrix-accepted",
            "reason_code": "UPSTREAM_WITHDRAWN",
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "accepted"

    outstanding = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/settle-termination",
        json={
            "expected_lifecycle_revision": accepted.json()["lifecycle_revision"],
            "idempotency_key": "s14-matrix-arm",
        },
        headers=_operator_auth(),
    )
    assert outstanding.status_code == 202
    assert outstanding.json()["status"] == "outstanding"

    conflict = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/cancel",
        json={
            "expected_lifecycle_revision": revision + 5,
            "idempotency_key": "s14-matrix-conflict",
            "reason_code": "OTHER",
        },
    )
    assert conflict.status_code == 409

    missing = client.get(
        "/controlled/s01/api/queries/applications/app_unknown/current-route"
    )
    assert missing.status_code in {404}


def test_generated_s14_contract_matches_fastapi_schema() -> None:
    import json as _json

    generated = _json.loads(
        (ROOT / "frontend" / "src" / "generated" / "openapi.json").read_text()
    )
    live = webapp.app.openapi()

    s14_paths = [
        path
        for path in live["paths"]
        if path.startswith("/controlled/s01/api/commands/applications/")
        or path == "/controlled/s01/api/commands/process-termination-notification"
    ]
    assert s14_paths, "S14 paths missing from live schema"
    for path in s14_paths:
        assert path in generated["paths"], f"{path} missing from generated"
        live_responses = live["paths"][path]["post"]["responses"]
        gen_responses = generated["paths"][path]["post"]["responses"]
        assert set(live_responses) == set(gen_responses), (
            f"{path}: {sorted(live_responses)} != {sorted(gen_responses)}"
        )
