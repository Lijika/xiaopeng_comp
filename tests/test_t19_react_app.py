"""Ticket #53 / T19 FastAPI fixture for the controlled-export tracer."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from task4_consistency.controlled.s17 import (
    GovernedExportService,
    RecipientRegistration,
    RecipientRegistry,
    _digest,
)
from tests.test_s17_controlled import (
    FakeExportSource,
    InMemoryRegisteredExportDelivery,
    RecordingEncryptionProvider,
    RecordingWatermarkProvider,
)

SCOPE = "C-DEMO"
SCOPE_REFERENCE = "APP-REF-1"
REQUESTER_SUBJECT = "t19-requester"
REQUESTER_CREDENTIAL = "t19-requester-credential"
APPROVER_SUBJECT = "t19-approver"
APPROVER_CREDENTIAL = "t19-approver-credential"
WORKER_SUBJECT = "t19-worker"
WORKER_CREDENTIAL = "t19-worker-credential"
RECIPIENT_SUBJECT = "s17-recipient-1"
RECIPIENT_CREDENTIAL = "t19-recipient-credential"


class FileTokenDelivery(InMemoryRegisteredExportDelivery):
    def __init__(self, token_path: Path) -> None:
        super().__init__()
        self.token_path = token_path

    def deliver(self, request: Any) -> dict[str, Any]:
        result = super().deliver(request)
        assert self.last_token is not None
        self.token_path.write_text(self.last_token, encoding="utf-8")
        return result


class FixtureExportService(GovernedExportService):
    """Adapt the domain result to the released closed HTTP command DTOs."""

    _COMMAND_FIELDS = {
        "status",
        "request_id",
        "job_id",
        "package_id",
        "receipt_id",
        "attempt",
        "reason_code",
        "replayed",
    }

    @classmethod
    def _command(cls, result: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in result.items() if key in cls._COMMAND_FIELDS}

    def approve(self, **kwargs: Any) -> dict[str, Any]:
        return self._command(super().approve(**kwargs))

    def commit(self, **kwargs: Any) -> dict[str, Any]:
        return self._command(super().commit(**kwargs))

    def process_next_export(self, **kwargs: Any) -> dict[str, Any]:
        return self._command(super().process_next_export(**kwargs))

    def access(self, **kwargs: Any) -> dict[str, Any]:
        return self._command(super().access(**kwargs))

    def confirm(self, **kwargs: Any) -> dict[str, Any]:
        return self._command(super().confirm(**kwargs))

    def revoke(self, **kwargs: Any) -> dict[str, Any]:
        return self._command(super().revoke(**kwargs))

    def expire(self, **kwargs: Any) -> dict[str, Any]:
        return self._command(super().expire(**kwargs))

    def reconcile(self, **kwargs: Any) -> dict[str, Any]:
        return self._command(super().reconcile(**kwargs))


def _registration() -> RecipientRegistration:
    channel_id = "t19-registered-channel"
    return RecipientRegistration(
        recipient_id=RECIPIENT_SUBJECT,
        channel_id=channel_id,
        registration_digest=_digest(
            {
                "schema_version": "s17-recipient/1",
                "recipient_id": RECIPIENT_SUBJECT,
                "channel_id": channel_id,
            }
        ),
        allowed_classifications=("internal", "confidential", "restricted"),
    )


def _service(work_root: Path) -> GovernedExportService:
    source = FakeExportSource()
    source.register(tenant=SCOPE, reference=SCOPE_REFERENCE)
    return FixtureExportService(
        ledger_path=work_root / "s17.sqlite3",
        requester_subject=REQUESTER_SUBJECT,
        approver_subject=APPROVER_SUBJECT,
        worker_id=WORKER_SUBJECT,
        export_scope=SCOPE,
        recipient_registry=RecipientRegistry((_registration(),)),
        export_source=source,
        encryption_provider=RecordingEncryptionProvider(),
        watermark_provider=RecordingWatermarkProvider(),
        delivery=FileTokenDelivery(work_root / "delivery.token"),
        security_audit_available=True,
        security_audit_writer=lambda _record: True,
        storage_available=True,
        clock=lambda: int(time.time()),
    )


def create_t19_react_test_app():
    import task4_consistency.web.app as web

    work_root = Path(os.environ["TASK4_T19_FIXTURE_ROOT"])
    work_root.mkdir(parents=True, exist_ok=True)
    web.S01_REQUIRE_CONFIGURED_STARTUP = False
    web.S01_BACKGROUND_ENABLED = False
    web.S17_SERVICE = _service(work_root)
    web.S17_REQUESTER_SUBJECT = REQUESTER_SUBJECT
    web.S17_REQUESTER_CREDENTIAL = REQUESTER_CREDENTIAL
    web.S17_APPROVER_SUBJECT = APPROVER_SUBJECT
    web.S17_APPROVER_CREDENTIAL = APPROVER_CREDENTIAL
    web.S17_WORKER_SUBJECT = WORKER_SUBJECT
    web.S17_WORKER_CREDENTIAL = WORKER_CREDENTIAL
    web.S17_RECIPIENT_SUBJECT = RECIPIENT_SUBJECT
    web.S17_RECIPIENT_CREDENTIAL = RECIPIENT_CREDENTIAL
    web.S17_EXPORT_SCOPE = SCOPE
    web.S17_CONFIGURATION_ERROR = None
    react_dir = os.environ.get("TASK4_T19_REACT_DIR", "").strip()
    web.S17_REACT_INDEX = (
        Path(react_dir).resolve() / "index.html"
        if react_dir
        else web.S01_REACT_STATIC / "index.html"
    )
    (work_root / "fixture.json").write_text(
        json.dumps(
            {
                "scope_reference": SCOPE_REFERENCE,
                "requester_credential": REQUESTER_CREDENTIAL,
                "approver_credential": APPROVER_CREDENTIAL,
                "worker_credential": WORKER_CREDENTIAL,
                "recipient_credential": RECIPIENT_CREDENTIAL,
                "token_path": str(work_root / "delivery.token"),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return web.app


def test_t19_fixture_runs_the_fixed_request_and_one_time_delivery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TASK4_T19_FIXTURE_ROOT", str(tmp_path))
    client = TestClient(create_t19_react_test_app())
    requester = {"Authorization": f"Bearer {REQUESTER_CREDENTIAL}"}
    preview = client.post(
        "/controlled/s17/api/exports/preview",
        headers=requester,
        json={
            "purpose": "audit_response",
            "fields": ["application_fingerprint"],
            "artifacts": ["route_metadata"],
            "recipient_id": RECIPIENT_SUBJECT,
            "classification": "confidential",
            "expiry": int(time.time()) + 3600,
            "scope_reference": SCOPE_REFERENCE,
            "idempotency_key": "t19-preview",
        },
    )
    assert preview.status_code == 200
    fixed = preview.json()
    request_id = fixed["request_id"]
    queried = client.get(f"/controlled/s17/api/exports/{request_id}", headers=requester)
    assert queried.status_code == 200
    assert queried.json()["fields"] == ["application_fingerprint"]
    assert queried.json()["artifacts"] == ["route_metadata"]
    assert queried.json()["scope_reference"] == SCOPE_REFERENCE
    approved = client.post(
        f"/controlled/s17/api/exports/{request_id}/approve",
        headers={
            **requester,
            "X-S17-Approver-Token": f"Bearer {APPROVER_CREDENTIAL}",
        },
        json={
            "preview_digest": fixed["preview_digest"],
            "idempotency_key": "t19-approve",
        },
    )
    assert approved.json()["status"] == "approved"
    assert client.post(
        f"/controlled/s17/api/exports/{request_id}/commit",
        headers=requester,
        json={"idempotency_key": "t19-commit"},
    ).json()["status"] == "queued"
    assert client.post(
        "/controlled/s17/api/process",
        headers={"Authorization": f"Bearer {WORKER_CREDENTIAL}"},
    ).json()["status"] == "delivered"
    token = (tmp_path / "delivery.token").read_text(encoding="utf-8")
    recipient = {"Authorization": f"Bearer {RECIPIENT_CREDENTIAL}"}
    access = client.post(
        f"/controlled/s17/api/exports/{request_id}/access",
        headers=recipient,
        json={"token": token},
    )
    assert access.json()["status"] == "accessed"
    replay = client.post(
        f"/controlled/s17/api/exports/{request_id}/access",
        headers=recipient,
        json={"token": token},
    )
    assert replay.status_code == 409
    assert replay.json()["detail"]["reason_code"] == "S17_TOKEN_REPLAY"
    assert client.post(
        f"/controlled/s17/api/exports/{request_id}/confirm",
        headers={**recipient, "Idempotency-Key": "t19-confirm"},
    ).json()["status"] == "confirmed"
    receipt = client.get(
        f"/controlled/s17/api/exports/{request_id}/receipt",
        headers=requester,
    ).json()
    assert receipt["status"] == "confirmed"
    assert receipt["cleanup_result"] == "none"
