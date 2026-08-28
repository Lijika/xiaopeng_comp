"""Ticket #32 / S16 governed deletion — HTTP authority and transport.

Covers identity fail-closed configuration, the four-role allow/deny matrix,
same-scope existence hiding, typed 4xx/409/503 responses, no-store headers,
idempotent replay, OpenAPI schema exposure and the canonical/alias shell
routes with fail-closed missing-build 503.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from task4_consistency.controlled.s01 import (
    AdmissionDisposition,
    ControlledScenarioService,
    S01CommandPrincipal,
)
from task4_consistency.controlled.s16 import (
    BackupDeletionOwner,
    ExportTempOwner,
    GovernedDeletionService,
    RetentionPolicy,
    S01DeletionOwner,
    S02DeletionOwner,
    S12DeletionOwner,
)
from task4_consistency.controlled.s12 import EvaluationService

from tests.test_s16_controlled import (
    CLOCK,
    _admit_c_demo,
    _c_demo_service,
    _empty_evaluation,
    _terminate,
)

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"
FIXTURES = ROOT / "fixtures" / "applications"

GOVERNANCE_CREDENTIAL = "s16-governance-credential"
GOVERNANCE_SUBJECT = "s16-http-governance"
APPROVER1_CREDENTIAL = "s16-approver1-credential"
APPROVER1_SUBJECT = "s16-http-approver1"
APPROVER2_CREDENTIAL = "s16-approver2-credential"
APPROVER2_SUBJECT = "s16-http-approver2"
OPERATOR_CREDENTIAL = "s16-http-operator-credential"
DEMO_CREDENTIAL = "s16-http-demo-credential"
S08_ADMIN_CREDENTIAL = "s16-http-s08-admin-credential"
REVIEWER_CREDENTIAL = "s16-http-reviewer-credential"

PREFERRED = "APP-R53-BAD-ENGINE"


def _auth(credential: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential}"}


def _recording_writer() -> Any:
    def writer(record: dict[str, Any]) -> bool:
        return True

    return writer


def _build_s16_service(
    tmp_path: Path, service: ControlledScenarioService
) -> GovernedDeletionService:
    retention = RetentionPolicy(retention_seconds=0)
    s01_owner = S01DeletionOwner(
        service,
        retention=retention,
        clock=lambda: int(CLOCK["now"]),
    )
    return GovernedDeletionService(
        ledger_path=tmp_path / "s16.sqlite3",
        owners={
            "s01": s01_owner,
            "s02": S02DeletionOwner(
                service.registered_source_boundary, s01_owner
            ),
            "s12": S12DeletionOwner(_empty_evaluation(tmp_path)),
            "backup": BackupDeletionOwner(
                tmp_path / "backups", clock=lambda: int(CLOCK["now"])
            ),
            "s17-disabled": ExportTempOwner(),
        },
        retention=retention,
        governance_subject=GOVERNANCE_SUBJECT,
        approver_subjects=(APPROVER1_SUBJECT, APPROVER2_SUBJECT),
        security_audit_writer=_recording_writer(),
        clock=lambda: int(CLOCK["now"]),
    )


def _install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Any, str]:
    """Bind the web module to one terminated C-DEMO application and a live
    S16 service; returns the module and the application id."""
    import task4_consistency.web.app as web

    service = _c_demo_service(tmp_path)
    application_id = _admit_c_demo(service, key="s16-http-intake")
    _terminate(service, application_id)
    s16 = _build_s16_service(tmp_path, service)

    monkeypatch.setattr(web, "S01_SERVICE", service)
    monkeypatch.setattr(web, "S01_BACKGROUND_ENABLED", False)
    monkeypatch.setattr(web, "S01_REQUIRE_CONFIGURED_STARTUP", False)
    monkeypatch.setattr(web, "S16_SERVICE", s16)
    monkeypatch.setattr(web, "S16_GOVERNANCE_CREDENTIAL", GOVERNANCE_CREDENTIAL)
    monkeypatch.setattr(web, "S16_GOVERNANCE_SUBJECT", GOVERNANCE_SUBJECT)
    monkeypatch.setattr(web, "S16_APPROVER1_CREDENTIAL", APPROVER1_CREDENTIAL)
    monkeypatch.setattr(web, "S16_APPROVER1_SUBJECT", APPROVER1_SUBJECT)
    monkeypatch.setattr(web, "S16_APPROVER2_CREDENTIAL", APPROVER2_CREDENTIAL)
    monkeypatch.setattr(web, "S16_APPROVER2_SUBJECT", APPROVER2_SUBJECT)
    monkeypatch.setattr(web, "S16_GOVERNANCE_SCOPE", "C-DEMO")
    return web, application_id


def test_s16_routes_fail_closed_without_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import task4_consistency.web.app as web

    service = _c_demo_service(tmp_path)
    monkeypatch.setattr(web, "S01_SERVICE", service)
    monkeypatch.setattr(web, "S01_BACKGROUND_ENABLED", False)
    monkeypatch.setattr(web, "S01_REQUIRE_CONFIGURED_STARTUP", False)
    monkeypatch.setattr(web, "S16_SERVICE", None)
    monkeypatch.setattr(web, "S16_GOVERNANCE_CREDENTIAL", "")
    monkeypatch.setattr(web, "S16_GOVERNANCE_SUBJECT", "")
    client = TestClient(web.app)

    # R5 (P2-1): without a configured governance subject NO caller is
    # authorized — every S16 surface returns the stable 403 in the
    # unconfigured state instead of a 503 that leaks configuration state.
    for method, path, body in (
        ("post", "/controlled/s16/api/deletions/preflight", {"application_reference": PREFERRED, "idempotency_key": "x"}),
        ("post", "/controlled/s16/api/deletions/req/cancel", {"idempotency_key": "x"}),
        ("post", "/controlled/s16/api/deletions/req/commit", {"idempotency_key": "x"}),
        ("get", "/controlled/s16/api/deletions/req", None),
        ("get", "/controlled/s16/api/deletions/req/receipt", None),
        ("get", "/controlled/s16", None),
    ):
        kwargs = {"headers": _auth(GOVERNANCE_CREDENTIAL)}
        if body is not None:
            kwargs["json"] = body
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 403, (method, path, response.text)
        assert response.json()["detail"]["error"] == "S16_FORBIDDEN"
        assert response.headers["cache-control"] == "no-store"
    # Other planes stay reachable.
    assert client.get("/api/health").status_code == 200


def test_s16_identity_matrix_allows_governance_and_approvers_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    web, _application_id = _install(monkeypatch, tmp_path)
    del web
    client = TestClient(_webapp())
    # Anonymous and every non-S16 identity are denied on the governance
    # surface.
    denied = (None, OPERATOR_CREDENTIAL, DEMO_CREDENTIAL, S08_ADMIN_CREDENTIAL, REVIEWER_CREDENTIAL, APPROVER1_CREDENTIAL)
    for credential in denied:
        headers = _auth(credential) if credential is not None else {}
        response = client.post(
            "/controlled/s16/api/deletions/preflight",
            headers=headers,
            json={"application_reference": PREFERRED, "idempotency_key": "matrix-pre"},
        )
        assert response.status_code == 403, (credential, response.text)
        assert response.json()["detail"]["error"] == "S16_FORBIDDEN"
    # Approvers can approve but never preflight/commit.
    for credential in (APPROVER1_CREDENTIAL, APPROVER2_CREDENTIAL):
        response = client.post(
            "/controlled/s16/api/deletions/preflight",
            headers=_auth(credential),
            json={"application_reference": PREFERRED, "idempotency_key": f"matrix-{credential}"},
        )
        assert response.status_code == 403
    # The governance owner can preflight.
    response = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"application_reference": PREFERRED, "idempotency_key": "matrix-ok"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def _webapp():
    import task4_consistency.web.app as web

    return web.app


def test_s16_preflight_commit_process_receipt_flow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path)
    client = TestClient(_webapp())

    preflight = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"application_reference": PREFERRED, "idempotency_key": "flow-pre"},
    )
    assert preflight.status_code == 200, preflight.text
    body = preflight.json()
    assert body["status"] == "accepted"
    assert len(body["entries"]) == 9
    request_id = body["request_id"]
    manifest_digest = body["manifest_digest"]
    assert body["early_deletion"] is False

    # Approve with the governance owner is forbidden (never an approver).
    denied = client.post(
        f"/controlled/s16/api/deletions/{request_id}/approve",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"manifest_digest": manifest_digest, "idempotency_key": "flow-ap-gov"},
    )
    assert denied.status_code == 403
    # Approvers can approve even for a due deletion (harmless replay state).
    approved = client.post(
        f"/controlled/s16/api/deletions/{request_id}/approve",
        headers=_auth(APPROVER1_CREDENTIAL),
        json={"manifest_digest": manifest_digest, "idempotency_key": "flow-ap-1"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "accepted"

    commit = client.post(
        f"/controlled/s16/api/deletions/{request_id}/commit",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"idempotency_key": "flow-commit"},
    )
    assert commit.status_code == 200, commit.text
    assert commit.json()["status"] == "accepted"

    processed = client.post(
        "/controlled/s16/api/process",
        headers=_auth(GOVERNANCE_CREDENTIAL),
    )
    assert processed.status_code == 200, processed.text
    assert processed.json()["status"] == "complete"

    query = client.get(
        f"/controlled/s16/api/deletions/{request_id}",
        headers=_auth(GOVERNANCE_CREDENTIAL),
    )
    assert query.status_code == 200, query.text
    assert query.json()["job"]["status"] == "complete"
    assert query.headers["cache-control"] == "no-store"

    receipt = client.get(
        f"/controlled/s16/api/deletions/{request_id}/receipt",
        headers=_auth(GOVERNANCE_CREDENTIAL),
    )
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["result"] == "deleted"
    assert receipt.headers["cache-control"] == "no-store"
    serialized = json.dumps(receipt.json())
    for token in (PREFERRED, "app_", "target.sqlite3"):
        assert token not in serialized


def test_s16_typed_errors_idempotency_and_existence_hiding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    web, _application_id = _install(monkeypatch, tmp_path)
    del web
    client = TestClient(_webapp())

    # Unknown application: governance and demo identities receive the same
    # existence-hiding 404.
    unknown = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"application_reference": "UNKNOWN-REF", "idempotency_key": "eh-1"},
    )
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["error"] == "S16_NOT_FOUND"
    unknown_demo = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_auth(DEMO_CREDENTIAL),
        json={"application_reference": "UNKNOWN-REF", "idempotency_key": "eh-2"},
    )
    assert unknown_demo.status_code == 403  # unauthorized, never existence
    # Same-scope hidden: a registered reviewer still gets 403, not 404.
    reviewer = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_auth(REVIEWER_CREDENTIAL),
        json={"application_reference": "UNKNOWN-REF", "idempotency_key": "eh-3"},
    )
    assert reviewer.status_code == 403

    # Unknown request ids hide on every command.
    for method, path, body in (
        ("get", "/controlled/s16/api/deletions/s16req_unknown", None),
        ("get", "/controlled/s16/api/deletions/s16req_unknown/receipt", None),
        ("post", "/controlled/s16/api/deletions/s16req_unknown/cancel", {"idempotency_key": "x"}),
        ("post", "/controlled/s16/api/deletions/s16req_unknown/commit", {"idempotency_key": "x"}),
        ("post", "/controlled/s16/api/deletions/s16req_unknown/repair", {"owner_id": "s02", "repair_fact": "x", "idempotency_key": "x"}),
    ):
        kwargs = {"headers": _auth(GOVERNANCE_CREDENTIAL)}
        if body is not None:
            kwargs["json"] = body
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 404, (method, path, response.text)

    # Idempotency: same key same content replays; same key different content
    # is a typed conflict.
    first = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"application_reference": PREFERRED, "idempotency_key": "idem-1"},
    )
    assert first.status_code == 200
    replayed = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"application_reference": PREFERRED, "idempotency_key": "idem-1"},
    )
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] is True
    conflict = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"application_reference": "OTHER-REF", "idempotency_key": "idem-1"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["reason_code"] == "S16 idempotency conflict: same key different content"

    # Early deletion without two approvals is a typed 409 gate.
    service = ControlledScenarioService(
        fixture_root=FIXTURES,
        rules_path=RULES,
        state_path=tmp_path / "early.sqlite3",
        clock=lambda: int(CLOCK["now"]),
    )
    application_id = _admit_c_demo(service, key="s16-http-early")
    _terminate(service, application_id)
    early_retention = RetentionPolicy(retention_seconds=10**12)
    early_s01_owner = S01DeletionOwner(
        service,
        retention=early_retention,
        clock=lambda: int(CLOCK["now"]),
    )
    early_s16 = GovernedDeletionService(
        ledger_path=tmp_path / "s16-early.sqlite3",
        owners={
            "s01": early_s01_owner,
            "s02": S02DeletionOwner(
                service.registered_source_boundary, early_s01_owner
            ),
            "s12": S12DeletionOwner(_empty_evaluation(tmp_path)),
            "backup": BackupDeletionOwner(tmp_path / "early-backups", clock=lambda: int(CLOCK["now"])),
            "s17-disabled": ExportTempOwner(),
        },
        retention=early_retention,
        governance_subject=GOVERNANCE_SUBJECT,
        approver_subjects=(APPROVER1_SUBJECT, APPROVER2_SUBJECT),
        security_audit_writer=_recording_writer(),
        clock=lambda: int(CLOCK["now"]),
    )
    import task4_consistency.web.app as web2

    monkeypatch.setattr(web2, "S16_SERVICE", early_s16)
    early = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"application_reference": PREFERRED, "idempotency_key": "early-pre"},
    )
    assert early.status_code == 200
    assert early.json()["early_deletion"] is True
    blocked = client.post(
        f"/controlled/s16/api/deletions/{early.json()['request_id']}/commit",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"idempotency_key": "early-commit"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["reason_code"] == "S16_APPROVALS_INCOMPLETE"

    # Invalid command shape is a typed 422.
    invalid = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"application_reference": PREFERRED, "idempotency_key": ""},
    )
    assert invalid.status_code == 422


def test_s16_openapi_schema_exposes_typed_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path)
    client = TestClient(_webapp())
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    for path in (
        "/controlled/s16/api/deletions/preflight",
        "/controlled/s16/api/deletions/{request_id}/approve",
        "/controlled/s16/api/deletions/{request_id}/cancel",
        "/controlled/s16/api/deletions/{request_id}/commit",
        "/controlled/s16/api/deletions/{request_id}/repair",
        "/controlled/s16/api/deletions/{request_id}",
        "/controlled/s16/api/deletions/{request_id}/receipt",
        "/controlled/s16/api/process",
        "/controlled/s16",
        "/controlled/s16/react",
    ):
        assert path in paths, path
    schemas = spec["components"]["schemas"]
    for name in (
        "S16PreflightResponse",
        "S16ManifestEntry",
        "S16QueryResponse",
        "S16ReceiptResponse",
        "S16CommandResponse",
        "S16ErrorResponse",
        "S16ProcessResponse",
    ):
        assert name in schemas, name


def test_s16_shell_canonical_alias_and_missing_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    web, _application_id = _install(monkeypatch, tmp_path)
    import task4_consistency.web.app as webapp_module

    del webapp_module
    client = TestClient(_webapp())

    anonymous = client.get("/controlled/s16")
    assert anonymous.status_code == 403
    anonymous_alias = client.get("/controlled/s16/react")
    assert anonymous_alias.status_code == 403

    # A qualified build serves both the canonical route and the alias.
    monkeypatch.setattr(
        web,
        "_react_shell_index_html",
        lambda: "<!doctype html><html><head></head><body><div id=\"root\"></div><script type=\"module\" src=\"/static/react/assets/index-hash.js\"></script><link rel=\"stylesheet\" href=\"/static/react/assets/index-hash.css\"></body></html>",
    )
    canonical = client.get("/controlled/s16", headers=_auth(GOVERNANCE_CREDENTIAL))
    assert canonical.status_code == 200, canonical.text
    assert canonical.headers["cache-control"] == "no-store"
    alias = client.get("/controlled/s16/react", headers=_auth(GOVERNANCE_CREDENTIAL))
    assert alias.status_code == 200
    assert alias.headers["cache-control"] == "no-store"

    # A missing or partial build is a fail-closed no-store 503.
    monkeypatch.setattr(web, "_react_shell_index_html", lambda: None)
    missing = client.get("/controlled/s16", headers=_auth(GOVERNANCE_CREDENTIAL))
    assert missing.status_code == 503
    assert missing.json()["detail"]["error"] == "S16_REACT_UNAVAILABLE"
    assert missing.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# R1 targeted HTTP regressions
# ---------------------------------------------------------------------------


def test_s16_every_domain_and_validation_error_carries_no_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path)
    client = TestClient(_webapp())

    # Unknown application: existence-hiding 404 with no-store.
    missing = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"application_reference": "UNKNOWN-REF", "idempotency_key": "ns-1"},
    )
    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "no-store"
    # Wrong identity: 403 with no-store.
    denied = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_auth(DEMO_CREDENTIAL),
        json={"application_reference": PREFERRED, "idempotency_key": "ns-2"},
    )
    assert denied.status_code == 403
    assert denied.headers["cache-control"] == "no-store"
    # Invalid body: FastAPI validation 422 with no-store (middleware).
    invalid = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"application_reference": PREFERRED, "idempotency_key": ""},
    )
    assert invalid.status_code == 422
    assert invalid.headers["cache-control"] == "no-store"
    # Unavailable plane: 503 with no-store.
    import task4_consistency.web.app as web

    monkeypatch.setattr(web, "S16_SERVICE", None)
    unavailable = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"application_reference": PREFERRED, "idempotency_key": "ns-3"},
    )
    assert unavailable.status_code == 503
    assert unavailable.headers["cache-control"] == "no-store"


def test_s16_legal_hold_http_command_surface(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path)
    client = TestClient(_webapp())
    preflight = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"application_reference": PREFERRED, "idempotency_key": "hold-pre"},
    )
    assert preflight.status_code == 200
    scope_fingerprint = preflight.json()["scope_fingerprint"]

    # Closed vocabulary: invalid reason is a typed 409.
    invalid = client.post(
        "/controlled/s16/api/legal-holds/impose",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={
            "scope_fingerprint": scope_fingerprint,
            "reason_code": "FREE_TEXT_REASON",
            "owner": "s01",
            "effective_time": 1_800_000_000,
            "idempotency_key": "hold-invalid",
        },
    )
    assert invalid.status_code == 422  # closed Literal vocabulary
    assert invalid.headers["cache-control"] == "no-store"

    imposed = client.post(
        "/controlled/s16/api/legal-holds/impose",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={
            "scope_fingerprint": scope_fingerprint,
            "reason_code": "litigation",
            "owner": "all",
            "effective_time": 1_800_000_000,
            "idempotency_key": "hold-1",
        },
    )
    assert imposed.status_code == 200, imposed.text
    assert imposed.json()["status"] == "accepted"
    hold_id = imposed.json()["hold_id"]
    assert imposed.headers["cache-control"] == "no-store"

    # Unknown scope existence-hides.
    unknown_scope = client.post(
        "/controlled/s16/api/legal-holds/impose",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={
            "scope_fingerprint": "0" * 64,
            "reason_code": "litigation",
            "owner": "s01",
            "effective_time": 1_800_000_000,
            "idempotency_key": "hold-unknown-scope",
        },
    )
    assert unknown_scope.status_code == 404
    assert unknown_scope.json()["detail"]["error"] == "S16_NOT_FOUND"

    released = client.post(
        f"/controlled/s16/api/legal-holds/{hold_id}/release",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"idempotency_key": "release-1"},
    )
    assert released.status_code == 200, released.text
    assert released.headers["cache-control"] == "no-store"
    # R3 (P1-6): the release response carries the request id and the
    # monotonic hold generation.
    assert released.json()["request_id"]
    assert isinstance(released.json()["generation"], int)
    assert released.json()["generation"] >= 1
    # Releasing twice is a stable replay.
    replayed = client.post(
        f"/controlled/s16/api/legal-holds/{hold_id}/release",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"idempotency_key": "release-1"},
    )
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] is True
    # R4 (P2-2): the query reports the explicit terminal hold state.
    query = client.get(
        f"/controlled/s16/api/deletions/{preflight.json()['request_id']}",
        headers=_auth(GOVERNANCE_CREDENTIAL),
    )
    assert query.status_code == 200
    holds = query.json()["legal_holds"]
    assert holds and all(
        hold["state"] in {"active", "released", "expired"} for hold in holds
    )
    assert all(hold["hold_id"] != hold_id or hold["state"] == "released" for hold in holds)
    # Unknown hold id existence-hides.
    unknown_hold = client.post(
        "/controlled/s16/api/legal-holds/hold_unknown/release",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"idempotency_key": "release-unknown"},
    )
    assert unknown_hold.status_code == 404


def test_s16_restore_readiness_gate_closes_all_restricted_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After an old-backup restore under the running app, every restricted
    controlled read shares one 503 readiness gate until the runtime replay
    re-deletes the scope."""
    import shutil

    import task4_consistency.web.app as web

    fixture = _install(monkeypatch, tmp_path)
    client = TestClient(_webapp())
    state_path = tmp_path / "target.sqlite3"
    original_db = tmp_path / "original.sqlite3"
    shutil.copy2(state_path, original_db)

    preflight = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"application_reference": PREFERRED, "idempotency_key": "gate-pre"},
    )
    request_id = preflight.json()["request_id"]
    committed = client.post(
        f"/controlled/s16/api/deletions/{request_id}/commit",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"idempotency_key": "gate-commit"},
    )
    assert committed.status_code == 200
    processed = client.post("/controlled/s16/api/process", headers=_auth(GOVERNANCE_CREDENTIAL))
    assert processed.status_code == 200
    assert processed.json()["status"] == "complete"

    # Old backup restored: the gate closes every restricted read.
    shutil.copy2(original_db, state_path)
    assert web.S16_SERVICE is not None
    assert web.S16_SERVICE.ready() is False
    closed_query = client.get(
        f"/controlled/s16/api/deletions/{request_id}",
        headers=_auth(GOVERNANCE_CREDENTIAL),
    )
    assert closed_query.status_code == 503
    assert (
        closed_query.json()["detail"]["error"]
        == "S16_RESTORE_READINESS_UNAVAILABLE"
    )
    assert closed_query.headers["cache-control"] == "no-store"
    # A governed S01 read is closed by the same gate.
    closed_s01 = client.get(
        f"/controlled/s01/api/queries/applications/{fixture[1]}/current-route",
    )
    assert closed_s01.status_code == 503
    assert (
        closed_s01.json()["detail"]["error"]
        == "S16_RESTORE_READINESS_UNAVAILABLE"
    )

    # The runtime replay re-deletes; readiness reopens and the receipt
    # remains readable (append-only).
    replayed = web.S16_SERVICE.replay_restore_if_needed()
    assert replayed["jobs"] >= 1
    assert web.S16_SERVICE.ready() is True
    reopened = client.get(
        f"/controlled/s16/api/deletions/{request_id}/receipt",
        headers=_auth(GOVERNANCE_CREDENTIAL),
    )
    assert reopened.status_code == 200
    assert reopened.json()["result"] == "deleted"


