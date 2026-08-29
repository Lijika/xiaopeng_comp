"""S17 governed export ledger and orchestration (Ticket #33).

Independent default-off export plane. Missing provider, recipient
registry, audit or storage configuration keeps the plane closed.
C-DEMO test providers prove the provider boundary only.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

S17_EXPORT_SCHEMA = "s17-governed-export/1"
S17_PACKAGE_SCHEMA = "s17-package/1"
S17_EVENT_SCHEMA = "s17-event/1"
S17_RECEIPT_SCHEMA = "s17-receipt/1"
S17_OBLIGATION_STATUSES = frozenset(
    {
        "previewed",
        "approved",
        "queued",
        "delivered",
        "confirmed",
        "revoked",
        "expired",
        "failed",
        "timeout",
        "accessed",
    }
)

S17_UNAVAILABLE = "S17_UNAVAILABLE"
S17_AUDIT_SEAM_UNAVAILABLE = "S17_AUDIT_SEAM_UNAVAILABLE"
S17_STORAGE_UNAVAILABLE = "S17_STORAGE_UNAVAILABLE"
S17_PROVIDER_UNAVAILABLE = "S17_PROVIDER_UNAVAILABLE"
S17_FORBIDDEN = "S17_FORBIDDEN"
S17_INVALID_SCOPE = "S17_INVALID_SCOPE"
S17_DIGEST_DRIFT = "S17_DIGEST_DRIFT"
S17_SOURCE_DRIFT = "S17_SOURCE_DRIFT"
S17_SELF_APPROVAL = "S17_SELF_APPROVAL"
S17_RECIPIENT_MISMATCH = "S17_RECIPIENT_MISMATCH"
S17_TOKEN_EXPIRED = "S17_TOKEN_EXPIRED"
S17_TOKEN_REPLAY = "S17_TOKEN_REPLAY"
S17_TOKEN_REVOKED = "S17_TOKEN_REVOKED"

REQUESTER_ROLE = "operator"
REQUESTER_SOURCE_ID = "s17-export-console"
APPROVER_ROLE = "operator"
APPROVER_SOURCE_ID = "s17-approval-desk"
WORKER_ROLE = "system"
WORKER_SOURCE_ID = "s17-export-worker"
RECIPIENT_ROLE = "recipient"
RECIPIENT_SOURCE_ID = "s17-recipient-channel"

ALLOWED_PURPOSES = frozenset({"regulatory_review", "audit_response", "legal_process"})
ALLOWED_CLASSIFICATIONS = frozenset({"internal", "confidential", "restricted"})
ALLOWED_FIELDS = frozenset(
    {
        "application_fingerprint",
        "lifecycle_phase",
        "route_status",
        "finding_count",
        "delivery_status",
        "evaluation_track",
    }
)
ALLOWED_ARTIFACTS = frozenset(
    {
        "route_metadata",
        "evaluation_bundle_digest",
        "delivery_verification_digest",
    }
)
MAX_EXPORT_TTL_SECONDS = 7 * 24 * 3600
LEASE_SECONDS = 60
WATERMARK_PLAN = {
    "scheme": "s17-watermark/1",
    "binds": ["obligation", "recipient", "expiry", "purpose", "package_digest"],
}


class S17Forbidden(PermissionError):
    def __init__(self, message: str = "S17_FORBIDDEN: governed export identity required") -> None:
        super().__init__(message)


class S17NotFound(LookupError):
    def __init__(self, message: str = "export request is unavailable") -> None:
        super().__init__(message)


class S17Blocked(RuntimeError):
    def __init__(self, reason_code: str, message: str | None = None) -> None:
        self.reason_code = reason_code
        super().__init__(message or reason_code)


class S17Unavailable(RuntimeError):
    def __init__(self, reason_code: str = S17_UNAVAILABLE, message: str | None = None) -> None:
        self.reason_code = reason_code
        super().__init__(message or reason_code)


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, bytes):
        return hashlib.sha256(value).hexdigest()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    return str(value)


def _digest(value: Any) -> str:
    encoded = json.dumps(_canonical(value), separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{_digest(value)[:24]}"


def _names(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(str(item) for item in value)


@dataclass(frozen=True)
class RecipientRegistration:
    recipient_id: str
    channel_id: str
    registration_digest: str
    allowed_classifications: tuple[str, ...] = ("internal", "confidential", "restricted")


class RecipientRegistry:
    def __init__(self, registrations: Iterable[RecipientRegistration] = ()) -> None:
        items = tuple(registrations)
        self._items = items
        self._by_id = {item.recipient_id: item for item in items}

    def get(self, recipient_id: str) -> RecipientRegistration | None:
        return self._by_id.get(recipient_id)

    def __bool__(self) -> bool:
        return bool(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)


@dataclass(frozen=True)
class EncryptionResult:
    ciphertext: bytes
    key_ref: str
    key_version: str
    package_digest: str


@dataclass(frozen=True)
class ExportScope:
    purpose: str
    fields: tuple[str, ...]
    artifacts: tuple[str, ...]
    classification: str
    recipient_id: str
    expiry: int
    scope_fingerprint: str


@dataclass(frozen=True)
class ExportRequest:
    request_id: str
    preview_digest: str
    scope: ExportScope
    source_revisions: dict[str, Any]
    policy_digest: str


@dataclass(frozen=True)
class ExportApproval:
    request_id: str
    preview_digest: str
    approver_subject: str


@dataclass(frozen=True)
class ExportObligation:
    obligation_id: str
    request_id: str
    preview_digest: str
    purpose: str
    recipient_id: str
    classification: str
    expiry: int
    scope_fingerprint: str
    source_revisions: dict[str, Any]
    policy_digest: str
    package_digest: str | None = None
    delivery_registration_digest: str | None = None


@dataclass(frozen=True)
class ExportPackage:
    package_id: str
    obligation_id: str
    package_digest: str
    watermark_id: str
    key_ref: str
    key_version: str


@dataclass
class ExportDeliveryRequest:
    operation_id: str
    obligation_id: str
    package_id: str
    package_digest: str
    recipient_id: str
    recipient_registration_digest: str
    expiry: int
    token: str
    binding_digest: str
    fence: int
    attempt: int


class ExportSource(Protocol):
    def resolve_reference(self, *, tenant_scope: str, scope_fingerprint: str) -> str | None:
        ...

    def pin(
        self,
        *,
        tenant_scope: str,
        scope_reference: str,
        fields: tuple[str, ...],
        artifacts: tuple[str, ...],
    ) -> dict[str, Any] | None:
        ...

    def snapshot(
        self,
        *,
        tenant_scope: str,
        scope_reference: str,
        fields: tuple[str, ...],
        artifacts: tuple[str, ...],
        source_revisions: dict[str, Any],
    ) -> bytes:
        ...


class EncryptionProvider(Protocol):
    def encrypt(self, plaintext: bytes, context: dict[str, Any]) -> EncryptionResult:
        ...


class WatermarkProvider(Protocol):
    def bind(
        self,
        *,
        obligation_id: str,
        recipient_id: str,
        expiry: int,
        purpose: str,
        package_digest: str,
    ) -> dict[str, str]:
        ...


class RegisteredExportDelivery(Protocol):
    def deliver(self, request: ExportDeliveryRequest) -> dict[str, Any]:
        ...

    def lookup(self, operation_id: str, **kwargs: Any) -> dict[str, Any]:
        ...

    def confirm(self, operation_id: str, **kwargs: Any) -> dict[str, Any]:
        ...

    def revoke(self, operation_id: str, **kwargs: Any) -> dict[str, Any]:
        ...


_LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS s17_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    request_id TEXT,
    payload_json TEXT NOT NULL,
    occurred_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS s17_jobs (
    job_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    obligation_id TEXT NOT NULL,
    status TEXT NOT NULL,
    fence INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    lease_until INTEGER NOT NULL,
    lease_owner TEXT,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS s17_bindings (
    binding_key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS s17_tokens (
    token_fingerprint TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    recipient_id TEXT NOT NULL,
    expiry INTEGER NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0,
    revoked INTEGER NOT NULL DEFAULT 0
);
"""


