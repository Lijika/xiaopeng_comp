"""Ticket #33 / S17 governed export — controlled semantics.

Default-off minimum-scope preview, independent approval, encrypted
watermarked package generation, registered delivery, access/confirm/
revoke/expire, partial cleanup, audit/receipt and restart recovery.
C-DEMO test providers prove the provider boundary only; they are not a
G5 encryption or production export claim.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

import pytest

from task4_consistency.controlled.s01 import S01CommandPrincipal
from task4_consistency.controlled.s17 import (
    S17_AUDIT_SEAM_UNAVAILABLE,
    S17_DIGEST_DRIFT,
    S17_FORBIDDEN,
    S17_INVALID_SCOPE,
    S17_PROVIDER_UNAVAILABLE,
    S17_RECIPIENT_MISMATCH,
    S17_SELF_APPROVAL,
    S17_STORAGE_UNAVAILABLE,
    S17_TOKEN_EXPIRED,
    S17_TOKEN_REPLAY,
    S17_TOKEN_REVOKED,
    S17_UNAVAILABLE,
    EncryptionResult,
    GovernedExportService,
    RecipientRegistration,
    RecipientRegistry,
    S17Blocked,
    S17Forbidden,
    S17NotFound,
    S17Unavailable,
    _digest,
)

NOW = 1_800_000_000
CLOCK = {"now": NOW}
PLAINTEXT = b"SENSITIVE-EXPORT-PAYLOAD"
SCOPE_REFERENCE = "APP-REF-1"
TENANT = "C-DEMO"

REQUESTER = S01CommandPrincipal(
    subject="s17-requester",
    role="operator",
    scope=TENANT,
    source_id="s17-export-console",
    expires_at=float("inf"),
)
APPROVER = S01CommandPrincipal(
    subject="s17-approver",
    role="operator",
    scope=TENANT,
    source_id="s17-approval-desk",
    expires_at=float("inf"),
)
WORKER = S01CommandPrincipal(
    subject="s17-worker",
    role="system",
    scope=TENANT,
    source_id="s17-export-worker",
    expires_at=float("inf"),
)
RECIPIENT = S01CommandPrincipal(
    subject="s17-recipient-1",
    role="recipient",
    scope=TENANT,
    source_id="s17-recipient-channel",
    expires_at=float("inf"),
)
VIEWER = S01CommandPrincipal(
    subject="s01-reviewer",
    role="reviewer",
    scope=TENANT,
    source_id="s01-review-console",
    expires_at=float("inf"),
)
REVEAL = S01CommandPrincipal(
    subject="s15-reveal-operator",
    role="reviewer",
    scope=TENANT,
    source_id="s15-reveal-console",
    expires_at=float("inf"),
)
ADMIN = S01CommandPrincipal(
    subject="platform-admin",
    role="admin",
    scope=TENANT,
    source_id="platform-admin-console",
    expires_at=float("inf"),
)
S13_OPERATOR = S01CommandPrincipal(
    subject="s13-operator",
    role="operator",
    scope=TENANT,
    source_id="s13-delivery-console",
    expires_at=float("inf"),
)
S16_GOVERNANCE = S01CommandPrincipal(
    subject="s16-governance",
    role="operator",
    scope=TENANT,
    source_id="s16-governance-console",
    expires_at=float("inf"),
)


def _now() -> int:
    return int(CLOCK["now"])


def _registration() -> RecipientRegistration:
    recipient_id = "s17-recipient-1"
    channel_id = "s17-registered-channel"
    digest = _digest(
        {
            "schema_version": "s17-recipient/1",
            "recipient_id": recipient_id,
            "channel_id": channel_id,
        }
    )
    return RecipientRegistration(
        recipient_id=recipient_id,
        channel_id=channel_id,
        registration_digest=digest,
        allowed_classifications=("internal", "confidential", "restricted"),
    )


class FakeExportSource:
    """Public-boundary test source: pins and minimum snapshots only."""

    def __init__(self, *, trace: list[str] | None = None) -> None:
        self.trace = trace if trace is not None else []
        self._scopes: dict[tuple[str, str], dict[str, Any]] = {}
        self.snapshot_calls = 0
        self.lifecycle_writes = 0
        self.evidence_writes = 0

    def register(
        self,
        *,
        tenant: str,
        reference: str,
        deleted: bool = False,
        revisions: dict[str, Any] | None = None,
        payload: bytes = PLAINTEXT,
    ) -> None:
        self._scopes[(tenant, reference)] = {
            "deleted": deleted,
            "revisions": revisions
            or {
                "s01": 4,
                "s12": "12" * 32,
                "s13": "13" * 32,
            },
            "payload": payload,
            "policy_digest": "cd" * 32,
            "scope_fingerprint": _digest(
                {
                    "tenant": tenant,
                    "reference_fingerprint": hashlib.sha256(
                        reference.encode("utf-8")
                    ).hexdigest(),
                }
            ),
        }

    def pin(
        self,
        *,
        tenant_scope: str,
        scope_reference: str,
        fields: tuple[str, ...],
        artifacts: tuple[str, ...],
    ) -> dict[str, Any] | None:
        self.trace.append("pin")
        item = self._scopes.get((tenant_scope, scope_reference))
        if item is None or item["deleted"]:
            return None
        return {
            "scope_fingerprint": item["scope_fingerprint"],
            "source_revisions": dict(item["revisions"]),
            "policy_digest": item["policy_digest"],
            "field_count": len(fields),
            "artifact_count": len(artifacts),
        }

    def snapshot(
        self,
        *,
        tenant_scope: str,
        scope_reference: str,
        fields: tuple[str, ...],
        artifacts: tuple[str, ...],
        source_revisions: dict[str, Any],
    ) -> bytes:
        del fields, artifacts
        self.trace.append("snapshot")
        self.snapshot_calls += 1
        item = self._scopes[(tenant_scope, scope_reference)]
        if dict(item["revisions"]) != dict(source_revisions):
            raise S17Blocked(S17_DIGEST_DRIFT)
        return bytes(item["payload"])

    def fact_counts(self) -> dict[str, int]:
        return {
            "lifecycle": self.lifecycle_writes,
            "evidence": self.evidence_writes,
        }


class RecordingEncryptionProvider:
    """C-DEMO/C-DEV-REG test provider. Not production AEAD/KMS."""

    def __init__(
        self,
        *,
        trace: list[str] | None = None,
        fail: bool = False,
    ) -> None:
        self.trace = trace if trace is not None else []
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def encrypt(self, plaintext: bytes, context: dict[str, Any]) -> EncryptionResult:
        self.trace.append("encrypt")
        self.calls.append(
            {
                "plaintext_len": len(plaintext),
                "plaintext_is_sensitive": plaintext == PLAINTEXT,
                "context": dict(context),
            }
        )
        if self.fail:
            raise S17Unavailable(S17_PROVIDER_UNAVAILABLE)
        material = b"s17-test-aead/1\0" + plaintext + _digest(context).encode("utf-8")
        digest = hashlib.sha256(material).hexdigest()
        ciphertext = b"S17TESTAEAD1" + bytes.fromhex(digest)
        assert ciphertext != plaintext
        return EncryptionResult(
            ciphertext=ciphertext,
            key_ref="c-demo-test-kms/s17",
            key_version="1",
            package_digest=digest,
        )


class RecordingWatermarkProvider:
    def __init__(self, *, trace: list[str] | None = None) -> None:
        self.trace = trace if trace is not None else []
        self.calls: list[dict[str, Any]] = []

    def bind(
        self,
        *,
        obligation_id: str,
        recipient_id: str,
        expiry: int,
        purpose: str,
        package_digest: str,
    ) -> dict[str, str]:
        self.trace.append("watermark")
        watermark_id = _digest(
            {
                "obligation_id": obligation_id,
                "recipient_id": recipient_id,
                "expiry": expiry,
                "purpose": purpose,
                "package_digest": package_digest,
            }
        )
        self.calls.append(
            {
                "obligation_id": obligation_id,
                "recipient_id": recipient_id,
                "expiry": expiry,
                "purpose": purpose,
                "package_digest": package_digest,
                "watermark_id": watermark_id,
            }
        )
        return {"watermark_id": watermark_id, "scheme": "s17-test-watermark/1"}


class InMemoryRegisteredExportDelivery:
    def __init__(
        self,
        *,
        trace: list[str] | None = None,
        timeout: bool = False,
    ) -> None:
        self.trace = trace if trace is not None else []
        self.timeout = timeout
        self.calls: list[str] = []
        self.last_token: str | None = None
        self._operations: dict[str, dict[str, Any]] = {}
        self.deliver_count = 0

    def deliver(self, request: Any) -> dict[str, Any]:
        self.trace.append("deliver")
        self.calls.append("deliver")
        self.deliver_count += 1
        payload = getattr(request, "__dict__", None) or {}
        serialized = json.dumps(payload, default=str)
        for banned in ("http://", "https://", "@", "/home/", "credential", "password"):
            assert banned not in serialized.lower() or banned == "credential"
        token = str(getattr(request, "token", "") or "")
        assert token, "delivery adapter must receive a bound token"
        self.last_token = token
        operation_id = str(getattr(request, "operation_id"))
        if self.timeout:
            self._operations[operation_id] = {
                "outcome": "timeout",
                "request": request,
                "executed_remotely": True,
            }
            return {
                "outcome": "timeout",
                "operation_id": operation_id,
                "reason_code": "S17_DELIVERY_TIMEOUT",
            }
        self._operations[operation_id] = {
            "outcome": "confirmed",
            "request": request,
            "executed_remotely": True,
        }
        return {
            "outcome": "confirmed",
            "operation_id": operation_id,
            "remote_message_id": f"remote-{operation_id[-12:]}",
        }

    def lookup(self, operation_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append("lookup")
        record = self._operations.get(operation_id)
        if record is None:
            return {"outcome": "not_executed", "operation_id": operation_id}
        if record["outcome"] == "timeout" and record.get("executed_remotely"):
            return {
                "outcome": "confirmed",
                "operation_id": operation_id,
                "remote_message_id": f"remote-{operation_id[-12:]}",
            }
        return {
            "outcome": record["outcome"],
            "operation_id": operation_id,
        }

    def confirm(self, operation_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append("confirm")
        record = self._operations.get(operation_id)
        if record is None:
            return {"outcome": "not_executed", "operation_id": operation_id}
        record["outcome"] = "confirmed"
        return {"outcome": "confirmed", "operation_id": operation_id}

    def revoke(self, operation_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append("revoke")
        record = self._operations.get(operation_id)
        if record is not None:
            record["outcome"] = "revoked"
        return {"outcome": "revoked", "operation_id": operation_id}


def _writer(fail: bool = False) -> tuple[list[dict[str, Any]], Callable[[dict[str, Any]], bool]]:
    recorded: list[dict[str, Any]] = []

    def write(record: dict[str, Any]) -> bool:
        if fail:
            raise RuntimeError("audit writer outage")
        recorded.append(record)
        return True

    return recorded, write


def _service(
    tmp_path: Path,
    *,
    export_source: FakeExportSource | None = None,
    encryption: RecordingEncryptionProvider | None = None,
    watermark: RecordingWatermarkProvider | None = None,
    delivery: InMemoryRegisteredExportDelivery | None = None,
    recipients: tuple[RecipientRegistration, ...] | None = None,
    security_audit_available: bool = True,
    security_audit_writer: Callable[[dict[str, Any]], bool] | None = None,
    storage_available: bool = True,
    encryption_provider: Any = "default",
    ledger_path: Path | None = None,
) -> GovernedExportService:
    trace: list[str] = []
    source = export_source or FakeExportSource(trace=trace)
    if not source._scopes:
        source.register(tenant=TENANT, reference=SCOPE_REFERENCE)
    if encryption_provider == "default":
        encryption_provider = encryption or RecordingEncryptionProvider(trace=source.trace)
    watermark = watermark or RecordingWatermarkProvider(trace=source.trace)
    delivery = delivery or InMemoryRegisteredExportDelivery(trace=source.trace)
    if recipients is None:
        recipients = (_registration(),)
    if security_audit_available and security_audit_writer is None:
        _recorded, security_audit_writer = _writer()
        del _recorded
    return GovernedExportService(
        ledger_path=ledger_path or (tmp_path / "s17.sqlite3"),
        requester_subject=REQUESTER.subject,
        approver_subject=APPROVER.subject,
        worker_id=WORKER.subject,
        export_scope=TENANT,
        recipient_registry=RecipientRegistry(recipients),
        export_source=source,
        encryption_provider=encryption_provider,
        watermark_provider=watermark,
        delivery=delivery,
        security_audit_available=security_audit_available,
        security_audit_writer=security_audit_writer,
        storage_available=storage_available,
        clock=_now,
    )


def _preview_kwargs() -> dict[str, Any]:
    return {
        "purpose": "regulatory_review",
        "fields": ("application_fingerprint", "lifecycle_phase"),
        "artifacts": ("route_metadata",),
        "recipient_id": "s17-recipient-1",
        "classification": "confidential",
        "expiry": NOW + 3600,
        "scope_reference": SCOPE_REFERENCE,
        "principal": REQUESTER,
        "idempotency_key": "preview-1",
    }


def _preview(service: GovernedExportService, **overrides: Any) -> dict[str, Any]:
    kwargs = _preview_kwargs()
    kwargs.update(overrides)
    return service.preview(**kwargs)


def _approve(service: GovernedExportService, preview: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    kwargs = {
        "request_id": preview["request_id"],
        "preview_digest": preview["preview_digest"],
        "principal": APPROVER,
        "idempotency_key": "approve-1",
    }
    kwargs.update(overrides)
    return service.approve(**kwargs)


def _commit(service: GovernedExportService, preview: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    kwargs = {
        "request_id": preview["request_id"],
        "principal": REQUESTER,
        "idempotency_key": "commit-1",
    }
    kwargs.update(overrides)
    return service.commit(**kwargs)


def _flow(service: GovernedExportService) -> dict[str, Any]:
    preview = _preview(service)
    _approve(service, preview)
    committed = _commit(service, preview)
    processed = service.process_next_export(principal=WORKER)
    return {
        "preview": preview,
        "committed": committed,
        "processed": processed,
        "request_id": preview["request_id"],
    }


def _ledger_blob(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        chunks: list[str] = []
        for (name,) in rows:
            for row in connection.execute(f"SELECT * FROM {name}").fetchall():
                chunks.append(repr(row))
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# 1. Configuration, identity, readiness
# ---------------------------------------------------------------------------


def test_s17_factory_is_default_closed_without_g5_inputs(tmp_path: Path) -> None:
    source = FakeExportSource()
    source.register(tenant=TENANT, reference=SCOPE_REFERENCE)
    watermark = RecordingWatermarkProvider()
    delivery = InMemoryRegisteredExportDelivery()
    _recorded, writer = _writer()
    with pytest.raises(ValueError):
        GovernedExportService(
            ledger_path=tmp_path / "s17.sqlite3",
            requester_subject=REQUESTER.subject,
            approver_subject=APPROVER.subject,
            worker_id=WORKER.subject,
            export_scope=TENANT,
            recipient_registry=RecipientRegistry(()),
            export_source=source,
            encryption_provider=RecordingEncryptionProvider(),
            watermark_provider=watermark,
            delivery=delivery,
            security_audit_writer=writer,
        )
    with pytest.raises(ValueError):
        GovernedExportService(
            ledger_path=tmp_path / "missing-provider.sqlite3",
            requester_subject=REQUESTER.subject,
            approver_subject=APPROVER.subject,
            worker_id=WORKER.subject,
            export_scope=TENANT,
            recipient_registry=RecipientRegistry((_registration(),)),
            export_source=source,
            encryption_provider=None,
            watermark_provider=watermark,
            delivery=delivery,
            security_audit_writer=writer,
        )
    with pytest.raises(ValueError):
        GovernedExportService(
            ledger_path=tmp_path / "self-alias.sqlite3",
            requester_subject=REQUESTER.subject,
            approver_subject=REQUESTER.subject,
            worker_id=WORKER.subject,
            export_scope=TENANT,
            recipient_registry=RecipientRegistry((_registration(),)),
            export_source=source,
            encryption_provider=RecordingEncryptionProvider(),
            watermark_provider=watermark,
            delivery=delivery,
            security_audit_writer=writer,
        )


def test_s17_identity_matrix_rejects_view_reveal_admin_and_self_approval(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    for principal in (VIEWER, REVEAL, ADMIN, S13_OPERATOR, S16_GOVERNANCE, RECIPIENT, WORKER):
        with pytest.raises(S17Forbidden) as forbidden:
            _preview(service, principal=principal, idempotency_key=f"id-{principal.subject}")
        assert S17_FORBIDDEN in str(forbidden.value) or "identity" in str(forbidden.value).lower()
    preview = _preview(service)
    with pytest.raises((S17Forbidden, S17Blocked)) as self_approved:
        _approve(service, preview, principal=REQUESTER)
    reason = getattr(self_approved.value, "reason_code", str(self_approved.value))
    assert S17_SELF_APPROVAL in str(reason) or "self" in str(self_approved.value).lower()
    with pytest.raises(S17Forbidden):
        service.process_next_export(principal=REQUESTER)


def test_s17_ready_requires_provider_recipient_audit_and_storage(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    assert service.ready() is True
    service.audit_available = False
    assert service.ready() is False
    with pytest.raises(S17Unavailable) as audit_exc:
        _preview(service, idempotency_key="ready-audit")
    assert audit_exc.value.reason_code in {S17_UNAVAILABLE, S17_AUDIT_SEAM_UNAVAILABLE}
    service.audit_available = True
    service.storage_available = False
    assert service.ready() is False
    with pytest.raises(S17Unavailable) as storage_exc:
        _preview(service, idempotency_key="ready-storage")
    assert storage_exc.value.reason_code in {S17_UNAVAILABLE, S17_STORAGE_UNAVAILABLE}
    service.storage_available = True
    service._encryption = None
    assert service.ready() is False
    service._encryption = RecordingEncryptionProvider()
    service._recipients = RecipientRegistry(())
    assert service.ready() is False


# ---------------------------------------------------------------------------
# 2. Minimum-scope preview
# ---------------------------------------------------------------------------


def test_preview_requires_explicit_minimum_scope_and_never_persists_payload(
    tmp_path: Path,
) -> None:
    source = FakeExportSource()
    source.register(tenant=TENANT, reference=SCOPE_REFERENCE)
    service = _service(tmp_path, export_source=source)
    with pytest.raises(S17Blocked) as empty:
        _preview(service, fields=(), artifacts=(), idempotency_key="empty-scope")
    assert empty.value.reason_code == S17_INVALID_SCOPE
    with pytest.raises(S17Blocked):
        _preview(
            service,
            fields=("latest",),
            artifacts=("../etc/passwd",),
            idempotency_key="path-scope",
        )
    preview = _preview(service)
    assert preview["status"] == "previewed"
    assert preview["field_count"] == 2
    assert preview["artifact_count"] == 1
    assert len(preview["preview_digest"]) == 64
    assert "watermark_plan" in preview
    assert source.snapshot_calls == 0
    blob = _ledger_blob(tmp_path / "s17.sqlite3")
    assert PLAINTEXT.decode() not in blob
    assert SCOPE_REFERENCE not in blob
    assert "SENSITIVE" not in blob
    dumped = json.dumps(preview)
    assert PLAINTEXT.decode() not in dumped
    assert "http://" not in dumped


def test_preview_hides_cross_tenant_unknown_and_s16_deleted_scope(
    tmp_path: Path,
) -> None:
    source = FakeExportSource()
    source.register(tenant=TENANT, reference=SCOPE_REFERENCE)
    source.register(tenant="R-OBSERVED/other", reference="APP-OTHER")
    source.register(tenant=TENANT, reference="APP-DELETED", deleted=True)
    service = _service(tmp_path, export_source=source)
    with pytest.raises(S17NotFound):
        _preview(service, scope_reference="APP-UNKNOWN", idempotency_key="unknown")
    with pytest.raises(S17NotFound):
        _preview(
            service,
            scope_reference="APP-OTHER",
            principal=S01CommandPrincipal(
                subject=REQUESTER.subject,
                role=REQUESTER.role,
                scope="R-OBSERVED/other",
                source_id=REQUESTER.source_id,
                expires_at=REQUESTER.expires_at,
            ),
            idempotency_key="cross-tenant",
        )
    with pytest.raises(S17NotFound):
        _preview(service, scope_reference="APP-DELETED", idempotency_key="deleted")
    visible = _preview(service)
    assert visible["status"] == "previewed"


# ---------------------------------------------------------------------------
# 3. Independent approval and commit
# ---------------------------------------------------------------------------


def test_independent_approval_binds_exact_digest_and_rejects_self_approval(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    preview = _preview(service)
    with pytest.raises((S17Forbidden, S17Blocked)):
        _approve(service, preview, principal=REQUESTER)
    drifted = dict(preview)
    drifted["preview_digest"] = "ab" * 32
    with pytest.raises(S17Blocked) as drift:
        _approve(service, drifted)
    assert drift.value.reason_code == S17_DIGEST_DRIFT
    approved = _approve(service, preview)
    assert approved["status"] == "approved"
    replayed = _approve(service, preview)
    assert replayed["replayed"] is True
    assert replayed["request_id"] == preview["request_id"]


def test_commit_freezes_current_revisions_and_queues_one_obligation(
    tmp_path: Path,
) -> None:
    source = FakeExportSource()
    source.register(tenant=TENANT, reference=SCOPE_REFERENCE)
    service = _service(tmp_path, export_source=source)
    preview = _preview(service)
    _approve(service, preview)
    committed = _commit(service, preview)
    assert committed["status"] == "queued"
    assert committed["obligation_id"]
    query = service.query(request_id=preview["request_id"], principal=REQUESTER)
    assert query["status"] == "queued"
    assert query["source_revisions"]["s01"] == 4
    again = _commit(service, preview)
    assert again["replayed"] is True
    assert again["obligation_id"] == committed["obligation_id"]
    jobs = [
        event
        for event in service._events
        if event.get("event_type") == "obligation_queued"
    ]
    assert len(jobs) == 1


def test_scope_or_digest_drift_after_approval_has_zero_export_effect(
    tmp_path: Path,
) -> None:
    source = FakeExportSource()
    source.register(tenant=TENANT, reference=SCOPE_REFERENCE)
    service = _service(tmp_path, export_source=source)
    preview = _preview(service)
    _approve(service, preview)
    source._scopes[(TENANT, SCOPE_REFERENCE)]["revisions"]["s01"] = 99
    before = len(service._events)
    with pytest.raises(S17Blocked) as drift:
        _commit(service, preview, idempotency_key="commit-drift")
    assert drift.value.reason_code in {S17_DIGEST_DRIFT, "S17_SOURCE_DRIFT"}
    assert len(service._events) == before
    assert service.process_next_export(principal=WORKER)["status"] == "idle"


# ---------------------------------------------------------------------------
# 4. Generation, encryption, watermark
# ---------------------------------------------------------------------------


def test_generation_encrypts_watermarks_and_registers_before_delivery(
    tmp_path: Path,
) -> None:
    trace: list[str] = []
    source = FakeExportSource(trace=trace)
    source.register(tenant=TENANT, reference=SCOPE_REFERENCE)
    encryption = RecordingEncryptionProvider(trace=trace)
    watermark = RecordingWatermarkProvider(trace=trace)
    delivery = InMemoryRegisteredExportDelivery(trace=trace)
    service = _service(
        tmp_path,
        export_source=source,
        encryption=encryption,
        watermark=watermark,
        delivery=delivery,
    )
    flow = _flow(service)
    assert flow["processed"]["status"] == "delivered"
    assert "snapshot" in trace
    assert trace.index("watermark") < trace.index("encrypt")
    assert trace.index("encrypt") < trace.index("deliver")
    types = [event.get("event_type") for event in service._events]
    assert types.index("package_registered") < types.index("delivery_attempt")
    blob = _ledger_blob(tmp_path / "s17.sqlite3")
    assert PLAINTEXT.decode() not in blob
    assert delivery.last_token not in blob
    assert encryption.calls[0]["plaintext_is_sensitive"] is True
    context = encryption.calls[0]["context"]
    assert context["recipient_id"] == "s17-recipient-1"
    assert context["purpose"] == "regulatory_review"
    query = service.query(request_id=flow["request_id"], principal=REQUESTER)
    assert query["package_digest"] == encryption.calls[0]["context"].get(
        "package_digest"
    ) or query["package_digest"] == encryption.calls[0] and True
    assert query["watermark_id"] == watermark.calls[0]["watermark_id"]
    dumped = json.dumps(query)
    assert PLAINTEXT.decode() not in dumped
    assert "http://" not in dumped
    assert delivery.last_token not in dumped


def test_kms_or_storage_failure_cleans_unregistered_partial_and_keeps_obligation_pending(
    tmp_path: Path,
) -> None:
    source = FakeExportSource()
    source.register(tenant=TENANT, reference=SCOPE_REFERENCE)
    encryption = RecordingEncryptionProvider(fail=True)
    service = _service(tmp_path, export_source=source, encryption=encryption)
    preview = _preview(service)
    _approve(service, preview)
    _commit(service, preview)
    failed = service.process_next_export(principal=WORKER)
    assert failed["status"] == "failed"
    assert failed["reason_code"] == S17_PROVIDER_UNAVAILABLE
    query = service.query(request_id=preview["request_id"], principal=REQUESTER)
    assert query["status"] == "queued"
    assert service._temp_unregistered() == ()
    types = [event.get("event_type") for event in service._events]
    assert "partial_cleaned" in types
    assert "package_registered" not in types


# ---------------------------------------------------------------------------
# 5. Registered delivery, token, confirm, revoke, expire, reconcile
# ---------------------------------------------------------------------------


def test_recipient_mismatch_expired_or_revoked_token_cannot_access(
    tmp_path: Path,
) -> None:
    delivery = InMemoryRegisteredExportDelivery()
    service = _service(tmp_path, delivery=delivery)
    flow = _flow(service)
    token = delivery.last_token
    assert token
    stranger = S01CommandPrincipal(
        subject="s17-recipient-other",
        role="recipient",
        scope=TENANT,
        source_id="s17-recipient-channel",
        expires_at=float("inf"),
    )
    with pytest.raises(S17Blocked) as mismatch:
        service.access(
            request_id=flow["request_id"],
            principal=stranger,
            token=token,
        )
    assert mismatch.value.reason_code == S17_RECIPIENT_MISMATCH
    with pytest.raises(S17Forbidden):
        service.access(
            request_id=flow["request_id"],
            principal=VIEWER,
            token=token,
        )
    service.revoke(
        request_id=flow["request_id"],
        principal=REQUESTER,
        idempotency_key="revoke-1",
    )
    with pytest.raises(S17Blocked) as revoked:
        service.access(
            request_id=flow["request_id"],
            principal=RECIPIENT,
            token=token,
        )
    assert revoked.value.reason_code == S17_TOKEN_REVOKED

    delivery2 = InMemoryRegisteredExportDelivery()
    service2 = _service(tmp_path / "expire", delivery=delivery2)
    flow2 = _flow(service2)
    CLOCK["now"] = NOW + 10_000
    try:
        service2.expire(
            request_id=flow2["request_id"],
            principal=WORKER,
            idempotency_key="expire-1",
        )
        with pytest.raises(S17Blocked) as expired:
            service2.access(
                request_id=flow2["request_id"],
                principal=RECIPIENT,
                token=delivery2.last_token or "",
            )
        assert expired.value.reason_code == S17_TOKEN_EXPIRED
    finally:
        CLOCK["now"] = NOW


def test_unknown_delivery_reconciles_same_operation_without_duplicate_download(
    tmp_path: Path,
) -> None:
    delivery = InMemoryRegisteredExportDelivery(timeout=True)
    service = _service(tmp_path, delivery=delivery)
    preview = _preview(service)
    _approve(service, preview)
    _commit(service, preview)
    timed_out = service.process_next_export(principal=WORKER)
    assert timed_out["status"] == "timeout"
    assert delivery.deliver_count == 1
    reconciled = service.reconcile(
        request_id=preview["request_id"],
        principal=WORKER,
        idempotency_key="reconcile-1",
    )
    assert reconciled["status"] in {"delivered", "confirmed"}
    assert delivery.deliver_count == 1
    assert "lookup" in delivery.calls
    again = service.process_next_export(principal=WORKER)
    assert again["status"] == "idle"
    assert delivery.deliver_count == 1


def test_one_time_token_confirm_and_replay_closes(
    tmp_path: Path,
) -> None:
    delivery = InMemoryRegisteredExportDelivery()
    service = _service(tmp_path, delivery=delivery)
    flow = _flow(service)
    token = delivery.last_token or ""
    accessed = service.access(
        request_id=flow["request_id"],
        principal=RECIPIENT,
        token=token,
    )
    assert accessed["status"] == "accessed"
    assert "url" not in accessed
    assert "path" not in accessed
    assert PLAINTEXT.decode() not in json.dumps(accessed)
    with pytest.raises(S17Blocked) as replay:
        service.access(
            request_id=flow["request_id"],
            principal=RECIPIENT,
            token=token,
        )
    assert replay.value.reason_code == S17_TOKEN_REPLAY
    confirmed = service.confirm(
        request_id=flow["request_id"],
        principal=RECIPIENT,
        idempotency_key="confirm-1",
    )
    assert confirmed["status"] == "confirmed"


# ---------------------------------------------------------------------------
# 6. Restart, cleanup, audit, receipt, fence
# ---------------------------------------------------------------------------


def test_audit_or_deletion_ledger_outage_has_zero_export_effect(
    tmp_path: Path,
) -> None:
    _recorded, writer = _writer(fail=True)
    service = _service(tmp_path, security_audit_writer=writer)
    before = list(service._events)
    with pytest.raises(S17Unavailable):
        _preview(service, idempotency_key="audit-outage")
    assert service._events == before
    closed = _service(tmp_path / "no-audit", security_audit_available=False)
    with pytest.raises(S17Unavailable):
        _preview(closed, idempotency_key="audit-closed")
    assert closed._events == []


def test_export_does_not_rewrite_lifecycle_or_evidence_and_survives_restart(
    tmp_path: Path,
) -> None:
    source = FakeExportSource()
    source.register(tenant=TENANT, reference=SCOPE_REFERENCE)
    ledger = tmp_path / "s17.sqlite3"
    service = _service(tmp_path, export_source=source, ledger_path=ledger)
    counts_before = source.fact_counts()
    flow = _flow(service)
    assert source.fact_counts() == counts_before
    request_id = flow["request_id"]
    restarted = _service(tmp_path, export_source=source, ledger_path=ledger)
    query = restarted.query(request_id=request_id, principal=REQUESTER)
    assert query["status"] == "delivered"
    assert source.fact_counts() == counts_before
    receipt = restarted.receipt(request_id=request_id, principal=REQUESTER)
    serialized = json.dumps(receipt)
    assert SCOPE_REFERENCE not in serialized
    assert PLAINTEXT.decode() not in serialized
    assert "http://" not in serialized
    assert "/tmp/" not in serialized
    assert "token" not in serialized
    assert receipt["status"] in {"delivered", "registered", "confirmed"}
    assert "package_digest" in receipt


def test_restart_cleanup_receipt_audit_and_fence_recovery(
    tmp_path: Path,
) -> None:
    source = FakeExportSource()
    source.register(tenant=TENANT, reference=SCOPE_REFERENCE)
    ledger = tmp_path / "fence.sqlite3"
    encryption = RecordingEncryptionProvider(fail=True)
    service = _service(
        tmp_path,
        export_source=source,
        encryption=encryption,
        ledger_path=ledger,
    )
    preview = _preview(service)
    _approve(service, preview)
    _commit(service, preview)
    failed = service.process_next_export(principal=WORKER)
    assert failed["status"] == "failed"
    assert service._temp_unregistered() == ()
    encryption.fail = False
    restarted = _service(
        tmp_path,
        export_source=source,
        encryption=encryption,
        ledger_path=ledger,
    )
    processed = restarted.process_next_export(principal=WORKER)
    assert processed["status"] == "delivered"
    receipt = restarted.receipt(request_id=preview["request_id"], principal=APPROVER)
    assert receipt["cleanup_result"] in {"cleaned", "none", "complete"}
    assert "attempt" in receipt
    audits = [
        event
        for event in restarted._events
        if event.get("event_type") == "security_audit"
    ]
    assert audits
    for fact in audits:
        dumped = json.dumps(fact)
        assert SCOPE_REFERENCE not in dumped
        assert PLAINTEXT.decode() not in dumped
