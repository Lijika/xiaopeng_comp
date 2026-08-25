"""Ticket #50 / T16 — production FastAPI fixture and shell contracts.

The factory drives the released S14 lifecycle seams into one cancellable
Manual Review cycle with a registered in-flight manual-review effect in one
persisted SQLite authority.  The browser then reads those facts through the
real S14/S01/S13 public routes and the shared production React build.

The lifecycle commands are exercised only through their public routes; the
integrator cancellation identity (demo session) and the operator settlement
identity (registered control-plane credential) stay separate end to end.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from task4_consistency.controlled.s01 import (
    AdmissionDisposition,
    ControlledScenarioService,
    S01CommandPrincipal,
)

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"
FIXTURES = ROOT / "fixtures" / "applications"

T16_DEMO_SUBJECT = "t16-registered-integrator"
T16_DEMO_CREDENTIAL = "t16-integrator-credential"
T16_OPERATOR_SUBJECT = "t16-operator"
T16_OPERATOR_CREDENTIAL = "t16-operator-credential"

INTEGRATOR = S01CommandPrincipal(
    subject=T16_DEMO_SUBJECT,
    role="integrator",
    scope="C-DEMO",
    source_id="t16-fixture-intake",
)


def _demo_auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {T16_DEMO_CREDENTIAL}"}


def _operator_auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {T16_OPERATOR_CREDENTIAL}"}


def _service(work_root: Path) -> ControlledScenarioService:
    return ControlledScenarioService(
        fixture_root=FIXTURES,
        rules_path=RULES,
        state_path=work_root / "target.sqlite3",
    )


def _build_workflows(work_root: Path) -> ControlledScenarioService:
    """One cancellable Manual Review application with a registered in-flight
    manual-review work item plus a completable termination notification."""

    service = _service(work_root)
    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="t16-intake",
        principal=INTEGRATOR,
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    assert admitted.application_id is not None
    assert service.process_next_job().status == "complete"
    service.refresh_projection()
    application_id = str(admitted.application_id)
    route = service.current_route_view(
        principal=S01CommandPrincipal(
            subject=T16_DEMO_SUBJECT,
            role="reviewer",
            scope="C-DEMO",
            source_id="t16-fixture-review-console",
        ),
        application_id=application_id,
    )
    assert route["phase"] == "Manual Review"
    assert route["cycle"] == 1
    (work_root / "fixture.json").write_text(
        json.dumps(
            {
                "schema_version": "t16-browser-fixture/1",
                "application_id": application_id,
                "phase": route["phase"],
                "cycle": route["cycle"],
                "lifecycle_revision": route["lifecycle_revision"],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return service


def create_t16_react_test_app():
    """Return the real FastAPI app bound to one persisted T16 authority."""
    import task4_consistency.web.app as web

    work_root = Path(os.environ["TASK4_T16_FIXTURE_ROOT"])
    work_root.mkdir(parents=True, exist_ok=True)
    if (work_root / "fixture.json").is_file() and (
        work_root / "target.sqlite3"
    ).is_file():
        service = _service(work_root)
    else:
        service = _build_workflows(work_root)

    web.S01_BACKGROUND_ENABLED = False
    web.S01_REQUIRE_CONFIGURED_STARTUP = False
    web.S01_SERVICE = service
    web.S01_DEMO_CREDENTIAL = T16_DEMO_CREDENTIAL
    web.S01_DEMO_SUBJECT = T16_DEMO_SUBJECT
    web.S01_OPERATOR_CREDENTIAL = T16_OPERATOR_CREDENTIAL
    web.S01_OPERATOR_SUBJECT = T16_OPERATOR_SUBJECT
    # The operator-context authoritative read seam is the released S13
    # delivery view; it validates against the S13-registered credential.
    web.S13_OPERATOR_CREDENTIAL = T16_OPERATOR_CREDENTIAL
    web.S13_OPERATOR_SUBJECT = T16_OPERATOR_SUBJECT
    web.S13_OPERATOR_SCOPE = "C-DEMO"
    react_dir = os.environ.get("TASK4_T16_REACT_DIR", "").strip()
    web.S01_REACT_INDEX = (
        Path(react_dir).resolve() / "index.html"
        if react_dir
        else web.S01_REACT_STATIC / "index.html"
    )
    return web.app


def _install(monkeypatch: Any, tmp_path: Path) -> ControlledScenarioService:
    import task4_consistency.web.app as web

    service = _service(tmp_path)
    monkeypatch.setattr(web, "S01_SERVICE", service)
    monkeypatch.setattr(web, "S01_DEMO_CREDENTIAL", T16_DEMO_CREDENTIAL)
    monkeypatch.setattr(web, "S01_DEMO_SUBJECT", T16_DEMO_SUBJECT)
    monkeypatch.setattr(web, "S01_OPERATOR_CREDENTIAL", T16_OPERATOR_CREDENTIAL)
    monkeypatch.setattr(web, "S01_OPERATOR_SUBJECT", T16_OPERATOR_SUBJECT)
    return service


def _login(client: TestClient) -> None:
    response = client.post("/controlled/s01/api/session", headers=_demo_auth())
    assert response.status_code == 204, response.text


# --------------------------------------------------------------------------
# T16 React shells: additive surfaces with closed auth/no-store/build gates
# --------------------------------------------------------------------------


def test_t16_cancellation_shell_requires_registered_identity(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path)
    client = TestClient(webapp_of())
    application_id = "app_whatever"

    anonymous = client.get(f"/controlled/s14?application={application_id}")
    assert anonymous.status_code == 403

    issued = client.get(
        f"/controlled/s14/react?application={application_id}",
        headers=_demo_auth(),
    )
    assert issued.status_code == 200, issued.text
    assert issued.headers["cache-control"] == "no-store"


def test_t16_settlement_shell_requires_operator_identity(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path)
    client = TestClient(webapp_of())

    denied = client.get("/controlled/s14/settlement")
    assert denied.status_code == 403

    # The demo session alone is never settlement authority.
    _login(client)
    session_only = client.get("/controlled/s14/settlement/react")
    assert session_only.status_code == 403

    granted = client.get(
        "/controlled/s14/settlement", headers=_operator_auth()
    )
    assert granted.status_code == 200, granted.text
    assert granted.headers["cache-control"] == "no-store"


def test_t16_shells_fail_closed_without_production_build(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import task4_consistency.web.app as web

    _install(monkeypatch, tmp_path)
    missing = tmp_path / "missing-react-build"
    missing.mkdir()
    monkeypatch.setattr(web, "S01_REACT_INDEX", missing / "index.html")
    client = TestClient(webapp_of())

    for path in ("/controlled/s14", "/controlled/s14/settlement"):
        broken = client.get(path, headers=_operator_auth())
        assert broken.status_code == 503
        assert broken.json()["detail"]["error"] == "S14_REACT_UNAVAILABLE"


# --------------------------------------------------------------------------
# Full vertical lifecycle through the public routes only
# --------------------------------------------------------------------------


def test_t16_full_cancel_settle_reopen_path_through_public_routes(
    monkeypatch: Any, tmp_path: Path
) -> None:
    service = _install(monkeypatch, tmp_path)
    client = TestClient(webapp_of())
    _login(client)
    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="t16-lifecycle-intake",
        principal=INTEGRATOR,
    )
    assert admitted.application_id is not None
    assert service.process_next_job().status == "complete"
    service.refresh_projection()
    application_id = str(admitted.application_id)

    current = client.get(
        f"/controlled/s01/api/queries/applications/{application_id}/current-route"
    )
    assert current.status_code == 200, current.text
    revision = int(current.json()["lifecycle_revision"])
    assert current.json()["phase"] == "Manual Review"
    assert current.json()["cycle"] == 1

    # A bearer-only operator context (no session cookie, exactly like the
    # operator browser context) can neither read the reviewer query seam nor
    # cancel; the integrator session can never settle.
    operator_client = TestClient(webapp_of())
    operator_read = operator_client.get(
        f"/controlled/s01/api/queries/applications/{application_id}/current-route",
        headers=_operator_auth(),
    )
    assert operator_read.status_code == 404
    forbidden_cancel = operator_client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/cancel",
        json={
            "expected_lifecycle_revision": revision,
            "idempotency_key": "t16-operator-cancel",
            "reason_code": "UPSTREAM_WITHDRAWN",
        },
        headers=_operator_auth(),
    )
    assert forbidden_cancel.status_code == 403
    assert forbidden_cancel.json()["reason_code"] == "S14_FORBIDDEN"

    cancel = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/cancel",
        json={
            "expected_lifecycle_revision": revision,
            "idempotency_key": "t16-cancel-1",
            "reason_code": "UPSTREAM_WITHDRAWN",
        },
    )
    assert cancel.status_code == 200, cancel.text
    body = cancel.json()
    assert body["status"] == "accepted"
    assert body["phase"] == "Terminating"
    terminating_revision = int(body["lifecycle_revision"])

    terminating = client.get(
        f"/controlled/s01/api/queries/applications/{application_id}/current-route"
    )
    assert terminating.json()["phase"] == "Terminating"

    duplicate = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/cancel",
        json={
            "expected_lifecycle_revision": revision,
            "idempotency_key": "t16-cancel-1",
            "reason_code": "UPSTREAM_WITHDRAWN",
        },
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "replayed"

    stale = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/settle-termination",
        json={
            "expected_lifecycle_revision": revision,
            "idempotency_key": "t16-settle-stale",
        },
        headers=_operator_auth(),
    )
    assert stale.status_code == 409
    assert stale.json()["status"] == "stale"

    armed = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/settle-termination",
        json={
            "expected_lifecycle_revision": terminating_revision,
            "idempotency_key": "t16-settle-arm",
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
            "expected_lifecycle_revision": terminating_revision,
            "idempotency_key": "t16-settle-seal",
        },
        headers=_operator_auth(),
    )
    assert settled.status_code == 200, settled.text
    settled_body = settled.json()
    assert settled_body["status"] == "terminated"
    terminated_revision = int(settled_body["lifecycle_revision"])

    terminated_route = client.get(
        f"/controlled/s01/api/queries/applications/{application_id}/current-route"
    )
    assert terminated_route.json()["phase"] == "Terminated"

    granted = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/grant-reopen-permission",
        json={
            "expected_lifecycle_revision": terminated_revision,
            "approver_subject": "t16-independent-approver",
            "permission_id": "t16-institutional-permission/1",
            "idempotency_key": "t16-grant-1",
        },
        headers=_operator_auth(),
    )
    assert granted.status_code == 200, granted.text
    granted_body = granted.json()
    assert granted_body["status"] == "accepted"
    # The permission result carries the server-owned pinned artifact digest
    # the successor reopen must bind; the UI never invents it.
    release_digest = str(granted_body["artifact_release_digest"])
    assert len(release_digest) == 64

    reopened = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/reopen",
        json={
            "expected_lifecycle_revision": terminated_revision,
            "idempotency_key": "t16-reopen-1",
            "target_phase": "Intake",
            "reopen_policy": {
                "permission_id": "t16-institutional-permission/1",
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

    reopened_route = client.get(
        f"/controlled/s01/api/queries/applications/{application_id}/current-route"
    )
    assert reopened_route.json()["cycle"] == 2
    assert reopened_route.json()["phase"] == "Intake"

    history = client.get(
        f"/controlled/s01/api/queries/applications/{application_id}/history"
    )
    assert history.status_code == 200, history.text
    cycles = {int(run["cycle"]) for run in history.json()["runs"]}
    assert 1 in cycles


def webapp_of():
    import task4_consistency.web.app as web

    return web.app