def test_s16_factory_wires_real_audit_seam_and_fails_closed_without_writer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R2 P1-5: the production factory's security-audit availability equals
    'writer configured AND callable'; a missing writer fails protected
    commands closed before any ledger write."""
    import task4_consistency.web.app as web
    from task4_consistency.controlled.s01 import ControlledScenarioService as _S
    from task4_consistency.controlled.s12 import EvaluationService as _E

    recorded: list[dict[str, Any]] = []

    def audit_writer(record: dict[str, Any]) -> bool:
        recorded.append(record)
        return True

    service = ControlledScenarioService(
        fixture_root=FIXTURES,
        rules_path=RULES,
        state_path=tmp_path / "factory.sqlite3",
        audit_writer=audit_writer,
    )
    monkeypatch.setattr(web, "S01_SERVICE", service)
    monkeypatch.setattr(web, "S12_SERVICE", _E(
        state_path=tmp_path / "factory-eval.sqlite3",
        clock=lambda: 1_800_000_000,
        snapshot_provider=lambda *_: None,
        release_provider=lambda *_: None,
        label_manifest_provider=lambda *_: None,
        business_state_provider=lambda: {},
        business_publication_guard=lambda revisions: None,
    ))
    monkeypatch.setattr(web, "S16_GOVERNANCE_CREDENTIAL", GOVERNANCE_CREDENTIAL)
    monkeypatch.setattr(web, "S16_GOVERNANCE_SUBJECT", GOVERNANCE_SUBJECT)
    monkeypatch.setattr(web, "S16_APPROVER1_CREDENTIAL", APPROVER1_CREDENTIAL)
    monkeypatch.setattr(web, "S16_APPROVER1_SUBJECT", APPROVER1_SUBJECT)
    monkeypatch.setattr(web, "S16_APPROVER2_CREDENTIAL", APPROVER2_CREDENTIAL)
    monkeypatch.setattr(web, "S16_APPROVER2_SUBJECT", APPROVER2_SUBJECT)
    independent_root = tmp_path.parent / f"{tmp_path.name}-backups"
    monkeypatch.setenv("TASK4_S16_STATE_PATH", str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setenv("TASK4_S16_BACKUP_ROOT", str(independent_root))
    monkeypatch.setenv("TASK4_S16_SECURITY_AUDIT_AVAILABLE", "true")

    factory_service = web._s16_service_factory()
    assert factory_service is not None
    assert factory_service.security_audit_available is True
    assert factory_service._security_audit_writer is not None

    # Without a writer the seam is unavailable: protected commands fail
    # closed before any ledger fact.
    service_no_writer = ControlledScenarioService(
        fixture_root=FIXTURES,
        rules_path=RULES,
        state_path=tmp_path / "factory-nowriter.sqlite3",
    )
    monkeypatch.setattr(web, "S01_SERVICE", service_no_writer)
    monkeypatch.setenv("TASK4_S16_STATE_PATH", str(tmp_path / "ledger2.sqlite3"))
    closed = web._s16_service_factory()
    assert closed is not None
    assert closed.security_audit_available is False
    from task4_consistency.controlled.s16 import S16_AUDIT_SEAM_UNAVAILABLE

    with pytest.raises(Exception) as excinfo:
        closed.preflight(
            application_reference=PREFERRED,
            principal=S01CommandPrincipal(
                subject=GOVERNANCE_SUBJECT,
                role="operator",
                scope="C-DEMO",
                source_id="s16-governance-console",
            ),
            idempotency_key="factory-no-writer",
        )
    assert str(excinfo.value) == S16_AUDIT_SEAM_UNAVAILABLE


def test_s16_readiness_identity_matrix_three_states(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R5 P2-1: the identity boundary is judged BEFORE readiness — in
    every plane state (ready, configured-but-unavailable, restore-closed)
    an unauthorized caller receives the stable 403, while an authorized
    governance/approver caller receives the plane 503 only when the plane
    is actually unavailable.  All branches carry no-store."""
    import shutil

    import task4_consistency.web.app as web

    fixture = _install(monkeypatch, tmp_path)
    client = TestClient(_webapp())
    state_path = tmp_path / "target.sqlite3"
    original_db = tmp_path / "original.sqlite3"
    shutil.copy2(state_path, original_db)

    non_governance = (
        None,
        OPERATOR_CREDENTIAL,
        DEMO_CREDENTIAL,
        S08_ADMIN_CREDENTIAL,
        REVIEWER_CREDENTIAL,
        APPROVER1_CREDENTIAL,
    )

    # State 1: READY — governance works, everyone else is 403.
    ok = client.post(
        "/controlled/s16/api/deletions/preflight",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"application_reference": PREFERRED, "idempotency_key": "matrix-ready"},
    )
    assert ok.status_code == 200
    for credential in non_governance:
        headers = _auth(credential) if credential is not None else {}
        denied = client.post(
            "/controlled/s16/api/deletions/preflight",
            headers=headers,
            json={"application_reference": PREFERRED, "idempotency_key": f"m-{credential}"},
        )
        assert denied.status_code == 403, (credential, denied.text)
        assert denied.json()["detail"]["error"] == "S16_FORBIDDEN"
        assert denied.headers["cache-control"] == "no-store"
    request_id = ok.json()["request_id"]

    # State 2: CONFIGURED-BUT-UNAVAILABLE — the authorized governance
    # caller sees the stable 503; everyone else still sees 403.
    monkeypatch.setattr(web, "S16_SERVICE", None)
    monkeypatch.setattr(web, "S16_CONFIGURED", True)
    for method, path, body in (
        (
            "post",
            "/controlled/s16/api/deletions/preflight",
            {"application_reference": PREFERRED, "idempotency_key": "matrix-unavail"},
        ),
        ("get", f"/controlled/s16/api/deletions/{request_id}", None),
        ("get", f"/controlled/s16/api/deletions/{request_id}/receipt", None),
    ):
        kwargs = {"headers": _auth(GOVERNANCE_CREDENTIAL)}
        if body is not None:
            kwargs["json"] = body
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 503, (method, path, response.text)
        assert response.json()["detail"]["error"] == "S16_UNAVAILABLE"
        assert response.headers["cache-control"] == "no-store"
    for credential in non_governance:
        headers = _auth(credential) if credential is not None else {}
        denied = client.post(
            "/controlled/s16/api/deletions/preflight",
            headers=headers,
            json={"application_reference": PREFERRED, "idempotency_key": f"mu-{credential}"},
        )
        assert denied.status_code == 403, (credential, denied.text)
        assert denied.json()["detail"]["error"] == "S16_FORBIDDEN"
        assert denied.headers["cache-control"] == "no-store"

    # State 3: RESTORE-CLOSED — authorized callers get the readiness 503,
    # unauthorized callers still get 403 (no state leak).
    web.S16_SERVICE = _build_s16_service(tmp_path, web.S01_SERVICE)
    committed = client.post(
        f"/controlled/s16/api/deletions/{request_id}/commit",
        headers=_auth(GOVERNANCE_CREDENTIAL),
        json={"idempotency_key": "matrix-commit"},
    )
    assert committed.status_code == 200
    processed = client.post(
        "/controlled/s16/api/process",
        headers=_auth(GOVERNANCE_CREDENTIAL),
    )
    assert processed.status_code == 200
    assert processed.json()["status"] == "complete"
    shutil.copy2(original_db, state_path)
    assert web.S16_SERVICE.ready() is False
    closed = client.get(
        f"/controlled/s16/api/deletions/{request_id}",
        headers=_auth(GOVERNANCE_CREDENTIAL),
    )
    assert closed.status_code == 503
    assert (
        closed.json()["detail"]["error"]
        == "S16_RESTORE_READINESS_UNAVAILABLE"
    )
    assert closed.headers["cache-control"] == "no-store"
    for credential in non_governance:
        headers = _auth(credential) if credential is not None else {}
        denied = client.get(
            f"/controlled/s16/api/deletions/{request_id}",
            headers=headers,
        )
        assert denied.status_code == 403, (credential, denied.text)
        assert denied.json()["detail"]["error"] == "S16_FORBIDDEN"
        assert denied.headers["cache-control"] == "no-store"
