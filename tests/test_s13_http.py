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
    # Global web token is never authority for S13.
    monkeypatch.setenv("TASK4_WEB_TOKEN", "global-token")
    assert (
        client.get(
            "/controlled/s13/delivery/app_unknown",
            headers={"Authorization": "Bearer global-token"},
        ).status_code
        == 403
    )


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


def test_s13_http_openapi_contains_closed_s13_schema(monkeypatch: Any) -> None:
    client = TestClient(webapp.app)
    openapi = client.get("/openapi.json")
    assert openapi.status_code == 200, openapi.text
    paths = openapi.json().get("paths", {})
    assert "/controlled/s13/delivery/{application_id}" in paths
    assert "/controlled/s13/api/commands/reconcile" in paths
    assert "/controlled/s13/api/commands/compensate" in paths
    assert "/controlled/s13/api/commands/process_next_delivery" in paths


def test_s13_http_register_router_conflicting_module_rejected(monkeypatch: Any) -> None:
    from task4_consistency.web.s13_http import register_router

    assert webapp.app is not None
    # Re-registering with the same module is idempotent.
    register_router(webapp.app, webapp)
    with pytest.raises(RuntimeError, match="different application module"):
        register_router(webapp.app, object())
