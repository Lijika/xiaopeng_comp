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
T16_S13_OPERATOR_CREDENTIAL = "t16-s13-delivery-operator-credential"

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
    """Two applications over one persisted authority:

    - ``active``: a cancellable Manual Review application (registered
      in-flight work) for the interactive cancel -> settle -> reopen path;
    - ``late``: a sealed cycle with an immutable late-input receipt
      demanding reopen, for the cycle-scoped read-only history assertions.
    """

    from tests.test_s14_controlled import (
        OPERATOR as S14_OPERATOR,
        SUPPLEMENT_INTEGRATOR,
        _attachment_submission,
        _grant_exact_permission,
        _ready_supplement_request,
        _reopen_policy,
        _settle_to_terminated,
    )

    late_service, reviewer, _intake, request, source = _ready_supplement_request(
        work_root
    )
    late_application_id = str(request["application_id"])
    late_route = late_service.current_route_view(
        principal=reviewer, application_id=late_application_id
    )
    assert late_route["cycle"] == 1
    cancel = late_service.cancel_application(
        application_id=late_application_id,
        principal=S01CommandPrincipal(
            subject=reviewer.subject,
            role="integrator",
            scope="C-DEMO",
            source_id="s14-demo-intake",
        ),
        expected_lifecycle_revision=int(late_route["lifecycle_revision"]),
        idempotency_key="t16-fixture-cancel",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    assert cancel["status"] == "accepted"
    settled = _settle_to_terminated(
        late_service, late_application_id, cancel["lifecycle_revision"]
    )
    late = late_service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="t16-fixture-late-upload",
        principal=SUPPLEMENT_INTEGRATOR,
        now=600,
    )
    assert late.reason_code == "evidence.late_input_requires_reopen"
    _grant_exact_permission(late_service, late_application_id, viewer=reviewer)
    reopened = late_service.reopen_application(
        application_id=late_application_id,
        principal=S14_OPERATOR,
        expected_lifecycle_revision=settled["lifecycle_revision"],
        idempotency_key="t16-fixture-late-reopen",
        target_phase="Intake",
        reopen_policy=_reopen_policy(late_service),
    )
    assert reopened["status"] == "accepted"

    # The cancellable application shares the same persisted store through a
    # sibling scenario-bound service instance.
    active_service = ControlledScenarioService(
        fixture_root=FIXTURES,
        rules_path=RULES,
        state_path=work_root / "target.sqlite3",
        scenario_id="app_r53_bad_engine.json",
    )
    admitted = active_service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="t16-fixture-active-intake",
        principal=S01CommandPrincipal(
            subject=reviewer.subject,
            role="integrator",
            scope="C-DEMO",
            source_id="t16-fixture-intake",
        ),
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    assert admitted.application_id is not None
    assert active_service.process_next_job().status == "complete"
    active_route = active_service.current_route_view(
        principal=reviewer, application_id=str(admitted.application_id)
    )
    assert active_route["phase"] == "Manual Review"
    (work_root / "fixture.json").write_text(
        json.dumps(
            {
                "schema_version": "t16-browser-fixture/1",
                "active_application_id": str(admitted.application_id),
                "late_application_id": late_application_id,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return active_service


def create_t16_react_test_app():
    """Return the real FastAPI app bound to one persisted T16 authority."""
    import task4_consistency.web.app as web

    work_root = Path(os.environ["TASK4_T16_FIXTURE_ROOT"])
    work_root.mkdir(parents=True, exist_ok=True)
    service = _build_workflows(work_root)

    web.S01_BACKGROUND_ENABLED = False
    web.S01_REQUIRE_CONFIGURED_STARTUP = False
    web.S01_SERVICE = service
    web.S01_DEMO_CREDENTIAL = T16_DEMO_CREDENTIAL
    # The admitted authority subject is the fixture reviewer (the upstream
    # canceller); the demo session must present exactly that subject for the
    # S14 cancel authorization while keeping the credential distinct.
    web.S01_DEMO_SUBJECT = "s14-reviewer"
    web.S01_OPERATOR_CREDENTIAL = T16_OPERATOR_CREDENTIAL
    web.S01_OPERATOR_SUBJECT = T16_OPERATOR_SUBJECT
    # The operator-context authoritative read seam is the released S13
    # delivery view, which owns a distinct registered credential.  The
    # controlled same-operator mapping lets the S01 control-plane credential
    # read it while the S13 credential stays separately configured.
    web.S13_OPERATOR_CREDENTIAL = T16_S13_OPERATOR_CREDENTIAL
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
    monkeypatch.setattr(
        web, "S13_OPERATOR_CREDENTIAL", T16_S13_OPERATOR_CREDENTIAL
    )
    monkeypatch.setattr(web, "S13_OPERATOR_SUBJECT", T16_OPERATOR_SUBJECT)
    return service


def _admit_manual_review(
    service: ControlledScenarioService,
    *,
    scenario_id: str,
    idempotency_key: str,
) -> str:
    admitted = service.submit_demo(
        scenario_id=scenario_id,
        idempotency_key=idempotency_key,
        principal=INTEGRATOR,
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    assert admitted.application_id is not None
    assert service.process_next_job().status == "complete"
    service.refresh_projection()
    return str(admitted.application_id)


def _operator_headers(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


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

    view_operation_id = str(
        client.get(
            f"/controlled/s01/api/queries/applications/{application_id}/settlement",
            headers=_operator_auth(),
        ).json()["pending_notification"]["operation_id"]
    )
    delivered = client.post(
        "/controlled/s01/api/commands/process-termination-notification",
        json={
            "application_id": application_id,
            "cycle": 1,
            "operation_id": view_operation_id,
        },
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


# --------------------------------------------------------------------------
# R1 fixes
# --------------------------------------------------------------------------


def test_t16_shell_error_contract_declared_and_fail_closed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path)
    client = TestClient(webapp_of())

    denied = client.get("/controlled/s14")
    assert denied.status_code == 403
    assert denied.json()["detail"]["error"] == "S01_FORBIDDEN"
    settlement_denied = client.get("/controlled/s14/settlement")
    assert settlement_denied.status_code == 403
    assert settlement_denied.json()["detail"]["error"] == "S01_FORBIDDEN"

    live = webapp_of().openapi()
    shell_paths = (
        "/controlled/s14",
        "/controlled/s14/react",
        "/controlled/s14/settlement",
        "/controlled/s14/settlement/react",
    )
    for path in shell_paths:
        responses = live["paths"][path]["get"]["responses"]
        assert {"200", "403", "503"} <= set(responses), path
        for code in ("403", "503"):
            schema = responses[code]["content"]["application/json"]["schema"]
            assert schema["$ref"].endswith("S01ErrorResponse"), (path, code)


def test_t16_notification_binding_is_application_scoped(
    monkeypatch: Any, tmp_path: Path
) -> None:
    service = _install(monkeypatch, tmp_path)
    client = TestClient(webapp_of())
    _login(client)
    app_a = _admit_manual_review(
        service,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="t16-binding-a",
    )
    # Each C-DEMO service instance is bound to one admitted scenario;
    # a sibling instance over the same persisted store admits the second
    # application so both live in one authority.
    sibling = ControlledScenarioService(
        fixture_root=FIXTURES,
        rules_path=RULES,
        state_path=tmp_path / "target.sqlite3",
        scenario_id="app_uncertain_ocr_noise.json",
    )
    admitted_b = sibling.submit_demo(
        scenario_id="app_uncertain_ocr_noise.json",
        idempotency_key="t16-binding-b",
        principal=INTEGRATOR,
    )
    assert admitted_b.disposition is AdmissionDisposition.ACCEPTED
    assert admitted_b.application_id is not None
    assert sibling.process_next_job().status == "complete"
    app_b = str(admitted_b.application_id)

    revision_a = int(
        client.get(
            f"/controlled/s01/api/queries/applications/{app_a}/current-route"
        ).json()["lifecycle_revision"]
    )
    cancel = client.post(
        f"/controlled/s01/api/commands/applications/{app_a}/cancel",
        json={
            "expected_lifecycle_revision": revision_a,
            "idempotency_key": "t16-binding-cancel-a",
            "reason_code": "UPSTREAM_WITHDRAWN",
        },
    )
    assert cancel.status_code == 200
    armed = client.post(
        f"/controlled/s01/api/commands/applications/{app_a}/settle-termination",
        json={
            "expected_lifecycle_revision": cancel.json()["lifecycle_revision"],
            "idempotency_key": "t16-binding-arm-a",
        },
        headers=_operator_auth(),
    )
    assert armed.status_code == 202

    # A binding naming application B (no pending notification of its own)
    # must never process A's pending event.
    # An unbound public POST is a contract violation and never processes
    # another application's effect.
    unbound = client.post(
        "/controlled/s01/api/commands/process-termination-notification",
        headers=_operator_auth(),
    )
    assert unbound.status_code == 422
    assert unbound.json()["status"] == "rejected"
    assert unbound.json()["reason_code"] == "S14_COMMAND_INVALID"

    foreign = client.post(
        "/controlled/s01/api/commands/process-termination-notification",
        json={"application_id": app_b, "cycle": 1, "operation_id": "op_none"},
        headers=_operator_auth(),
    )
    assert foreign.status_code == 200
    assert foreign.json()["status"] == "idle"
    assert foreign.json()["application_id"] == app_b

    # The bound call for the owning application/cycle processes exactly it.
    bound_operation_id = next(
        effect["id"]
        for effect in armed.json()["unresolved_effects"]
        if effect["kind"] == "termination_notification"
    )
    delivered = client.post(
        "/controlled/s01/api/commands/process-termination-notification",
        json={
            "application_id": app_a,
            "cycle": 1,
            "operation_id": bound_operation_id,
        },
        headers=_operator_auth(),
    )
    assert delivered.status_code == 200
    assert delivered.json()["status"] == "delivered"
    assert delivered.json()["application_id"] == app_a


def test_t16_history_exposes_lifecycle_events_and_late_receipts(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import task4_consistency.web.app as web
    from tests.test_s14_controlled import (
        OPERATOR as S14_OPERATOR,
        SUPPLEMENT_INTEGRATOR,
        _attachment_submission,
        _grant_exact_permission,
        _ready_supplement_request,
        _reopen_policy,
        _settle_to_terminated,
    )

    service, reviewer, _intake, request, source = _ready_supplement_request(
        tmp_path
    )
    application_id = str(request["application_id"])
    route = service.current_route_view(
        principal=reviewer, application_id=application_id
    )
    cancel = service.cancel_application(
        application_id=application_id,
        principal=S01CommandPrincipal(
            subject=reviewer.subject,
            role="integrator",
            scope="C-DEMO",
            source_id="s14-demo-intake",
        ),
        expected_lifecycle_revision=int(route["lifecycle_revision"]),
        idempotency_key="t16-history-cancel",
        reason_code="UPSTREAM_WITHDRAWN",
    )
    settled = _settle_to_terminated(
        service, application_id, cancel["lifecycle_revision"]
    )
    late = service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="t16-history-late-upload",
        principal=SUPPLEMENT_INTEGRATOR,
        now=600,
    )
    assert late.reason_code == "evidence.late_input_requires_reopen"
    _grant_exact_permission(service, application_id, viewer=reviewer)
    reopened = service.reopen_application(
        application_id=application_id,
        principal=S14_OPERATOR,
        expected_lifecycle_revision=settled["lifecycle_revision"],
        idempotency_key="t16-history-reopen",
        target_phase="Intake",
        reopen_policy=_reopen_policy(service),
    )
    assert reopened["status"] == "accepted"

    monkeypatch.setattr(web, "S01_SERVICE", service)
    monkeypatch.setattr(web, "S01_DEMO_SUBJECT", reviewer.subject)
    monkeypatch.setattr(web, "S01_DEMO_CREDENTIAL", "t16-history-credential")
    history_client = TestClient(webapp_of())
    session = history_client.post(
        "/controlled/s01/api/session",
        headers={"Authorization": "Bearer t16-history-credential"},
    )
    assert session.status_code == 204
    history = history_client.get(
        f"/controlled/s01/api/queries/applications/{application_id}/history"
    )
    assert history.status_code == 200, history.text
    body = history.json()
    assert len(body["cancellations"]) == 1
    assert body["cancellations"][0]["cycle"] == 1
    assert body["cancellations"][0]["reason_code"] == "UPSTREAM_WITHDRAWN"
    assert len(body["terminations"]) == 1
    assert body["terminations"][0]["cycle"] == 1
    assert len(body["reopens"]) == 1
    assert body["reopens"][0]["predecessor_cycle"] == 1
    assert body["reopens"][0]["cycle"] == 2
    late_receipts = body["late_input_receipts"]
    assert len(late_receipts) == 1
    assert (
        late_receipts[0]["reason_code"] == "evidence.late_input_requires_reopen"
    )
    # The sealed original cycle is bound to the receipt at rejection time.
    assert late_receipts[0]["cycle"] == 1


def test_t16_operator_planes_accept_distinct_credentials(
    monkeypatch: Any, tmp_path: Path
) -> None:
    service = _install(monkeypatch, tmp_path)
    client = TestClient(webapp_of())
    application_id = _admit_manual_review(
        service,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="t16-distinct-cred",
    )

    # The registered S01 control-plane credential is accepted at the S13
    # delivery read boundary as the same registered operator; audit keeps
    # the S13 subject/source identity.
    via_s01 = client.get(
        f"/controlled/s13/delivery/{application_id}",
        headers=_operator_auth(),
    )
    assert via_s01.status_code == 200, via_s01.text
    native = client.get(
        f"/controlled/s13/delivery/{application_id}",
        headers=_operator_headers(T16_S13_OPERATOR_CREDENTIAL),
    )
    assert native.status_code == 200

    unregistered = client.get(
        f"/controlled/s13/delivery/{application_id}",
        headers=_operator_headers("t16-not-a-credential"),
    )
    assert unregistered.status_code == 403

    # Separation of duties: the S13 credential never unlocks S14 commands.
    settle = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/settle-termination",
        json={
            "expected_lifecycle_revision": 5,
            "idempotency_key": "t16-distinct-cred-settle",
        },
        headers=_operator_headers(T16_S13_OPERATOR_CREDENTIAL),
    )
    assert settle.status_code == 403


def test_t16_s13_mapping_requires_subject_equivalence(
    monkeypatch: Any, tmp_path: Path
) -> None:
    service = _install(monkeypatch, tmp_path)
    client = TestClient(webapp_of())
    application_id = _admit_manual_review(
        service,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="t16-subject-map",
    )

    # The same-operator mapping is verified by subject equality across the
    # two planes; a mismatched S13 subject configuration fails closed even
    # for a valid S01 credential.
    import task4_consistency.web.app as web

    monkeypatch.setattr(web, "S13_OPERATOR_SUBJECT", "t16-someone-else")
    mismatched = client.get(
        f"/controlled/s13/delivery/{application_id}",
        headers=_operator_auth(),
    )
    assert mismatched.status_code == 403

def test_t16_shell_errors_carry_no_store_cache_policy(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import task4_consistency.web.app as web

    _install(monkeypatch, tmp_path)
    client = TestClient(webapp_of())
    shell_paths = (
        "/controlled/s14",
        "/controlled/s14/react",
        "/controlled/s14/settlement",
        "/controlled/s14/settlement/react",
    )
    for path in shell_paths:
        denied = client.get(path)
        assert denied.status_code == 403, path
        assert denied.headers["cache-control"] == "no-store", path
        assert denied.headers["pragma"] == "no-cache", path

        wrong = client.get(
            path, headers={"Authorization": "Bearer t16-not-a-credential"}
        )
        assert wrong.status_code == 403, path
        assert wrong.headers["cache-control"] == "no-store", path
        assert wrong.headers["pragma"] == "no-cache", path

    # Service-unavailable path closes with the same cache policy.
    monkeypatch.setattr(web, "S01_SERVICE", None)
    for path in shell_paths:
        unavailable = client.get(path, headers=_operator_auth())
        assert unavailable.status_code == 503, path
        assert unavailable.headers["cache-control"] == "no-store", path
        assert unavailable.headers["pragma"] == "no-cache", path
        assert (
            unavailable.json()["detail"]["error"] == "S01_UNAVAILABLE"
        ), path


def test_t16_shell_auth_precedes_availability_disclosure(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Unauthorized or invalid-credential callers see only the controlled
    403 fence — never service or React-build availability — while authorized
    callers receive the exact 503 for whichever dependency is unavailable."""
    import task4_consistency.web.app as web

    _install(monkeypatch, tmp_path)
    client = TestClient(webapp_of())
    shell_paths = (
        "/controlled/s14",
        "/controlled/s14/react",
        "/controlled/s14/settlement",
        "/controlled/s14/settlement/react",
    )
    unauthenticated_headers = (
        {},
        {"Authorization": "Bearer t16-not-a-credential"},
        _operator_headers(T16_S13_OPERATOR_CREDENTIAL),
    )

    # Service unavailable: unauthorized callers stay fenced at 403.
    monkeypatch.setattr(web, "S01_SERVICE", None)
    for path in shell_paths:
        for headers in unauthenticated_headers:
            response = client.get(path, headers=headers)
            assert response.status_code == 403, (path, headers)
            assert response.json()["detail"]["error"] == "S01_FORBIDDEN", path
            assert response.headers["cache-control"] == "no-store", path
            assert response.headers["pragma"] == "no-cache", path

    # Restored service but missing build: same fencing for unauthorized
    # callers; authorized callers receive the exact build 503.
    monkeypatch.setattr(web, "S01_SERVICE", _service(tmp_path))
    missing = tmp_path / "missing-react-build-r3"
    missing.mkdir()
    monkeypatch.setattr(web, "S01_REACT_INDEX", missing / "index.html")
    for path in shell_paths:
        for headers in unauthenticated_headers:
            response = client.get(path, headers=headers)
            assert response.status_code == 403, (path, headers)
            assert response.headers["cache-control"] == "no-store", path

        authorized = client.get(
            path,
            headers=_operator_auth()
            if "settlement" in path
            else _demo_auth(),
        )
        assert authorized.status_code == 503, path
        assert (
            authorized.json()["detail"]["error"] == "S14_REACT_UNAVAILABLE"
        ), path


def test_t16_settlement_view_unavailable_contract(
    monkeypatch: Any, tmp_path: Path
) -> None:
    import task4_consistency.web.app as web

    _install(monkeypatch, tmp_path)
    client = TestClient(webapp_of())
    view_url = (
        "/controlled/s01/api/queries/applications/app_some/settlement"
    )

    monkeypatch.setattr(web, "S01_SERVICE", None)
    unavailable = client.get(view_url, headers=_operator_auth())
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["error"] == "S01_UNAVAILABLE"
    assert unavailable.headers["cache-control"] == "no-store"
    assert unavailable.headers["pragma"] == "no-cache"

    live = webapp_of().openapi()["paths"][
        "/controlled/s01/api/queries/applications/{application_id}/settlement"
    ]["get"]["responses"]
    schema = live["503"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("S01ErrorResponse")


def test_t16_registered_late_disposition_binds_application_cycle_and_history(
    tmp_path: Path,
) -> None:
    """Public registered-source revision path: a revision-2 submission after
    an accepted revision-1 keeps its late disposition bound to the owning
    application and sealed rejection cycle, replays exactly, and surfaces in
    the owning application's history."""
    import copy as copy_module

    from tests.test_s02_controlled import (
        INTEGRATOR as REGISTERED_INTEGRATOR,
        TENANT_SCOPE,
        _registered_service,
    )

    service, submission = _registered_service(tmp_path)
    first = service.submit_registered(
        submission=submission,
        idempotency_key="t16-r4-late-1",
        principal=REGISTERED_INTEGRATOR,
    )
    assert first.disposition is AdmissionDisposition.ACCEPTED
    assert first.application_id is not None
    application_id = str(first.application_id)

    revision2 = copy_module.deepcopy(submission)
    revision2["envelope_id"] = "t16-r4-envelope-2"
    revision2["source_revision"] = 2
    revision2["predecessor_revision"] = 1
    late = service.submit_registered(
        submission=revision2,
        idempotency_key="t16-r4-late-2",
        principal=REGISTERED_INTEGRATOR,
    )
    assert late.disposition is AdmissionDisposition.REJECTED
    assert late.reason_code == "evidence.late_input_requires_reopen"
    # The disposition stays bound to the owning application and its current
    # sealed rejection cycle instead of becoming an orphan receipt.
    assert late.application_id == application_id
    assert late.cycle == 1

    replayed = service.submit_registered(
        submission=revision2,
        idempotency_key="t16-r4-late-2",
        principal=REGISTERED_INTEGRATOR,
    )
    assert replayed.disposition is AdmissionDisposition.REJECTED
    assert replayed.replayed is True
    assert replayed.receipt_id == late.receipt_id
    assert replayed.application_id == application_id
    assert replayed.cycle == 1

    history = service.application_history_view(
        principal=S01CommandPrincipal(
            subject=REGISTERED_INTEGRATOR.subject,
            role="reviewer",
            scope=TENANT_SCOPE,
            source_id="t16-r4-review-console",
        ),
        application_id=application_id,
    )
    receipts = [
        item
        for item in history["late_input_receipts"]
        if item.get("reason_code") == "evidence.late_input_requires_reopen"
    ]
    assert len(receipts) == 1
    assert receipts[0]["cycle"] == 1


def test_t16_notification_public_command_requires_target_binding(
    monkeypatch: Any, tmp_path: Path
) -> None:
    service = _install(monkeypatch, tmp_path)
    client = TestClient(webapp_of())
    _login(client)
    application_id = _admit_manual_review(
        service,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="t16-unbound-a",
    )
    revision = int(
        client.get(
            f"/controlled/s01/api/queries/applications/{application_id}/current-route"
        ).json()["lifecycle_revision"]
    )
    cancel = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/cancel",
        json={
            "expected_lifecycle_revision": revision,
            "idempotency_key": "t16-unbound-cancel",
            "reason_code": "UPSTREAM_WITHDRAWN",
        },
    )
    assert cancel.status_code == 200
    armed = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/settle-termination",
        json={
            "expected_lifecycle_revision": cancel.json()["lifecycle_revision"],
            "idempotency_key": "t16-unbound-arm",
        },
        headers=_operator_auth(),
    )
    assert armed.status_code == 202
    operation_id = next(
        effect["id"]
        for effect in armed.json()["unresolved_effects"]
        if effect["kind"] == "termination_notification"
    )

    # An unbound public POST is a contract violation and must never fall
    # back to a global claim.
    unbound = client.post(
        "/controlled/s01/api/commands/process-termination-notification",
        headers=_operator_auth(),
    )
    assert unbound.status_code == 422
    assert unbound.json()["status"] == "rejected"
    assert unbound.json()["reason_code"] == "S14_COMMAND_INVALID"

    # A binding with a wrong operation id processes nothing.
    mismatched = client.post(
        "/controlled/s01/api/commands/process-termination-notification",
        json={
            "application_id": application_id,
            "cycle": 1,
            "operation_id": "op_does_not_exist",
        },
        headers=_operator_auth(),
    )
    assert mismatched.status_code == 200
    assert mismatched.json()["status"] == "idle"

    # The exact bound command delivers exactly that operation.
    delivered = client.post(
        "/controlled/s01/api/commands/process-termination-notification",
        json={
            "application_id": application_id,
            "cycle": 1,
            "operation_id": operation_id,
        },
        headers=_operator_auth(),
    )
    assert delivered.status_code == 200
    assert delivered.json()["status"] == "delivered"


def test_t16_settlement_view_exposes_pending_effect_and_permission(
    monkeypatch: Any, tmp_path: Path
) -> None:
    service = _install(monkeypatch, tmp_path)
    client = TestClient(webapp_of())
    _login(client)
    application_id = _admit_manual_review(
        service,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="t16-settle-view",
    )
    view_url = (
        f"/controlled/s01/api/queries/applications/{application_id}/settlement"
    )

    forbidden = client.get(view_url)
    assert forbidden.status_code == 403

    revision = int(
        client.get(
            f"/controlled/s01/api/queries/applications/{application_id}/current-route"
        ).json()["lifecycle_revision"]
    )
    cancel = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/cancel",
        json={
            "expected_lifecycle_revision": revision,
            "idempotency_key": "t16-view-cancel",
            "reason_code": "UPSTREAM_WITHDRAWN",
        },
    )
    assert cancel.status_code == 200
    armed = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/settle-termination",
        json={
            "expected_lifecycle_revision": cancel.json()["lifecycle_revision"],
            "idempotency_key": "t16-view-arm",
        },
        headers=_operator_auth(),
    )
    assert armed.status_code == 202

    during = client.get(view_url, headers=_operator_auth())
    assert during.status_code == 200, during.text
    body = during.json()
    assert body["phase"] == "Terminating"
    assert body["cycle"] == 1
    assert body["pending_notification"] is not None
    assert body["pending_notification"]["operation_id"]
    assert body["reopen_permission"] is None
    assert during.headers["cache-control"] == "no-store"

    delivered = client.post(
        "/controlled/s01/api/commands/process-termination-notification",
        json={
            "application_id": application_id,
            "cycle": 1,
            "operation_id": body["pending_notification"]["operation_id"],
        },
        headers=_operator_auth(),
    )
    assert delivered.json()["status"] == "delivered"
    settled = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/settle-termination",
        json={
            "expected_lifecycle_revision": cancel.json()["lifecycle_revision"],
            "idempotency_key": "t16-view-seal",
        },
        headers=_operator_auth(),
    )
    assert settled.json()["status"] == "terminated"

    granted = client.post(
        f"/controlled/s01/api/commands/applications/{application_id}/grant-reopen-permission",
        json={
            "expected_lifecycle_revision": settled.json()["lifecycle_revision"],
            "approver_subject": "t16-independent-approver",
            "permission_id": "t16-view-permission/1",
            "idempotency_key": "t16-view-grant",
        },
        headers=_operator_auth(),
    )
    assert granted.status_code == 200

    after = client.get(view_url, headers=_operator_auth())
    after_body = after.json()
    assert after_body["phase"] == "Terminated"
    assert after_body["pending_notification"] is None
    permission = after_body["reopen_permission"]
    assert permission is not None
    assert permission["permission_id"] == "t16-view-permission/1"
    assert len(permission["artifact_release_digest"]) == 64

    missing = client.get(
        "/controlled/s01/api/queries/applications/app_unknown/settlement",
        headers=_operator_auth(),
    )
    assert missing.status_code == 404


def webapp_of():
    import task4_consistency.web.app as web

    return web.app