class S17Ledger:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self.connect() as connection:
            connection.executescript(_LEDGER_SQL)
            try:
                connection.execute("ALTER TABLE s17_jobs ADD COLUMN lease_owner TEXT")
            except sqlite3.OperationalError:
                pass
            connection.commit()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

# Source locators stay behind the registered ExportSource resolver. The ledger
# stores scope fingerprints only, so restart recovery remains value-free.
class GovernedExportService:
    """Deep S17 interface for the governed export lifecycle."""

    def __init__(
        self,
        *,
        ledger_path: Path,
        requester_subject: str,
        approver_subject: str,
        worker_id: str,
        export_scope: str,
        recipient_registry: RecipientRegistry,
        export_source: ExportSource,
        encryption_provider: EncryptionProvider | None,
        watermark_provider: WatermarkProvider | None,
        delivery: RegisteredExportDelivery | None,
        security_audit_available: bool = True,
        security_audit_writer: Callable[[dict[str, Any]], bool] | None = None,
        storage_available: bool = True,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if not requester_subject or not approver_subject or not worker_id:
            raise ValueError("S17 identities are required")
        if requester_subject == approver_subject:
            raise ValueError("S17 requester and approver identities must be independent")
        if not export_scope or not recipient_registry:
            raise ValueError("S17 export scope and recipient registration are required")
        if encryption_provider is None or watermark_provider is None or delivery is None:
            raise ValueError("S17 provider and delivery configuration are required")
        if security_audit_available and (security_audit_writer is None or not callable(security_audit_writer)):
            raise ValueError("S17 security audit writer is required")
        self.ledger = S17Ledger(ledger_path)
        self.ledger_path = Path(ledger_path)
        self.requester_subject = requester_subject
        self.approver_subject = approver_subject
        self.worker_id = worker_id
        self.export_scope = export_scope
        self._recipients = recipient_registry
        self._source = export_source
        self._encryption = encryption_provider
        self._watermark = watermark_provider
        self._delivery = delivery
        self.audit_available = bool(security_audit_available)
        self._audit_writer = security_audit_writer
        self.storage_available = bool(storage_available)
        self._clock = clock or (lambda: int(time.time()))
        self._lock = threading.RLock()
        self._events: list[dict[str, Any]] = []
        self._requests: dict[str, dict[str, Any]] = {}
        self._approvals: dict[str, dict[str, Any]] = {}
        self._obligations: dict[str, dict[str, Any]] = {}
        self._jobs: dict[str, dict[str, Any]] = {}
        self._packages: dict[str, dict[str, Any]] = {}
        self._deliveries: dict[str, dict[str, Any]] = {}
        self._tokens: dict[str, dict[str, Any]] = {}
        self._temp_partials: set[str] = set()
        self._load_events()

    # -- state and authorization -------------------------------------------------
    def _load_events(self) -> None:
        with self.ledger.connect() as db:
            rows = db.execute(
                "SELECT event_type, request_id, payload_json, occurred_at "
                "FROM s17_events ORDER BY rowid"
            ).fetchall()
            token_rows = db.execute(
                "SELECT token_fingerprint, request_id, recipient_id, expiry, consumed, revoked FROM s17_tokens"
            ).fetchall()
            job_rows = db.execute(
                "SELECT job_id, request_id, obligation_id, status, fence, attempt, lease_until, lease_owner, payload_json FROM s17_jobs"
            ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            event = {
                "event_type": row["event_type"],
                "request_id": row["request_id"],
                **payload,
            }
            self._events.append(event)
            self._apply_event(event)
        for row in token_rows:
            self._tokens[str(row["token_fingerprint"])] = {
                "request_id": row["request_id"],
                "recipient_id": row["recipient_id"],
                "expiry": int(row["expiry"]),
                "consumed": bool(row["consumed"]),
                "revoked": bool(row["revoked"]),
            }
        for row in job_rows:
            payload = json.loads(row["payload_json"])
            self._jobs[str(row["job_id"])] = {
                **payload,
                "job_id": row["job_id"],
                "request_id": row["request_id"],
                "obligation_id": row["obligation_id"],
                "status": row["status"],
                "fence": int(row["fence"]),
                "attempt": int(row["attempt"]),
                "lease_until": int(row["lease_until"]),
                "lease_owner": row["lease_owner"],
            }

    def _apply_event(self, event: dict[str, Any]) -> None:
        typ = event.get("event_type")
        rid = event.get("request_id")
        if typ == "previewed" and rid:
            self._requests[rid] = dict(event)
        elif typ == "approved" and rid:
            self._approvals[rid] = dict(event)
            if rid in self._requests:
                self._requests[rid]["status"] = "approved"
        elif typ == "obligation_queued" and rid:
            self._obligations[rid] = dict(event)
            self._jobs[event["job_id"]] = dict(event)
            self._requests.setdefault(rid, {})["status"] = "queued"
        elif typ == "package_registered" and rid:
            self._packages[rid] = dict(event)
            self._obligations.setdefault(rid, {}).update(event)
            self._requests.setdefault(rid, {})["status"] = "delivered"
        elif typ in {"delivery_attempt", "delivery_timeout", "delivery_reconciled"} and rid:
            self._deliveries[rid] = dict(event)
            self._requests.setdefault(rid, {})["status"] = event.get("status", "timeout")
        elif typ in {"accessed", "confirmed", "revoked", "expired", "generation_failed"} and rid:
            self._requests.setdefault(rid, {})["status"] = event.get("status", typ)
            if typ == "revoked":
                for token in self._tokens.values():
                    if token.get("request_id") == rid:
                        token["revoked"] = True
            if typ == "expired":
                for token in self._tokens.values():
                    if token.get("request_id") == rid:
                        token["revoked"] = True

    def _append(self, event_type: str, request_id: str | None, payload: dict[str, Any], *, audit: bool = True) -> dict[str, Any]:
        event = {"event_type": event_type, "request_id": request_id, **payload}
        audit_record: dict[str, Any] | None = None
        if audit:
            if not self.audit_available or self._audit_writer is None:
                raise S17Unavailable(S17_AUDIT_SEAM_UNAVAILABLE)
            audit_record = {
                "schema": S17_EVENT_SCHEMA,
                "actor_fingerprint": _digest(payload.get("actor", "")),
                "request_fingerprint": _digest(request_id or ""),
                "action": event_type,
                "result": payload.get("status", event_type),
                "reason_code": payload.get("reason_code"),
            }
            try:
                if self._audit_writer is None or not self._audit_writer(audit_record):
                    raise RuntimeError("audit writer rejected record")
            except Exception as exc:
                raise S17Unavailable(S17_AUDIT_SEAM_UNAVAILABLE) from exc
        event_id = _stable_id("evt", {"type": event_type, "request": request_id, "payload": payload, "n": len(self._events)})
        occurred = int(self._clock())
        with self.ledger.connect() as db:
            if audit_record is not None:
                audit_id = _stable_id("audit", {"event": event_id})
                db.execute(
                    "INSERT INTO s17_events(event_id,event_type,request_id,payload_json,occurred_at) VALUES (?,?,?,?,?)",
                    (audit_id, "security_audit", request_id, json.dumps(_canonical(audit_record), separators=(",", ":")), occurred),
                )
            db.execute(
                "INSERT INTO s17_events(event_id,event_type,request_id,payload_json,occurred_at) VALUES (?,?,?,?,?)",
                (event_id, event_type, request_id, json.dumps(_canonical(payload), separators=(",", ":")), occurred),
            )
            db.commit()
        event["occurred_at"] = occurred
        self._events.append(event)
        self._apply_event(event)
        if audit_record is not None:
            audit_event = {"event_type": "security_audit", "request_id": request_id, **audit_record, "occurred_at": occurred}
            self._events.insert(-1, audit_event)
        return event

    def _binding(self, key: str, fingerprint: str) -> dict[str, Any] | None:
        with self.ledger.connect() as db:
            row = db.execute("SELECT fingerprint, result_json FROM s17_bindings WHERE binding_key=?", (key,)).fetchone()
        if row is None:
            return None
        if str(row["fingerprint"]) != fingerprint:
            raise S17Blocked(S17_DIGEST_DRIFT)
        return json.loads(row["result_json"])

    def _save_binding(self, key: str, fingerprint: str, result: dict[str, Any]) -> None:
        with self.ledger.connect() as db:
            db.execute("INSERT OR REPLACE INTO s17_bindings(binding_key,fingerprint,result_json,created_at) VALUES (?,?,?,?)", (key, fingerprint, json.dumps(_canonical(result), separators=(",", ":")), int(self._clock())))
            db.commit()

    def _commit_transaction(self, request_id: str, payload: dict[str, Any], binding_key: str, fingerprint: str, result: dict[str, Any]) -> None:
        """Persist commit event, job projection and idempotency binding atomically."""
        event_id = _stable_id("evt", {"type": "obligation_queued", "request": request_id, "payload": payload})
        now = int(self._clock())
        audit_record = {"schema": S17_EVENT_SCHEMA, "actor_fingerprint": _digest(payload.get("actor", "")), "request_fingerprint": _digest(request_id), "action": "obligation_queued", "result": "queued"}
        with self.ledger.connect() as db:
            db.execute("INSERT INTO s17_events(event_id,event_type,request_id,payload_json,occurred_at) VALUES (?,?,?,?,?)", (_stable_id("audit", event_id), "security_audit", request_id, json.dumps(_canonical(audit_record), separators=(",", ":")), now))
            db.execute("INSERT INTO s17_events(event_id,event_type,request_id,payload_json,occurred_at) VALUES (?,?,?,?,?)", (event_id, "obligation_queued", request_id, json.dumps(_canonical(payload), separators=(",", ":")), now))
            db.execute("INSERT OR IGNORE INTO s17_jobs(job_id,request_id,obligation_id,status,fence,attempt,lease_until,lease_owner,payload_json) VALUES (?,?,?,?,?,?,?,?,?)", (payload["job_id"], request_id, payload["obligation_id"], "queued", 0, 0, 0, None, json.dumps(_canonical(payload), separators=(",", ":"))))
            db.execute("INSERT INTO s17_bindings(binding_key,fingerprint,result_json,created_at) VALUES (?,?,?,?)", (binding_key, fingerprint, json.dumps(_canonical(result), separators=(",", ":")), now))
            db.commit()
        # The SQLite audit fact is part of the atomic commit. The institution
        # sink receives the same redacted record after commit and cannot create
        # a durable side effect ahead of a transaction that may roll back.
        try:
            if self._audit_writer is not None:
                self._audit_writer(audit_record)
        except Exception:
            pass
        event = {"event_type": "obligation_queued", "request_id": request_id, **payload, "occurred_at": now}
        audit_event = {"event_type": "security_audit", "request_id": request_id, **audit_record, "occurred_at": now}
        self._events.extend((audit_event, event))
        self._apply_event(event)

    @staticmethod
    def _principal_attrs(principal: Any) -> tuple[str, str, str, str, float]:
        return (
            str(getattr(principal, "subject", "")),
            str(getattr(principal, "role", "")),
            str(getattr(principal, "scope", "")),
            str(getattr(principal, "source_id", "")),
            float(getattr(principal, "expires_at", 0)),
        )

    def _require_requester(self, principal: Any) -> None:
        subject, role, scope, source, expires = self._principal_attrs(principal)
        if (subject, role, scope, source) != (self.requester_subject, REQUESTER_ROLE, self.export_scope, REQUESTER_SOURCE_ID) or expires < self._clock():
            raise S17Forbidden()

    def _require_approver(self, principal: Any) -> None:
        subject, role, scope, source, expires = self._principal_attrs(principal)
        if (subject, role, scope, source) != (self.approver_subject, APPROVER_ROLE, self.export_scope, APPROVER_SOURCE_ID) or expires < self._clock():
            raise S17Forbidden()

    def _require_worker(self, principal: Any) -> None:
        subject, role, scope, source, expires = self._principal_attrs(principal)
        if (subject, role, scope, source) != (self.worker_id, WORKER_ROLE, self.export_scope, WORKER_SOURCE_ID) or expires < self._clock():
            raise S17Forbidden()

    def _require_recipient(self, principal: Any, recipient_id: str) -> None:
        subject, role, scope, source, expires = self._principal_attrs(principal)
        if (subject, role, scope, source) != (recipient_id, RECIPIENT_ROLE, self.export_scope, RECIPIENT_SOURCE_ID) or expires < self._clock():
            raise S17Forbidden()

    def ready(self) -> bool:
        return bool(
            self.audit_available
            and self.storage_available
            and callable(self._audit_writer)
            and self._encryption is not None
            and self._watermark is not None
            and self._delivery is not None
            and self._recipients
        )

    def _require_ready(self) -> None:
        if not self.ready():
            reason = S17_AUDIT_SEAM_UNAVAILABLE if not self.audit_available or not callable(self._audit_writer) else S17_STORAGE_UNAVAILABLE if not self.storage_available else S17_PROVIDER_UNAVAILABLE
            raise S17Unavailable(reason)

    def _resolve_source_reference(self, request: dict[str, Any]) -> str | None:
        resolver = getattr(self._source, "resolve_reference", None)
        if callable(resolver):
            return resolver(tenant_scope=self.export_scope, scope_fingerprint=str(request.get("scope_fingerprint", "")))
        return None

    # -- request, approval and commit -------------------------------------------
    def preview(self, *, purpose: str, fields: Iterable[str], artifacts: Iterable[str], recipient_id: str, classification: str, expiry: int, scope_reference: str, principal: Any, idempotency_key: str) -> dict[str, Any]:
        subject, role, scope, source, _expires = self._principal_attrs(principal)
        if subject == self.requester_subject and role == REQUESTER_ROLE and source == REQUESTER_SOURCE_ID and scope != self.export_scope:
            raise S17NotFound()
        self._require_requester(principal)
        self._require_ready()
        fields_t, artifacts_t = _names(fields), _names(artifacts)
        if not fields_t and not artifacts_t or any(x not in ALLOWED_FIELDS for x in fields_t) or any(x not in ALLOWED_ARTIFACTS for x in artifacts_t) or len(set(fields_t)) != len(fields_t) or len(set(artifacts_t)) != len(artifacts_t) or purpose not in ALLOWED_PURPOSES or classification not in ALLOWED_CLASSIFICATIONS or expiry <= self._clock() or expiry - self._clock() > MAX_EXPORT_TTL_SECONDS:
            raise S17Blocked(S17_INVALID_SCOPE)
        registration = self._recipients.get(recipient_id)
        if registration is None or classification not in registration.allowed_classifications:
            raise S17Blocked(S17_RECIPIENT_MISMATCH)
        pin = self._source.pin(tenant_scope=self.export_scope, scope_reference=scope_reference, fields=fields_t, artifacts=artifacts_t)
        if pin is None:
            raise S17NotFound()
        scope_fp = str(pin["scope_fingerprint"])
        source_revisions = dict(pin.get("source_revisions", {}))
        policy_digest = str(pin.get("policy_digest", ""))
        context = {"purpose": purpose, "fields": fields_t, "artifacts": artifacts_t, "recipient_id": recipient_id, "classification": classification, "expiry": expiry, "scope_fingerprint": scope_fp, "source_revisions": source_revisions, "policy_digest": policy_digest}
        fingerprint = _digest(context)
        binding_key = f"preview:{self.export_scope}:{idempotency_key}"
        bound = self._binding(binding_key, fingerprint)
        if bound is not None:
            return bound | {"replayed": True}
        request_id = _stable_id("s17req", {"scope": self.export_scope, "reference": hashlib.sha256(scope_reference.encode()).hexdigest(), "idempotency": idempotency_key})
        existing = self._requests.get(request_id)
        if existing:
            if existing.get("preview_digest") != fingerprint:
                raise S17Blocked(S17_DIGEST_DRIFT)
            return self._preview_result(existing, replayed=True)
        event = self._append("previewed", request_id, {"status": "previewed", "preview_digest": fingerprint, "purpose": purpose, "fields": fields_t, "artifacts": artifacts_t, "recipient_id": recipient_id, "recipient_registration_digest": registration.registration_digest, "classification": classification, "expiry": expiry, "scope_fingerprint": scope_fp, "source_revisions": source_revisions, "policy_digest": policy_digest, "field_count": len(fields_t), "artifact_count": len(artifacts_t), "watermark_plan": WATERMARK_PLAN, "actor": principal.subject})
        result = self._preview_result(event)
        self._save_binding(binding_key, fingerprint, result)
        return result

    def _preview_result(self, item: dict[str, Any], *, replayed: bool = False) -> dict[str, Any]:
        return {k: item[k] for k in ("status", "request_id", "preview_digest", "purpose", "fields", "artifacts", "recipient_id", "recipient_registration_digest", "classification", "expiry", "scope_fingerprint", "source_revisions", "policy_digest", "field_count", "artifact_count", "watermark_plan") if k in item} | {"replayed": replayed}

    def approve(self, *, request_id: str, preview_digest: str, principal: Any, idempotency_key: str) -> dict[str, Any]:
        subject, _role, _scope, _source, _expires = self._principal_attrs(principal)
        if subject == self.requester_subject:
            raise S17Blocked(S17_SELF_APPROVAL)
        self._require_approver(principal)
        self._require_ready()
        request = self._requests.get(request_id)
        if request is None:
            raise S17NotFound()
        if principal.subject == request.get("actor") or principal.subject == self.requester_subject:
            raise S17Blocked(S17_SELF_APPROVAL)
        if preview_digest != request.get("preview_digest"):
            raise S17Blocked(S17_DIGEST_DRIFT)
        binding_key = f"approve:{request_id}:{idempotency_key}"
        bound = self._binding(binding_key, preview_digest)
        if bound is not None:
            return bound | {"replayed": True}
        existing = self._approvals.get(request_id)
        if existing:
            return {"status": "approved", "request_id": request_id, "preview_digest": preview_digest, "approved_by": _digest(principal.subject), "replayed": True}
        self._append("approved", request_id, {"status": "approved", "preview_digest": preview_digest, "approver_fingerprint": _digest(principal.subject), "approved_by": _digest(principal.subject), "actor": principal.subject})
        result = {"status": "approved", "request_id": request_id, "preview_digest": preview_digest, "approved_by": _digest(principal.subject), "replayed": False}
        self._save_binding(binding_key, preview_digest, result)
        return result

    def commit(self, *, request_id: str, principal: Any, idempotency_key: str) -> dict[str, Any]:
        self._require_requester(principal)
        self._require_ready()
        request = self._requests.get(request_id)
        if request is None:
            raise S17NotFound()
        if request_id not in self._approvals:
            raise S17Blocked(S17_FORBIDDEN)
        scope_reference = self._resolve_source_reference(request)
        if not scope_reference:
            raise S17Blocked(S17_SOURCE_DRIFT)
        pin = self._source.pin(tenant_scope=self.export_scope, scope_reference=scope_reference, fields=tuple(request.get("fields", ())), artifacts=tuple(request.get("artifacts", ())))
        if pin is None:
            raise S17Blocked(S17_SOURCE_DRIFT)
        if dict(pin.get("source_revisions", {})) != dict(request.get("source_revisions", {})) or str(pin.get("policy_digest", "")) != request.get("policy_digest"):
            raise S17Blocked(S17_DIGEST_DRIFT)
        if request_id in self._obligations:
            item = self._obligations[request_id]
            return {"status": "queued", "request_id": request_id, "obligation_id": item["obligation_id"], "job_id": item["job_id"], "replayed": True}
        binding_key = f"commit:{request_id}:{idempotency_key}"
        bound = self._binding(binding_key, request["preview_digest"])
        if bound is not None:
            return bound | {"replayed": True}
        obligation_id = _stable_id("s17obl", {"request": request_id, "digest": request["preview_digest"]})
        job_id = _stable_id("s17job", obligation_id)
        payload = {"status": "queued", "obligation_id": obligation_id, "job_id": job_id, "preview_digest": request["preview_digest"], "purpose": request["purpose"], "recipient_id": request["recipient_id"], "recipient_registration_digest": request["recipient_registration_digest"], "classification": request["classification"], "expiry": request["expiry"], "scope_fingerprint": request["scope_fingerprint"], "source_revisions": request["source_revisions"], "policy_digest": request["policy_digest"], "attempt": 0, "fence": 0, "actor": principal.subject}
        result = {"status": "queued", "request_id": request_id, "obligation_id": obligation_id, "job_id": job_id, "replayed": False}
        self._commit_transaction(request_id, payload, binding_key, request["preview_digest"], result)
        return result

    # -- generation and delivery -----------------------------------------------
    def process_next_export(self, *, principal: Any) -> dict[str, Any]:
        self._require_worker(principal)
        self._require_ready()
        with self._lock:
            now = int(self._clock())
            job = next(
                (
                    j
                    for j in self._jobs.values()
                    if j.get("status") in {"queued", "timeout"}
                    or (
                        j.get("status") == "processing"
                        and int(j.get("lease_until", 0)) <= now
                    )
                ),
                None,
            )
            if job is None:
                return {"status": "idle"}
            rid = job["request_id"]
            previous_job = dict(job)
            job["status"] = "processing"
            job["attempt"] = int(job.get("attempt", 0)) + 1
            job["fence"] = int(job.get("fence", 0)) + 1
            job["lease_until"] = now + LEASE_SECONDS
            with self.ledger.connect() as db:
                updated = db.execute("UPDATE s17_jobs SET status='processing', fence=?, attempt=?, lease_until=?, lease_owner=? WHERE job_id=? AND status IN ('queued','timeout','processing') AND fence=? AND (lease_until=0 OR lease_until<=?)", (job["fence"], job["attempt"], job["lease_until"], self.worker_id, job["job_id"], job["fence"] - 1, now)).rowcount
                db.commit()
            if updated != 1:
                job.clear()
                job.update(previous_job)
                return {"status": "idle"}
            request = self._requests[rid]
            obligation_id = job["obligation_id"]
            try:
                scope_reference = self._resolve_source_reference(request)
                if not scope_reference:
                    raise S17Unavailable(S17_SOURCE_DRIFT)
                plaintext = self._source.snapshot(tenant_scope=self.export_scope, scope_reference=scope_reference, fields=tuple(request.get("fields", ())), artifacts=tuple(request.get("artifacts", ())), source_revisions=dict(request.get("source_revisions", {})))
                predicted_digest = _digest({"obligation_id": obligation_id, "plaintext": plaintext, "source_revisions": request.get("source_revisions", {})})
                watermark = self._watermark.bind(obligation_id=obligation_id, recipient_id=request["recipient_id"], expiry=int(request["expiry"]), purpose=request["purpose"], package_digest=predicted_digest)
                encrypted = self._encryption.encrypt(plaintext, {"obligation_id": obligation_id, "recipient_id": request["recipient_id"], "purpose": request["purpose"], "expiry": request["expiry"], "package_digest": predicted_digest, "source_revisions": request["source_revisions"]})
                self._temp_partials.add(rid)
                package_id = _stable_id("s17pkg", {"obligation": obligation_id, "digest": encrypted.package_digest})
                self._append("package_registered", rid, {"status": "registered", "package_id": package_id, "package_digest": encrypted.package_digest, "package_context_digest": predicted_digest, "watermark_id": watermark.get("watermark_id", ""), "key_ref": encrypted.key_ref, "key_version": encrypted.key_version, "obligation_id": obligation_id, "actor": principal.subject})
                token = secrets.token_urlsafe(24)
                operation_id = _stable_id("s17op", {"obligation": obligation_id, "package": package_id})
                binding = _digest({"operation_id": operation_id, "package_id": package_id, "package_digest": encrypted.package_digest, "recipient_id": request["recipient_id"], "expiry": request["expiry"], "registration": request["recipient_registration_digest"]})
                delivery_request = ExportDeliveryRequest(operation_id=operation_id, obligation_id=obligation_id, package_id=package_id, package_digest=encrypted.package_digest, recipient_id=request["recipient_id"], recipient_registration_digest=request["recipient_registration_digest"], expiry=int(request["expiry"]), token=token, binding_digest=binding, fence=int(job.get("fence", 0)), attempt=int(job.get("attempt", 0)))
                token_fp = _digest(token)
                self._tokens[token_fp] = {"request_id": rid, "recipient_id": request["recipient_id"], "expiry": int(request["expiry"]), "consumed": False, "revoked": False}
                with self.ledger.connect() as db:
                    db.execute("INSERT OR REPLACE INTO s17_tokens(token_fingerprint,request_id,recipient_id,expiry,consumed,revoked) VALUES (?,?,?,?,0,0)", (token_fp, rid, request["recipient_id"], int(request["expiry"])))
                    db.commit()
                result = self._delivery.deliver(delivery_request)
                outcome = str(result.get("outcome", "unknown"))
                if outcome == "timeout":
                    self._append("delivery_timeout", rid, {"status": "timeout", "operation_id": operation_id, "package_id": package_id, "package_digest": encrypted.package_digest, "attempt": delivery_request.attempt, "fence": delivery_request.fence, "actor": principal.subject, "reason_code": result.get("reason_code", "S17_DELIVERY_TIMEOUT")})
                    job["status"] = "timeout"
                    with self.ledger.connect() as db:
                        db.execute("UPDATE s17_jobs SET status='timeout', lease_until=0 WHERE job_id=? AND lease_owner=? AND fence=? AND attempt=?", (job["job_id"], self.worker_id, job["fence"], job["attempt"])); db.commit()
                    return {"status": "timeout", "request_id": rid, "job_id": job["job_id"], "attempt": delivery_request.attempt, "reason_code": result.get("reason_code", "S17_DELIVERY_TIMEOUT")}
                self._append("delivery_attempt", rid, {"status": "delivered", "operation_id": operation_id, "package_id": package_id, "package_digest": encrypted.package_digest, "attempt": delivery_request.attempt, "fence": delivery_request.fence, "actor": principal.subject})
                job["status"] = "delivered"
                with self.ledger.connect() as db:
                    db.execute("UPDATE s17_jobs SET status='delivered', lease_until=0 WHERE job_id=? AND lease_owner=? AND fence=? AND attempt=?", (job["job_id"], self.worker_id, job["fence"], job["attempt"])); db.commit()
                self._temp_partials.discard(rid)
                return {"status": "delivered", "request_id": rid, "job_id": job["job_id"], "package_id": package_id, "attempt": delivery_request.attempt}
            except S17Unavailable as exc:
                self._temp_partials.add(rid)
                self._temp_partials.discard(rid)
                self._append("partial_cleaned", rid, {"status": "failed", "reason_code": exc.reason_code, "attempt": int(job.get("attempt", 0)) + 1, "actor": principal.subject})
                job["status"] = "queued"
                with self.ledger.connect() as db:
                    db.execute("UPDATE s17_jobs SET status='queued', lease_until=0 WHERE job_id=? AND lease_owner=? AND fence=? AND attempt=?", (job["job_id"], self.worker_id, job["fence"], job["attempt"])); db.commit()
                return {"status": "failed", "request_id": rid, "job_id": job["job_id"], "reason_code": exc.reason_code}
            except Exception:
                self._temp_partials.add(rid)
                self._temp_partials.discard(rid)
                self._append("partial_cleaned", rid, {"status": "failed", "reason_code": S17_PROVIDER_UNAVAILABLE, "attempt": int(job.get("attempt", 0)) + 1, "actor": principal.subject})
                job["status"] = "queued"
                with self.ledger.connect() as db:
                    db.execute("UPDATE s17_jobs SET status='queued', lease_until=0 WHERE job_id=? AND lease_owner=? AND fence=? AND attempt=?", (job["job_id"], self.worker_id, job["fence"], job["attempt"])); db.commit()
                return {"status": "failed", "request_id": rid, "job_id": job["job_id"], "reason_code": S17_PROVIDER_UNAVAILABLE}

    def _temp_unregistered(self) -> tuple[str, ...]:
        return tuple(sorted(self._temp_partials))

    # -- recipient lifecycle ----------------------------------------------------
    def _delivery_for(self, request_id: str) -> dict[str, Any]:
        item = self._deliveries.get(request_id)
        if item is None:
            raise S17NotFound()
        return item

    def access(self, *, request_id: str, principal: Any, token: str) -> dict[str, Any]:
        request = self._requests.get(request_id)
        if request is None:
            raise S17NotFound()
        subject, role, _scope, _source, _expires = self._principal_attrs(principal)
        if role == RECIPIENT_ROLE and subject != request["recipient_id"]:
            raise S17Blocked(S17_RECIPIENT_MISMATCH)
        self._require_recipient(principal, request["recipient_id"])
        record = self._tokens.get(_digest(token))
        if record is None or record.get("request_id") != request_id:
            raise S17Blocked(S17_RECIPIENT_MISMATCH)
        if int(self._clock()) >= int(record["expiry"]):
            raise S17Blocked(S17_TOKEN_EXPIRED)
        if record.get("revoked"):
            raise S17Blocked(S17_TOKEN_REVOKED)
        if record.get("consumed"):
            raise S17Blocked(S17_TOKEN_REPLAY)
        record["consumed"] = True
        with self.ledger.connect() as db:
            db.execute("UPDATE s17_tokens SET consumed=1 WHERE token_fingerprint=?", (_digest(token),))
            db.commit()
        self._append("accessed", request_id, {"status": "accessed", "package_id": self._packages.get(request_id, {}).get("package_id"), "actor": principal.subject})
        return {"status": "accessed", "request_id": request_id, "package_id": self._packages.get(request_id, {}).get("package_id"), "watermark_id": self._packages.get(request_id, {}).get("watermark_id")}

    def confirm(self, *, request_id: str, principal: Any, idempotency_key: str) -> dict[str, Any]:
        request = self._requests.get(request_id)
        if request is None:
            raise S17NotFound()
        self._require_recipient(principal, request["recipient_id"])
        binding_key = f"confirm:{request_id}:{idempotency_key}"
        bound = self._binding(binding_key, request_id)
        if bound is not None:
            return bound | {"replayed": True}
        delivery = self._delivery_for(request_id)
        if request.get("status") == "confirmed":
            return {"status": "confirmed", "request_id": request_id, "replayed": True}
        self._delivery.confirm(delivery.get("operation_id", ""), package_id=self._packages.get(request_id, {}).get("package_id"), recipient_id=request["recipient_id"])
        self._append("confirmed", request_id, {"status": "confirmed", "package_id": self._packages.get(request_id, {}).get("package_id"), "actor": principal.subject})
        result = {"status": "confirmed", "request_id": request_id, "replayed": False}
        self._save_binding(binding_key, request_id, result)
        return result

    def revoke(self, *, request_id: str, principal: Any, idempotency_key: str) -> dict[str, Any]:
        self._require_requester(principal)
        request = self._requests.get(request_id)
        if request is None:
            raise S17NotFound()
        binding_key = f"revoke:{request_id}:{idempotency_key}"
        bound = self._binding(binding_key, request_id)
        if bound is not None:
            return bound | {"replayed": True}
        delivery = self._deliveries.get(request_id)
        if delivery:
            self._delivery.revoke(delivery.get("operation_id", ""), package_id=self._packages.get(request_id, {}).get("package_id"), recipient_id=request["recipient_id"])
        for token in self._tokens.values():
            if token.get("request_id") == request_id:
                token["revoked"] = True
        with self.ledger.connect() as db:
            db.execute("UPDATE s17_tokens SET revoked=1 WHERE request_id=?", (request_id,))
            db.commit()
        self._append("revoked", request_id, {"status": "revoked", "package_id": self._packages.get(request_id, {}).get("package_id"), "actor": principal.subject, "reason_code": S17_TOKEN_REVOKED})
        result = {"status": "revoked", "request_id": request_id, "replayed": False}
        self._save_binding(binding_key, request_id, result)
        return result

    def expire(self, *, request_id: str, principal: Any, idempotency_key: str) -> dict[str, Any]:
        self._require_worker(principal)
        request = self._requests.get(request_id)
        if request is None:
            raise S17NotFound()
        binding_key = f"expire:{request_id}:{idempotency_key}"
        bound = self._binding(binding_key, request_id)
        if bound is not None:
            return bound | {"replayed": True}
        if int(self._clock()) < int(request.get("expiry", 0)):
            raise S17Blocked(S17_TOKEN_EXPIRED)
        for token in self._tokens.values():
            if token.get("request_id") == request_id:
                token["revoked"] = True
        with self.ledger.connect() as db:
            db.execute("UPDATE s17_tokens SET revoked=1 WHERE request_id=?", (request_id,))
            db.commit()
        self._append("expired", request_id, {"status": "expired", "actor": principal.subject})
        result = {"status": "expired", "request_id": request_id, "replayed": False}
        self._save_binding(binding_key, request_id, result)
        return result

    def reconcile(self, *, request_id: str, principal: Any, idempotency_key: str) -> dict[str, Any]:
        self._require_worker(principal)
        binding_key = f"reconcile:{request_id}:{idempotency_key}"
        bound = self._binding(binding_key, request_id)
        if bound is not None:
            return bound | {"replayed": True}
        delivery = self._delivery_for(request_id)
        outcome = self._delivery.lookup(delivery.get("operation_id", ""), package_id=self._packages.get(request_id, {}).get("package_id"), recipient_id=self._requests[request_id]["recipient_id"])
        if outcome.get("outcome") in {"confirmed", "delivered"}:
            self._append("delivery_reconciled", request_id, {"status": "delivered", "operation_id": delivery.get("operation_id"), "package_id": self._packages.get(request_id, {}).get("package_id"), "actor": principal.subject})
            self._requests[request_id]["status"] = "delivered"
            job = self._jobs.get(self._obligations.get(request_id, {}).get("job_id", ""))
            if job is not None:
                with self.ledger.connect() as db:
                    db.execute(
                        "UPDATE s17_jobs SET status='delivered', lease_until=0 WHERE job_id=? AND status='timeout' AND lease_owner=? AND fence=? AND attempt=?",
                        (job["job_id"], self.worker_id, int(job.get("fence", 0)), int(job.get("attempt", 0))),
                    )
                    db.commit()
                job["status"] = "delivered"
                job["lease_until"] = 0
            result = {"status": "delivered", "request_id": request_id, "replayed": False}
            self._save_binding(binding_key, request_id, result)
            return result
        result = {"status": "timeout", "request_id": request_id, "replayed": False}
        self._save_binding(binding_key, request_id, result)
        return result

    # -- read models ------------------------------------------------------------
    def query(self, *, request_id: str, principal: Any) -> dict[str, Any]:
        request = self._requests.get(request_id)
        if request is None:
            raise S17NotFound()
        subject = self._principal_attrs(principal)[0]
        if subject not in {self.requester_subject, self.approver_subject}:
            raise S17Forbidden()
        package = self._packages.get(request_id, {})
        delivery = self._deliveries.get(request_id, {})
        return {"schema_version": S17_EXPORT_SCHEMA, "status": request.get("status", "previewed"), "request_id": request_id, "preview_digest": request.get("preview_digest"), "scope_fingerprint": request.get("scope_fingerprint"), "purpose": request.get("purpose"), "fields": request.get("fields", ()), "artifacts": request.get("artifacts", ()), "recipient_id": request.get("recipient_id"), "classification": request.get("classification"), "expiry": request.get("expiry"), "source_revisions": request.get("source_revisions", {}), "policy_digest": request.get("policy_digest"), "package_id": package.get("package_id"), "package_digest": package.get("package_context_digest") or package.get("package_digest"), "watermark_id": package.get("watermark_id"), "delivery_status": delivery.get("status"), "attempt": delivery.get("attempt", 0), "operation_id": delivery.get("operation_id")}

    def receipt(self, *, request_id: str, principal: Any) -> dict[str, Any]:
        self.query(request_id=request_id, principal=principal)
        request, package, delivery = self._requests[request_id], self._packages.get(request_id, {}), self._deliveries.get(request_id, {})
        return {"schema_version": S17_RECEIPT_SCHEMA, "receipt_id": _stable_id("s17rcpt", request_id), "request_fingerprint": _digest(request_id), "status": request.get("status", "previewed"), "package_digest": package.get("package_digest"), "delivery_status": delivery.get("status"), "attempt": delivery.get("attempt", 0), "expiry": request.get("expiry"), "cleanup_result": "cleaned" if request.get("status") == "failed" else "none", "replayed": False}
