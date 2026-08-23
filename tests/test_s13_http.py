"""Ticket #29 S13 — HTTP authority, authorization, query invariants."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from task4_consistency.controlled.s01 import ControlledScenarioService, S01CommandPrincipal
from task4_consistency.controlled.s13 import (
    DownstreamRecipientRegistration,
    InMemoryDownstreamAdapter,
    RegisteredDownstreamRegistry,
)
from task4_consistency.web import app as webapp

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"

INTEGRATOR = S01CommandPrincipal(
    subject="registered-test-integrator",
    role="integrator",
    scope="C-DEMO",
    source_id="s01-test-client",
)

S13_CREDENTIAL = "s13-test-operator-credential"
S13_SUBJECT = "s13-test-operator"


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {S13_CREDENTIAL}"}


def _harness(tmp_path: Path) -> tuple[ControlledScenarioService, str]:
    adapter = InMemoryDownstreamAdapter()
    reg = DownstreamRecipientRegistration(
        scope="C-DEMO",
        recipient_registration_id="c-demo-downstream-review-default",
        recipient_id="downstream-review-desk",
        adapter_id=adapter.adapter_id,
        adapter_version=adapter.adapter_version,
    )
    registry = RegisteredDownstreamRegistry([reg], {adapter.adapter_id: adapter})
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=RULES,
        state_path=tmp_path / "target.sqlite3",
        downstream_registry=registry,
    )
    adapter2 = registry._adapters[adapter.adapter_id]
    _ = adapter2
    return service, adapter.adapter_id


def _install(monkeypatch: Any, tmp_path: Path) -> tuple[ControlledScenarioService, str]:
    service, _ = _harness(tmp_path)
    # Monkeypatch the app module's S13 operator credential + S01_SERVICE authority.
    monkeypatch.setattr(webapp, "S13_OPERATOR_CREDENTIAL", S13_CREDENTIAL)
    monkeypatch.setattr(webapp, "S13_OPERATOR_SUBJECT", S13_SUBJECT)
    # Also ensure S01_SERVICE exposes the same instance (s13_http falls back to it).
    monkeypatch.setattr(webapp, "S01_SERVICE", service)
    return service, tmp_path.name


def test_s13_http_requires_operator_identity(monkeypatch: Any, tmp_path: Path) -> None:
    _install(monkeypatch, tmp_path)
    client = TestClient(webapp.app)
    # No credential -> 403 regardless of path.
    assert client.get("/controlled/s13/delivery/app_unknown").status_code == 403
    assert (
        client.post(
            "/controlled/s13/api/commands/reconcile",
            json={"obligation_id": "obligation_xxx"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/controlled/s13/api/commands/compensate",
            json={"obligation_id": "obligation_xxx"},
        ).status_code
        == 403
    )
    assert client.post("/controlled/s13/api/commands/process_next_delivery").status_code == 403
    assert client.get("/controlled/s13").status_code == 403
    # Global web token is never authority for S13.
    monkeypatch.setenv("TASK4_WEB_TOKEN", "global-token")
    assert (
        client.get(
            "/controlled/s13/delivery/app_unknown",
            headers={"Authorization": "Bearer global-token"},
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/controlled/s13/delivery/app_unknown",
            headers=_auth(),
        ).status_code
        == 404
    )
    assert client.get("/controlled/s13", headers=_auth()).status_code == 200
    assert (
        client.get(
            "/controlled/s13",
            headers={"Authorization": "Bearer global-token"},
        ).status_code
        == 403
    )


def test_s13_http_never_falls_back_to_s01_operator_identity(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path)
    monkeypatch.setattr(webapp, "S13_OPERATOR_CREDENTIAL", "")
    monkeypatch.setattr(webapp, "S13_OPERATOR_SUBJECT", "")
    monkeypatch.setattr(webapp, "S01_OPERATOR_CREDENTIAL", "s01-operator")
    monkeypatch.setattr(webapp, "S01_OPERATOR_SUBJECT", "s01-subject")
    client = TestClient(webapp.app)
    response = client.get(
        "/controlled/s13/delivery/app_unknown",
        headers={"Authorization": "Bearer s01-operator"},
    )
    assert response.status_code == 403


def test_s13_http_query_shows_verification_completed_pending_and_received_distinct(
    monkeypatch: Any, tmp_path: Path
) -> None:
    service, _ = _install(monkeypatch, tmp_path)
    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s13-http-query",
        principal=INTEGRATOR,
    )
    assert admitted.application_id is not None
    orig = service.verification_route_for_checks
    service.verification_route_for_checks = lambda checks, findings: "auto_complete"  # type: ignore[assignment,method-assign]
    service.process_next_job()
    service.verification_route_for_checks = orig  # type: ignore[assignment]
    service.refresh_projection()

    client = TestClient(webapp.app)
    resp = client.get(
        f"/controlled/s13/delivery/{admitted.application_id}",
        headers=_auth(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schema_version"] == "s13-delivery-view/1"
    assert body["phase"] == "Verification Completed"
    assert body["verification_completed"] is True
    assert body["obligation"] is not None
    assert "payload" not in body["obligation"]
    assert "route_basis" not in body["obligation"]
    assert len(body["routing_history"]) == 1
    history = body["routing_history"][0]
    assert history["route"] == "auto_complete"
    assert history["attribution_kind"] == "automatic"
    assert history["attribution"] == {
        "decision_id": None,
        "work_item_id": None,
        "request_id": None,
        "batch_id": None,
        "work_item_ids": [],
    }
    assert history["completion_event_id"]
    assert history["completion_lifecycle_revision"] == body["lifecycle_revision"]
    assert history["run_id"]
    assert len(history["evidence_snapshot_digest"]) == 64
    assert len(history["release_digest"]) == 64
    assert len(history["route_basis_digest"]) == 64
    assert history["obligation_id"] == body["obligation"]["obligation_id"]
    assert history["operation_id"] == body["obligation"]["operation_id"]
    assert body["delivery_status"] == "pending"

    # Drive the delivery through the HTTP sender; then query shows received.
    send_resp = client.post("/controlled/s13/api/commands/process_next_delivery", headers=_auth())
    assert send_resp.status_code == 200, send_resp.text
    assert send_resp.json()["status"] == "received"

    again = client.get(
        f"/controlled/s13/delivery/{admitted.application_id}",
        headers=_auth(),
    )
    assert again.json()["delivery_status"] == "received"
    assert again.json()["obligation"] is not None
    # Verification Completed vs delivery pending/received are distinct fields.
    assert again.json()["verification_completed"] is True
    assert again.json()["phase"] == "Verification Completed"


def test_s13_query_keeps_prior_cycle_receipt_out_of_current_projection(
    monkeypatch: Any, tmp_path: Path
) -> None:
    service, _ = _install(monkeypatch, tmp_path)
    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s13-http-cycle-one",
        principal=INTEGRATOR,
    )
    assert admitted.application_id is not None
    original_route = service.verification_route_for_checks
    service.verification_route_for_checks = lambda checks, findings: "auto_complete"  # type: ignore[assignment,method-assign]
    service.process_next_job()
    service.verification_route_for_checks = original_route  # type: ignore[assignment]
    service.refresh_projection()
    first = service.delivery_view(
        principal=S01CommandPrincipal(
            subject=S13_SUBJECT,
            role="operator",
            scope="C-DEMO",
            source_id="s13-delivery-console",
        ),
        application_id=str(admitted.application_id),
    )
    assert first["cycle"] == 1
    assert first["delivery_status"] == "pending"

    # Reopen is an application-Lifecycle owner operation.  This test mutates
    # only the copied authority state to exercise the read projection seam;
    # no browser command or second business authority is introduced.
    service._store.applications[str(admitted.application_id)].update(  # type: ignore[index]
        {
            "cycle": 2,
            "phase": "Assembly",
            "route": "pending_check",
            "lifecycle_revision": int(
                service._store.applications[str(admitted.application_id)][  # type: ignore[index]
                    "lifecycle_revision"
                ]
            )
            + 1,
        }
    )
    service._store.persist()
    current = service.delivery_view(
        principal=S01CommandPrincipal(
            subject=S13_SUBJECT,
            role="operator",
            scope="C-DEMO",
            source_id="s13-delivery-console",
        ),
        application_id=str(admitted.application_id),
    )
    assert current["cycle"] == 2
    assert current["phase"] == "Assembly"
    assert current["obligation"] is None
    assert current["delivery_status"] == "none"
    assert len(current["routing_history"]) == 1
    assert current["routing_history"][0]["operation_id"] == first["obligation"]["operation_id"]


def test_s13_http_reconcile_unknown_via_same_operation(monkeypatch: Any, tmp_path: Path) -> None:
    adapter = InMemoryDownstreamAdapter(behavior="timeout_after_execute")
    reg = DownstreamRecipientRegistration(
        scope="C-DEMO",
        recipient_registration_id="c-demo-downstream-review-default",
        recipient_id="downstream-review-desk",
        adapter_id=adapter.adapter_id,
        adapter_version=adapter.adapter_version,
    )
    registry = RegisteredDownstreamRegistry([reg], {adapter.adapter_id: adapter})
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=RULES,
        state_path=tmp_path / "target.sqlite3",
        downstream_registry=registry,
    )
    monkeypatch.setattr(webapp, "S13_OPERATOR_CREDENTIAL", S13_CREDENTIAL)
    monkeypatch.setattr(webapp, "S13_OPERATOR_SUBJECT", S13_SUBJECT)
    monkeypatch.setattr(webapp, "S01_SERVICE", service)

    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s13-http-reconcile",
        principal=INTEGRATOR,
    )
    orig = service.verification_route_for_checks
    service.verification_route_for_checks = lambda checks, findings: "auto_complete"  # type: ignore[assignment,method-assign]
    service.process_next_job()
    service.verification_route_for_checks = orig  # type: ignore[assignment]
    service.refresh_projection()

    client = TestClient(webapp.app)
    unknown_resp = client.post("/controlled/s13/api/commands/process_next_delivery", headers=_auth())
    assert unknown_resp.json()["status"] == "unknown"

    # Resolve obligation id via query.
    query = client.get(f"/controlled/s13/delivery/{admitted.application_id}", headers=_auth()).json()
    oid = query["obligation"]["obligation_id"]

    recon = client.post(
        "/controlled/s13/api/commands/reconcile",
        json={"obligation_id": oid},
        headers=_auth(),
    )
    assert recon.status_code == 200, recon.text
    assert recon.json()["status"] == "received"
    # Same operation id as before.
    assert recon.json()["operation_id"] == query["obligation"]["operation_id"]


def test_s13_http_compensate_and_compensation_failure_with_recovery(
    monkeypatch: Any, tmp_path: Path
) -> None:
    adapter = InMemoryDownstreamAdapter(compensation_behavior="fail")
    reg = DownstreamRecipientRegistration(
        scope="C-DEMO",
        recipient_registration_id="c-demo-downstream-review-default",
        recipient_id="downstream-review-desk",
        adapter_id=adapter.adapter_id,
        adapter_version=adapter.adapter_version,
    )
    registry = RegisteredDownstreamRegistry([reg], {adapter.adapter_id: adapter})
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=RULES,
        state_path=tmp_path / "target.sqlite3",
        downstream_registry=registry,
    )
    monkeypatch.setattr(webapp, "S13_OPERATOR_CREDENTIAL", S13_CREDENTIAL)
    monkeypatch.setattr(webapp, "S13_OPERATOR_SUBJECT", S13_SUBJECT)
    monkeypatch.setattr(webapp, "S01_SERVICE", service)

    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s13-http-compensate",
        principal=INTEGRATOR,
    )
    orig = service.verification_route_for_checks
    service.verification_route_for_checks = lambda checks, findings: "auto_complete"  # type: ignore[assignment,method-assign]
    service.process_next_job()
    service.verification_route_for_checks = orig  # type: ignore[assignment]
    service.refresh_projection()

    client = TestClient(webapp.app)
    client.post("/controlled/s13/api/commands/process_next_delivery", headers=_auth())
    query = client.get(f"/controlled/s13/delivery/{admitted.application_id}", headers=_auth()).json()
    oid = query["obligation"]["obligation_id"]

    comp = client.post(
        "/controlled/s13/api/commands/compensate",
        json={"obligation_id": oid},
        headers=_auth(),
    )
    assert comp.status_code == 200, comp.text
    assert comp.json()["status"] == "failed"
    # Delivery status reflects compensation failure and the obligation is not deleted.
    after = client.get(f"/controlled/s13/delivery/{admitted.application_id}", headers=_auth()).json()
    assert after["delivery_status"] == "compensation_failed"
    assert after["obligation"] is not None
    adapter.compensation_behavior = "succeed"
    retry = client.post(
        "/controlled/s13/api/commands/compensate",
        json={"obligation_id": oid},
        headers=_auth(),
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "compensated"


def test_s13_http_openapi_contains_closed_s13_schema(monkeypatch: Any) -> None:
    client = TestClient(webapp.app)
    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200, openapi.text
    paths = openapi.json().get("paths", {})
    assert "/controlled/s13/delivery/{application_id}" in paths
    assert "/controlled/s13/api/commands/reconcile" in paths
    assert "/controlled/s13/api/commands/compensate" in paths
    assert "/controlled/s13/api/commands/process_next_delivery" in paths
    for shell_path in ("/controlled/s13", "/controlled/s13/react"):
        assert shell_path in paths
        assert set(paths[shell_path]["get"]["responses"]) == {"200", "403", "503"}
        for status in ("403", "503"):
            assert set(paths[shell_path]["get"]["responses"][status]["content"]) == {
                "application/json"
            }
    schema = openapi.json()["components"]["schemas"]["S13ObligationSummary"]
    assert schema["additionalProperties"] is False
    query_schema = openapi.json()["components"]["schemas"]["S13QueryResponse"]
    assert query_schema["properties"]["routing_history"]["items"]["$ref"].endswith(
        "/S13RoutingHistoryEntry"
    )
    for model in ("S13RoutingAttribution", "S13RoutingHistoryEntry"):
        assert openapi.json()["components"]["schemas"][model]["additionalProperties"] is False


def test_s13_react_shell_is_no_store_and_missing_build_fails_closed(
    monkeypatch: Any, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path)
    client = TestClient(webapp.app)
    for shell_path in ("/controlled/s13", "/controlled/s13/react"):
        response = client.get(shell_path, headers=_auth())
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "session" not in response.cookies

    monkeypatch.setattr(
        webapp,
        "S01_REACT_INDEX",
        tmp_path / "missing-react-build" / "index.html",
    )
    for shell_path in ("/controlled/s13", "/controlled/s13/react"):
        response = client.get(shell_path, headers=_auth())
        assert response.status_code == 503
        assert response.json() == {
            "detail": {
                "error": "S13_REACT_UNAVAILABLE",
                "message": "Controlled S13 delivery shell is not built",
            }
        }
        assert response.headers["cache-control"] == "no-store"


def test_s13_http_register_router_conflicting_module_rejected(monkeypatch: Any) -> None:
    from task4_consistency.web.s13_http import register_router

    assert webapp.app is not None
    # Re-registering with the same module is idempotent.
    register_router(webapp.app, webapp)
    with pytest.raises(RuntimeError, match="different application module"):
        register_router(webapp.app, object())
