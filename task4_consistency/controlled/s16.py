"""S16 governed deletion ledger and orchestration (Ticket #32).

The data-governance plane owns one independent SQLite ledger (events, jobs,
receipts) that never shares a failure domain with the business backup.  It
orchestrates complete-aggregate deletion of one terminated application
across the registered copy owners through a narrow Protocol:

- ``inventory(scope_fingerprint)``
- ``delete(copy_fingerprints, operation_id, fence)``
- ``verify_absent(copy_fingerprints)``
- ``replay(copy_fingerprints)``

No orchestrator code writes another owner's tables directly.  Owners resolve
their own copies by scanning their own stores and matching the value-free
scope fingerprint, so the ledger never stores application ids, object refs,
paths, raw values or credentials (ADR-0003 minimization).

Copy classes are fixed at nine: ``source_object``, ``derived_object``,
``evidence``, ``run_or_finding``, ``projection_or_cache``,
``export_or_temp``, ``evaluation_copy``, ``replica``, ``backup_manifest``.
``export_or_temp`` stays closed while S17 is disabled: the disabled owner
returns a documented zero-inventory proof so the class still appears in
every manifest.

Semantics pinned by ADR-0003/0004/0007/0008 and the ROUND32 plan:

- commit is the only irreversible boundary; every command is bound to
  subject, role, scope, action and request id;
- Legal Hold lives in this ledger; impose and commit race inside one
  SQLite transaction per command (short transactions, no broker);
- the deletion worker uses a finite lease, a monotonic fence, bounded
  attempts and owner-level idempotent deletes; after retry exhaustion a
  job enters ``repair_required`` with a stable failure, responsible party
  and recovery action, and ``repair`` resumes the same job;
- completion writes a value-free receipt and a restore-replay record;
  startup replays every completed manifest against the owners and only
  then opens readiness;
- when the security audit availability flag is off, protected commands
  fail closed with zero state change.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

S16_SCHEMA_VERSION = "s16-governed-deletion/1"
S16_POLICY_ID = "s16-retention/1"
S16_POLICY_VERSION = "1"
S16_OWNER_REGISTRY_SCHEMA = "s16-owner-registry/1"
S16_RECEIPT_SCHEMA = "s16-receipt/1"
S16_EVENT_SCHEMA = "s16-event/1"

# Stable shared-copy block reason (ADR-0007: in-place rewrites of immutable
# evaluation copies are forbidden; the owner must repack or obtain whole
# disposition first).
S16_SHARED_COPY_REQUIRES_REPACK = "S16_SHARED_COPY_REQUIRES_REPACK"
S16_ACTIVE_APPLICATION = "S16_ACTIVE_APPLICATION"
S16_ACTIVE_LEGAL_HOLD = "S16_ACTIVE_LEGAL_HOLD"
S16_AUDIT_UNAVAILABLE = "S16_AUDIT_UNAVAILABLE"
S16_STORAGE_UNAVAILABLE = "S16_STORAGE_UNAVAILABLE"
S16_APPROVALS_INCOMPLETE = "S16_APPROVALS_INCOMPLETE"
S16_MANIFEST_STALE = "S16_MANIFEST_STALE"
S16_OWNER_REGISTRY_STALE = "S16_OWNER_REGISTRY_STALE"
S16_POLICY_STALE = "S16_POLICY_STALE"
S16_REVISION_CHANGED = "S16_REVISION_CHANGED"
S16_RETENTION_NOT_DUE = "S16_RETENTION_NOT_DUE"
S16_OWNER_MISSING = "S16_OWNER_MISSING"
S16_OWNER_INTEGRITY = "S16_OWNER_INTEGRITY"
S16_REQUIRED_OWNER_ABSENT = "S16_REQUIRED_OWNER_ABSENT"
S16_ALREADY_COMMITTED = "S16_ALREADY_COMMITTED"
S16_ALREADY_CANCELLED = "S16_ALREADY_CANCELLED"
S16_HOLD_ACTIVE = "S16_HOLD_ACTIVE"
S16_REPAIR_REQUIRED = "S16_REPAIR_REQUIRED"
S16_OWNER_DELETE_FAILED = "S16_OWNER_DELETE_FAILED"
S16_VERIFY_FAILED = "S16_VERIFY_FAILED"
S16_REPAIR_NOT_VERIFIED = "S16_REPAIR_NOT_VERIFIED"
S16_RETAINED_VALUE = "S16_RETAINED_VALUE_FOUND"

COPY_CLASS_SOURCE_OBJECT = "source_object"
COPY_CLASS_DERIVED_OBJECT = "derived_object"
COPY_CLASS_EVIDENCE = "evidence"
COPY_CLASS_RUN_OR_FINDING = "run_or_finding"
COPY_CLASS_PROJECTION_OR_CACHE = "projection_or_cache"
COPY_CLASS_EXPORT_OR_TEMP = "export_or_temp"
COPY_CLASS_EVALUATION_COPY = "evaluation_copy"
COPY_CLASS_REPLICA = "replica"
COPY_CLASS_BACKUP_MANIFEST = "backup_manifest"

COPY_CLASSES = (
    COPY_CLASS_SOURCE_OBJECT,
    COPY_CLASS_DERIVED_OBJECT,
    COPY_CLASS_EVIDENCE,
    COPY_CLASS_RUN_OR_FINDING,
    COPY_CLASS_PROJECTION_OR_CACHE,
    COPY_CLASS_EXPORT_OR_TEMP,
    COPY_CLASS_EVALUATION_COPY,
    COPY_CLASS_REPLICA,
    COPY_CLASS_BACKUP_MANIFEST,
)

# Fixed nine-class owner registry.  ``export_or_temp`` maps to the disabled
# S17 owner while S17 export stays closed; the owner returns a proven zero
# inventory so the class still appears in every manifest.
OWNER_REGISTRY: dict[str, str] = {
    COPY_CLASS_SOURCE_OBJECT: "s01",
    COPY_CLASS_EVIDENCE: "s01",
    COPY_CLASS_RUN_OR_FINDING: "s01",
    COPY_CLASS_PROJECTION_OR_CACHE: "s01",
    COPY_CLASS_DERIVED_OBJECT: "s02",
    COPY_CLASS_EVALUATION_COPY: "s12",
    COPY_CLASS_EXPORT_OR_TEMP: "s17-disabled",
    COPY_CLASS_REPLICA: "backup",
    COPY_CLASS_BACKUP_MANIFEST: "backup",
}
REQUIRED_OWNERS = frozenset({"s01", "s02", "s12", "backup", "s17-disabled"})

# Worker execution order: external objects -> evaluation copies -> backup
# copies -> S01 restricted rows -> verification -> receipt.
EXECUTION_ORDER = ("s02", "s12", "backup", "s01", "s17-disabled")

MAX_OWNER_ATTEMPTS = 5
LEASE_SECONDS = 60

JOB_STATUSES = frozenset(
    {"pending", "running", "repair_required", "complete"}
)


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _stable_id(prefix: str, fingerprint: str) -> str:
    return f"{prefix}_{hashlib.sha256(fingerprint.encode()).hexdigest()[:24]}"


def s16_owner_registry_digest() -> str:
    """Versioned digest of the fixed nine-class owner registry."""
    return _digest(
        {
            "schema_version": S16_OWNER_REGISTRY_SCHEMA,
            "registry": {key: OWNER_REGISTRY[key] for key in COPY_CLASSES},
        }
    )


def scope_fingerprint_for(application_id: str) -> str:
    """The value-free application scope fingerprint.

    Only the fingerprint of the application identity participates; no
    application id, reference, path or value is stored (ADR-0003).
    """
    return _digest(
        {
            "schema_version": "s16-scope/1",
            "application_id_fingerprint": application_id_fingerprint(
                application_id
            ),
        }
    )


def zero_proof_entry(
    owner_id: str, copy_class: str, *, reason: str = "empty"
) -> CopyInventoryEntry:
    """A documented empty-collection proof so every copy class still appears
    in the manifest with an owner attestation (valid empty sets carry owner
    proof and the registry digest)."""
    proof = _digest(
        {
            "schema_version": "s16-empty-proof/1",
            "owner_id": owner_id,
            "copy_class": copy_class,
            "reason": reason,
        }
    )
    return CopyInventoryEntry(
        owner_id=owner_id,
        copy_class=copy_class,
        content_sha256=proof,
        identity_fingerprint=copy_identity_fingerprint(
            owner_id, copy_class, proof
        ),
        count=0,
        planned_action="none",
    )


def application_id_fingerprint(application_id: str) -> str:
    return hashlib.sha256(application_id.encode("utf-8")).hexdigest()


def copy_identity_fingerprint(
    owner_id: str, copy_class: str, content_sha256: str
) -> str:
    """The canonical value-free identity fingerprint of one copy entry."""
    return _digest(
        {
            "owner_id": owner_id,
            "copy_class": copy_class,
            "content_sha256": content_sha256,
        }
    )


class S16Unavailable(RuntimeError):
    """The governed deletion plane cannot prove its authority."""


class S16NotFound(LookupError):
    """Existence-hiding lookup failure (unknown request or application)."""


class S16Forbidden(PermissionError):
    """The caller is not an authorized governed-deletion identity."""


class S16Conflict(RuntimeError):
    """Idempotency conflict or post-commit cancellation conflict."""


class S16Blocked(RuntimeError):
    """A protected command was rejected with a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class S16OwnerFailure(RuntimeError):
    """One owner delete/verify step failed with a stable reason."""

    def __init__(
        self,
        owner_id: str,
        reason_code: str,
        *,
        retryable: bool = True,
        responsible_party: str = "runtime_operations_owner",
        recovery_action: str = "repair_owner_and_resume_the_same_job",
    ) -> None:
        super().__init__(f"{owner_id}: {reason_code}")
        self.owner_id = owner_id
        self.reason_code = reason_code
        self.retryable = retryable
        self.responsible_party = responsible_party
        self.recovery_action = recovery_action


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionPolicy:
    """A small versioned retention value object injected at startup.

    Normal due deletion requires every restricted entry to have reached its
    due time; anything earlier is an early deletion needing two approvals.
    """

    policy_id: str = S16_POLICY_ID
    policy_version: str = S16_POLICY_VERSION
    retention_seconds: int = 90 * 24 * 60 * 60

    def digest(self) -> str:
        return _digest(
            {
                "policy_id": self.policy_id,
                "policy_version": self.policy_version,
                "retention_seconds": self.retention_seconds,
            }
        )

    def due_at(self, terminated_at: int) -> int:
        return int(terminated_at) + int(self.retention_seconds)


@dataclass(frozen=True)
class CopyInventoryEntry:
    """One value-free manifest entry for one owner copy collection."""

    owner_id: str
    copy_class: str
    classification: str = "RESTRICTED"
    content_sha256: str = ""
    identity_fingerprint: str = ""
    retention_policy_id: str = S16_POLICY_ID
    retention_policy_version: str = S16_POLICY_VERSION
    retention_due_at: int | None = None
    legal_hold_generation: int = 0
    hold_state: str = "none"
    shared_state: str = "exclusive"
    planned_action: str = "delete"
    count: int = 0


class DeletionOwner(Protocol):
    owner_id: str

    def inventory(self, scope_fingerprint: str) -> list[CopyInventoryEntry]:
        """Value-free copy inventory for one scope; raises S16NotFound for
        unknown scopes and S16Unavailable when the owner is missing."""

    def delete(
        self,
        copy_fingerprints: Iterable[str],
        *,
        scope_fingerprint: str,
        operation_id: str,
        fence: int,
    ) -> dict[str, Any]:
        """Delete the named copies; monotonic idempotent (absent is success)."""

    def verify_absent(
        self, copy_fingerprints: Iterable[str], *, scope_fingerprint: str
    ) -> dict[str, Any]:
        """Prove the named copies are absent."""

    def replay(
        self, copy_fingerprints: Iterable[str], *, scope_fingerprint: str
    ) -> dict[str, Any]:
        """Restore-time replay: idempotently re-delete the named copies."""

    def verify_repair(self, owner_id: str, repair_fact: str) -> bool:
        """True when the owner accepts the operator repair fact."""


# ---------------------------------------------------------------------------
# The independent ledger
# ---------------------------------------------------------------------------


class S16Ledger:
    """Independent SQLite ledger for S16: append-only events, mutable jobs,
    append-only receipts.  One short transaction writes command event,
    audit event, idempotency binding and job together (ADR-0003)."""

    def __init__(self, state_path: str | Path) -> None:
        self.state_path = str(state_path)
        Path(self.state_path).parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.state_path, timeout=10.0)
        connection.isolation_level = None
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS s16_events (
                    event_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    integrity_sha256 TEXT NOT NULL,
                    appended_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS s16_jobs (
                    job_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS s16_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    integrity_sha256 TEXT NOT NULL,
                    appended_at INTEGER NOT NULL
                )
                """
            )
            connection.commit()

    @staticmethod
    def _integrity_digest(table: str, item_id: str, payload: str) -> str:
        material = "\0".join(
            ("s16-integrity/1", table, item_id, payload)
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def _append_event(
        self, connection: sqlite3.Connection, event: dict[str, Any], now: int
    ) -> None:
        event_id = str(event.get("event_id") or "")
        if not event_id:
            raise ValueError("S16 event requires a stable event id")
        payload = _canonical(event)
        digest = self._integrity_digest("s16_events", event_id, payload)
        connection.execute(
            "INSERT INTO s16_events(event_id, payload, integrity_sha256, appended_at) "
            "VALUES (?, ?, ?, ?)",
            (event_id, payload, digest, now),
        )

    def _load_events(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id, payload, integrity_sha256 FROM s16_events"
            ).fetchall()
        events: list[dict[str, Any]] = []
        for event_id, payload, declared_digest in rows:
            if self._integrity_digest("s16_events", event_id, payload) != declared_digest:
                raise S16Unavailable("S16 ledger event integrity failed")
            events.append(json.loads(payload))
        events.sort(key=lambda event: int(event.get("appended_at") or 0))
        return events

    def _upsert_job(
        self, connection: sqlite3.Connection, job: dict[str, Any], now: int
    ) -> None:
        connection.execute(
            "INSERT INTO s16_jobs(job_id, payload, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET payload = excluded.payload, "
            "updated_at = excluded.updated_at",
            (str(job["job_id"]), _canonical(job), now),
        )

    def _load_jobs(self) -> dict[str, dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id, payload FROM s16_jobs"
            ).fetchall()
        return {job_id: json.loads(payload) for job_id, payload in rows}

    def _append_receipt(
        self,
        connection: sqlite3.Connection,
        receipt: dict[str, Any],
        now: int,
    ) -> None:
        receipt_id = str(receipt.get("receipt_id") or "")
        if not receipt_id:
            raise ValueError("S16 receipt requires a stable receipt id")
        payload = _canonical(receipt)
        digest = self._integrity_digest("s16_receipts", receipt_id, payload)
        connection.execute(
            "INSERT OR REPLACE INTO s16_receipts("
            "receipt_id, payload, integrity_sha256, appended_at) "
            "VALUES (?, ?, ?, ?)",
            (receipt_id, payload, digest, now),
        )

    def _load_receipts(self) -> dict[str, dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT receipt_id, payload, integrity_sha256 FROM s16_receipts"
            ).fetchall()
        receipts: dict[str, dict[str, Any]] = {}
        for receipt_id, payload, declared_digest in rows:
            if (
                self._integrity_digest("s16_receipts", receipt_id, payload)
                != declared_digest
            ):
                raise S16Unavailable("S16 ledger receipt integrity failed")
            receipts[receipt_id] = json.loads(payload)
        return receipts


# ---------------------------------------------------------------------------
# Owner adapters
# ---------------------------------------------------------------------------


class S01DeletionOwner:
    """S01 structured business authority adapter (lifecycle owner).

    Inventory/apply go through the ControlledScenarioService public S16
    seam; the orchestrator never writes S01 tables directly.
    """

    owner_id = "s01"

    def __init__(
        self,
        service: Any,
        *,
        retention: RetentionPolicy,
        clock: Callable[[], int],
    ) -> None:
        self._service = service
        self._retention = retention
        self._clock = clock

    def _resolve_application_id(self, scope_fingerprint: str) -> str:
        application_id = self._service.s16_resolve_by_scope_fingerprint(
            scope_fingerprint
        )
        if application_id is None:
            raise S16NotFound(scope_fingerprint)
        return application_id

    def inventory(self, scope_fingerprint: str) -> list[CopyInventoryEntry]:
        application_id = self._resolve_application_id(scope_fingerprint)
        facts = self._service.s16_inventory(application_id)
        terminated_at = facts.get("terminated_at")
        due_at = (
            self._retention.due_at(int(terminated_at))
            if terminated_at is not None
            else None
        )
        entries: list[CopyInventoryEntry] = []
        for copy_class in (
            COPY_CLASS_SOURCE_OBJECT,
            COPY_CLASS_EVIDENCE,
            COPY_CLASS_RUN_OR_FINDING,
            COPY_CLASS_PROJECTION_OR_CACHE,
        ):
            class_facts = facts["classes"][copy_class]
            content_sha256 = str(class_facts["content_sha256"])
            identity_fingerprint = copy_identity_fingerprint(
                self.owner_id, copy_class, content_sha256
            )
            entries.append(
                CopyInventoryEntry(
                    owner_id=self.owner_id,
                    copy_class=copy_class,
                    content_sha256=content_sha256,
                    identity_fingerprint=identity_fingerprint,
                    retention_policy_id=self._retention.policy_id,
                    retention_policy_version=self._retention.policy_version,
                    retention_due_at=due_at,
                    shared_state=str(class_facts.get("shared_state") or "exclusive"),
                    planned_action=(
                        S16_SHARED_COPY_REQUIRES_REPACK
                        if class_facts.get("shared_state") == "shared"
                        else "delete"
                    ),
                    count=int(class_facts.get("count") or 0),
                )
            )
        return entries

    def delete(
        self,
        copy_fingerprints: Iterable[str],
        *,
        scope_fingerprint: str,
        operation_id: str,
        fence: int,
    ) -> dict[str, Any]:
        del copy_fingerprints
        application_id = self._service.s16_resolve_by_scope_fingerprint(
            scope_fingerprint
        )
        if application_id is None:
            if self._service.s16_tombstone_verified(scope_fingerprint):
                # Monotonic idempotency: a duplicate delivery after the
                # committed deletion is already a complete success.
                return {
                    "owner_id": self.owner_id,
                    "status": "complete",
                    "deleted_counts": {},
                    "already_absent": True,
                }
            raise S16OwnerFailure(
                self.owner_id,
                S16_OWNER_INTEGRITY,
                retryable=False,
                responsible_party="platform_storage_owner",
                recovery_action="verify_owner_state_and_repair",
            )
        now = int(self._clock())
        receipt = {
            "deletion_receipt_id": _stable_id(
                "s16deletion", f"{operation_id}:{fence}"
            ),
            "action": "s16_governed_deletion",
            "policy": "s16-governed-deletion/1",
            "scope_fingerprint": scope_fingerprint,
            "deleted_at": now,
            "result": "deleted",
            "subject": "s16-deletion-worker",
            "role": "system",
        }
        try:
            result = self._service.s16_apply_deletion(
                application_id,
                receipt,
                operation_id=operation_id,
                fence=fence,
                scope_fingerprint=scope_fingerprint,
            )
        except S16Blocked as error:
            raise S16OwnerFailure(
                self.owner_id,
                error.reason_code,
                retryable=False,
                responsible_party="platform_storage_owner",
                recovery_action="resolve_the_blocking_condition_and_repair",
            ) from error
        return {
            "owner_id": self.owner_id,
            "status": "complete",
            "deleted_counts": result.get("deleted_counts", {}),
        }

    def verify_absent(
        self, copy_fingerprints: Iterable[str], *, scope_fingerprint: str
    ) -> dict[str, Any]:
        if not set(copy_fingerprints):
            return {"owner_id": self.owner_id, "status": "verified"}
        result = self._service.s16_verify_absent(scope_fingerprint)
        if not result.get("absent"):
            raise S16OwnerFailure(
                self.owner_id,
                S16_VERIFY_FAILED,
                retryable=True,
                responsible_party="runtime_operations_owner",
                recovery_action="verify_owner_absence_and_resume_the_same_job",
            )
        return {"owner_id": self.owner_id, "status": "verified"}

    def replay(
        self, copy_fingerprints: Iterable[str], *, scope_fingerprint: str
    ) -> dict[str, Any]:
        if not set(copy_fingerprints):
            return {"owner_id": self.owner_id, "status": "replayed"}
        application_id = self._service.s16_resolve_by_scope_fingerprint(
            scope_fingerprint
        )
        if application_id is None:
            # Already absent on this owner: replay is idempotently complete.
            return {"owner_id": self.owner_id, "status": "replayed", "already_absent": True}
        now = int(self._clock())
        receipt = {
            "deletion_receipt_id": _stable_id(
                "s16replay", f"{scope_fingerprint}:{now}"
            ),
            "action": "s16_governed_deletion_replay",
            "policy": "s16-governed-deletion/1",
            "scope_fingerprint": scope_fingerprint,
            "deleted_at": now,
            "result": "deleted",
            "subject": "s16-restore-replay",
            "role": "system",
        }
        self._service.s16_apply_deletion(
            application_id,
            receipt,
            operation_id=f"s16-replay:{scope_fingerprint[:24]}",
            fence=0,
            scope_fingerprint=scope_fingerprint,
        )
        return {"owner_id": self.owner_id, "status": "replayed"}

    def verify_repair(self, owner_id: str, repair_fact: str) -> bool:
        if owner_id != self.owner_id:
            return False
        try:
            self._service.s16_owner_healthy()
        except Exception:
            return False
        return repair_fact == "s01-repair-verified"


class S02DeletionOwner:
    """Registered source object owner adapter (S02 boundary)."""

    owner_id = "s02"

    def __init__(self, boundary: Any, s01_service: Any) -> None:
        self._boundary = boundary
        self._s01_service = s01_service

    def inventory(self, scope_fingerprint: str) -> list[CopyInventoryEntry]:
        application_id = self._s01_service.s16_resolve_by_scope_fingerprint(
            scope_fingerprint
        )
        if application_id is None:
            raise S16NotFound(scope_fingerprint)
        target_digests = self._s01_service.s16_referenced_object_digests(
            application_id
        )
        other_digests: set[str] = set()
        for other_id in self._s01_service.s16_application_ids():
            if other_id == application_id:
                continue
            other_digests.update(
                self._s01_service.s16_referenced_object_digests(other_id)
            )
        facts = self._boundary.s02_inventory()
        entries: list[CopyInventoryEntry] = []
        for item in facts["objects"]:
            content_sha256 = str(item["content_sha256"])
            if content_sha256 not in target_digests:
                continue
            shared = content_sha256 in other_digests
            identity_fingerprint = copy_identity_fingerprint(
                self.owner_id, COPY_CLASS_DERIVED_OBJECT, content_sha256
            )
            entries.append(
                CopyInventoryEntry(
                    owner_id=self.owner_id,
                    copy_class=COPY_CLASS_DERIVED_OBJECT,
                    content_sha256=content_sha256,
                    identity_fingerprint=identity_fingerprint,
                    shared_state="shared" if shared else "exclusive",
                    planned_action=(
                        S16_SHARED_COPY_REQUIRES_REPACK
                        if shared
                        else "delete"
                    ),
                    count=1,
                )
            )
        if not entries:
            entries.append(
                zero_proof_entry(
                    self.owner_id,
                    COPY_CLASS_DERIVED_OBJECT,
                    reason="no_referenced_objects",
                )
            )
        return entries

    def delete(
        self,
        copy_fingerprints: Iterable[str],
        *,
        scope_fingerprint: str,
        operation_id: str,
        fence: int,
    ) -> dict[str, Any]:
        del scope_fingerprint
        fingerprints = sorted(set(copy_fingerprints))
        result = self._boundary.s02_delete(fingerprints)
        return {
            "owner_id": self.owner_id,
            "status": result.get("status", "complete"),
            "deleted_counts": result.get("deleted_counts", {}),
        }

    def verify_absent(
        self, copy_fingerprints: Iterable[str], *, scope_fingerprint: str
    ) -> dict[str, Any]:
        del scope_fingerprint
        fingerprints = sorted(set(copy_fingerprints))
        result = self._boundary.s02_verify_absent(fingerprints)
        if not result.get("absent"):
            raise S16OwnerFailure(
                self.owner_id,
                S16_VERIFY_FAILED,
                retryable=True,
                responsible_party="platform_storage_owner",
                recovery_action="verify_object_absence_and_resume_the_same_job",
            )
        return {"owner_id": self.owner_id, "status": "verified"}

    def replay(
        self, copy_fingerprints: Iterable[str], *, scope_fingerprint: str
    ) -> dict[str, Any]:
        del scope_fingerprint
        fingerprints = sorted(set(copy_fingerprints))
        result = self._boundary.s02_replay(fingerprints)
        return {"owner_id": self.owner_id, "status": result.get("status", "replayed")}

    def verify_repair(self, owner_id: str, repair_fact: str) -> bool:
        if owner_id != self.owner_id:
            return False
        return self._boundary.s02_verify_repair(repair_fact)


class S12DeletionOwner:
    """Isolated evaluation copy owner adapter (S12 plane)."""

    owner_id = "s12"

    def __init__(self, evaluation_service: Any) -> None:
        self._service = evaluation_service

    def inventory(self, scope_fingerprint: str) -> list[CopyInventoryEntry]:
        facts = self._service.s16_enumerate_scope(scope_fingerprint)
        entries: list[CopyInventoryEntry] = []
        for row_type in ("plans", "jobs", "bundles"):
            for item in facts[row_type]:
                content_sha256 = str(item["content_sha256"])
                identity_fingerprint = copy_identity_fingerprint(
                    self.owner_id, COPY_CLASS_EVALUATION_COPY, content_sha256
                )
                entries.append(
                    CopyInventoryEntry(
                        owner_id=self.owner_id,
                        copy_class=COPY_CLASS_EVALUATION_COPY,
                        content_sha256=content_sha256,
                        identity_fingerprint=identity_fingerprint,
                        shared_state=str(item.get("shared_state") or "exclusive"),
                        planned_action=(
                            S16_SHARED_COPY_REQUIRES_REPACK
                            if item.get("shared_state") == "shared"
                            else "delete"
                        ),
                        count=1,
                    )
                )
        if not entries:
            entries.append(
                zero_proof_entry(
                    self.owner_id,
                    COPY_CLASS_EVALUATION_COPY,
                    reason="no_referenced_rows",
                )
            )
        return entries

    def delete(
        self,
        copy_fingerprints: Iterable[str],
        *,
        scope_fingerprint: str,
        operation_id: str,
        fence: int,
    ) -> dict[str, Any]:
        del scope_fingerprint
        fingerprints = sorted(set(copy_fingerprints))
        result = self._service.s16_delete_scope(fingerprints)
        return {
            "owner_id": self.owner_id,
            "status": "complete",
            "deleted_counts": result.get("deleted_counts", {}),
        }

    def verify_absent(
        self, copy_fingerprints: Iterable[str], *, scope_fingerprint: str
    ) -> dict[str, Any]:
        del scope_fingerprint
        fingerprints = sorted(set(copy_fingerprints))
        result = self._service.s16_verify_absent(fingerprints)
        if not result.get("absent"):
            raise S16OwnerFailure(
                self.owner_id,
                S16_VERIFY_FAILED,
                retryable=True,
                responsible_party="evaluation_plane_owner",
                recovery_action="verify_evaluation_absence_and_resume_the_same_job",
            )
        return {"owner_id": self.owner_id, "status": "verified"}

    def replay(
        self, copy_fingerprints: Iterable[str], *, scope_fingerprint: str
    ) -> dict[str, Any]:
        del scope_fingerprint
        fingerprints = sorted(set(copy_fingerprints))
        result = self._service.s16_replay_scope(fingerprints)
        return {"owner_id": self.owner_id, "status": result.get("status", "replayed")}

    def verify_repair(self, owner_id: str, repair_fact: str) -> bool:
        if owner_id != self.owner_id:
            return False
        try:
            self._service.s16_owner_healthy()
        except Exception:
            return False
        return repair_fact == "s12-repair-verified"


class BackupDeletionOwner:
    """Scope-scoped backup copies + restore manifests (backup owner).

    ``capture`` is called by the backup system at backup time and stores a
    value-free manifest (scope fingerprint + content digests only).  Delete
    removes the captured copies; replay re-deletes after an old backup
    restore so readiness stays gated until absence is verified."""

    owner_id = "backup"

    def __init__(self, root: str | Path, *, clock: Callable[[], int]) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._clock = clock

    def _manifest_path(self, manifest_id: str) -> Path:
        return self._root / f"{manifest_id}.json"

    def capture(
        self,
        *,
        scope_fingerprint: str,
        copy_files: Iterable[tuple[str, str]],
    ) -> dict[str, Any]:
        """Record one scope-scoped backup: file name + content digest pairs.

        Called by the backup system at backup time; the manifest stores no
        paths, values, application ids or references — only digests and the
        value-free scope fingerprint.
        """
        files = [
            {"name": name, "content_sha256": content_sha256}
            for name, content_sha256 in sorted(copy_files)
        ]
        if not files:
            raise ValueError("S16 backup capture requires at least one file")
        manifest_id = _stable_id(
            "backup", f"{scope_fingerprint}:{int(self._clock())}"
        )
        manifest = {
            "schema_version": "s16-backup-manifest/1",
            "manifest_id": manifest_id,
            "scope_fingerprint": scope_fingerprint,
            "captured_at": int(self._clock()),
            "files": files,
            "entries_digest": _digest(files),
        }
        self._manifest_path(manifest_id).write_text(
            _canonical(manifest), encoding="utf-8"
        )
        return manifest

    def _load_manifests(self) -> list[dict[str, Any]]:
        manifests: list[dict[str, Any]] = []
        for path in sorted(self._root.glob("backup_*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise S16Unavailable("S16 backup manifest is unreadable") from error
            if not isinstance(value, dict) or value.get("schema_version") != "s16-backup-manifest/1":
                raise S16Unavailable("S16 backup manifest schema is invalid")
            manifests.append(value)
        return manifests

    def inventory(self, scope_fingerprint: str) -> list[CopyInventoryEntry]:
        entries: list[CopyInventoryEntry] = []
        for manifest in self._load_manifests():
            if manifest.get("scope_fingerprint") != scope_fingerprint:
                continue
            file_digest = _digest(manifest["files"])
            replica_fingerprint = copy_identity_fingerprint(
                self.owner_id, COPY_CLASS_REPLICA, file_digest
            )
            entries.append(
                CopyInventoryEntry(
                    owner_id=self.owner_id,
                    copy_class=COPY_CLASS_REPLICA,
                    content_sha256=file_digest,
                    identity_fingerprint=replica_fingerprint,
                    count=len(manifest["files"]),
                )
            )
            manifest_digest = _digest(manifest)
            manifest_fingerprint = copy_identity_fingerprint(
                self.owner_id, COPY_CLASS_BACKUP_MANIFEST, manifest_digest
            )
            entries.append(
                CopyInventoryEntry(
                    owner_id=self.owner_id,
                    copy_class=COPY_CLASS_BACKUP_MANIFEST,
                    content_sha256=manifest_digest,
                    identity_fingerprint=manifest_fingerprint,
                    count=1,
                )
            )
        if not entries:
            entries.append(
                zero_proof_entry(
                    self.owner_id, COPY_CLASS_REPLICA, reason="no_captured_copies"
                )
            )
            entries.append(
                zero_proof_entry(
                    self.owner_id,
                    COPY_CLASS_BACKUP_MANIFEST,
                    reason="no_captured_manifests",
                )
            )
        return entries

    def _manifests_for_fingerprints(
        self, copy_fingerprints: set[str]
    ) -> list[dict[str, Any]]:
        if not copy_fingerprints:
            return []
        return [
            manifest
            for manifest in self._load_manifests()
            if copy_identity_fingerprint(
                self.owner_id, COPY_CLASS_REPLICA, _digest(manifest["files"])
            )
            in copy_fingerprints
            or copy_identity_fingerprint(
                self.owner_id, COPY_CLASS_BACKUP_MANIFEST, _digest(manifest)
            )
            in copy_fingerprints
        ]

    def delete(
        self,
        copy_fingerprints: Iterable[str],
        *,
        scope_fingerprint: str,
        operation_id: str,
        fence: int,
    ) -> dict[str, Any]:
        del scope_fingerprint
        fingerprints = set(copy_fingerprints)
        deleted = 0
        for manifest in self._manifests_for_fingerprints(fingerprints):
            for file_entry in manifest["files"]:
                target = self._root / str(file_entry["name"])
                if target.is_file() and hashlib.sha256(
                    target.read_bytes()
                ).hexdigest() == str(file_entry["content_sha256"]):
                    target.unlink()
                    deleted += 1
            manifest_path = self._manifest_path(str(manifest["manifest_id"]))
            if manifest_path.is_file():
                manifest_path.unlink()
        return {"status": "complete", "deleted_counts": {"replica": deleted}}

    def verify_absent(
        self, copy_fingerprints: Iterable[str], *, scope_fingerprint: str
    ) -> dict[str, Any]:
        del scope_fingerprint
        fingerprints = set(copy_fingerprints)
        remaining = self._manifests_for_fingerprints(fingerprints)
        if remaining:
            raise S16OwnerFailure(
                self.owner_id,
                S16_VERIFY_FAILED,
                retryable=True,
                responsible_party="backup_operations_owner",
                recovery_action="verify_backup_absence_and_resume_the_same_job",
            )
        return {"owner_id": self.owner_id, "status": "verified"}

    def replay(
        self, copy_fingerprints: Iterable[str], *, scope_fingerprint: str
    ) -> dict[str, Any]:
        return self.delete(
            copy_fingerprints,
            scope_fingerprint=scope_fingerprint,
            operation_id="s16-backup-replay",
            fence=0,
        )

    def verify_repair(self, owner_id: str, repair_fact: str) -> bool:
        if owner_id != self.owner_id:
            return False
        return repair_fact == "backup-repair-verified"


class ExportTempOwner:
    """The disabled S17 owner: documented zero inventory with a proof.

    While S17 export stays closed there are no export/temp copies to
    govern; the owner still appears in the manifest so the nine-class
    contract holds."""

    owner_id = "s17-disabled"

    def __init__(self) -> None:
        pass

    def inventory(self, scope_fingerprint: str) -> list[CopyInventoryEntry]:
        del scope_fingerprint
        return [
            zero_proof_entry(
                self.owner_id,
                COPY_CLASS_EXPORT_OR_TEMP,
                reason="s17_export_disabled",
            )
        ]

    def delete(
        self,
        copy_fingerprints: Iterable[str],
        *,
        scope_fingerprint: str,
        operation_id: str,
        fence: int,
    ) -> dict[str, Any]:
        del copy_fingerprints, scope_fingerprint, operation_id, fence
        return {"status": "complete", "deleted_counts": {}}

    def verify_absent(
        self, copy_fingerprints: Iterable[str], *, scope_fingerprint: str
    ) -> dict[str, Any]:
        del copy_fingerprints, scope_fingerprint
        return {"owner_id": self.owner_id, "status": "verified"}

    def replay(
        self, copy_fingerprints: Iterable[str], *, scope_fingerprint: str
    ) -> dict[str, Any]:
        del copy_fingerprints, scope_fingerprint
        return {"owner_id": self.owner_id, "status": "replayed"}

    def verify_repair(self, owner_id: str, repair_fact: str) -> bool:
        return owner_id == self.owner_id


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class GovernedDeletionService:
    """The S16 orchestrator: preflight, legal hold, approvals, commit,
    durable worker, repair, receipts and startup restore replay."""

    def __init__(
        self,
        *,
        ledger_path: str | Path,
        owners: dict[str, DeletionOwner],
        retention: RetentionPolicy,
        governance_subject: str,
        approver_subjects: Iterable[str],
        worker_id: str = "s16-deletion-worker",
        audit_available: bool = True,
        storage_available: bool = True,
        max_owner_attempts: int = MAX_OWNER_ATTEMPTS,
        clock: Callable[[], int] | None = None,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        if not governance_subject or governance_subject.strip() != governance_subject:
            raise ValueError("S16 governance subject must be canonical")
        approvers = tuple(dict.fromkeys(approver_subjects))
        if len(approvers) < 2 or any(
            not subject or subject.strip() != subject for subject in approvers
        ):
            raise ValueError("S16 requires two distinct canonical approver subjects")
        if governance_subject in approvers:
            raise ValueError("S16 governance subject must not alias an approver")
        missing = REQUIRED_OWNERS.difference(owners)
        if missing:
            raise ValueError(
                f"S16 required owners are missing: {sorted(missing)}"
            )
        self._ledger = S16Ledger(ledger_path)
        self._owners = dict(owners)
        self._retention = retention
        self._governance_subject = governance_subject
        self._approvers = frozenset(approvers)
        self._worker_id = worker_id
        self.audit_available = bool(audit_available)
        self.storage_available = bool(storage_available)
        if (
            isinstance(max_owner_attempts, bool)
            or not isinstance(max_owner_attempts, int)
            or max_owner_attempts < 1
        ):
            raise ValueError("S16 max owner attempts must be a positive integer")
        self._max_owner_attempts = max_owner_attempts
        self._clock = clock or (lambda: int(time.time()))
        self._fault_injector = fault_injector
        self._lock = threading.RLock()
        self._events = self._ledger._load_events()
        self._jobs = self._ledger._load_jobs()
        self._receipts = self._ledger._load_receipts()
        self._replay_restore()

    # -- internal helpers ---------------------------------------------------

    def _now(self) -> int:
        return int(self._clock())

    def _event_id(self, kind: str, fingerprint: str) -> str:
        return _stable_id(f"s16:{kind}", f"{self._now()}:{fingerprint}:{secrets.token_hex(4)}")

    def _events_of(self, event_type: str, request_id: str | None = None) -> list[dict[str, Any]]:
        return [
            event
            for event in self._events
            if event.get("event_type") == event_type
            and (request_id is None or event.get("request_id") == request_id)
        ]

    def _active_hold_union(self, scope_fingerprint: str) -> list[dict[str, Any]]:
        holds: list[dict[str, Any]] = []
        for event in self._events:
            if (
                event.get("event_type") == "legal_hold_imposed"
                and event.get("scope_fingerprint") == scope_fingerprint
            ):
                hold_id = str(event.get("hold_id") or "")
                released = any(
                    item.get("event_type") == "legal_hold_released"
                    and item.get("hold_id") == hold_id
                    for item in self._events
                )
                expiry = event.get("expiry")
                expired = (
                    isinstance(expiry, (int, float))
                    and not isinstance(expiry, bool)
                    and int(expiry) <= self._now()
                )
                if not released and not expired:
                    holds.append(event)
        return holds

    def _hold_generation(self, scope_fingerprint: str) -> int:
        return len(
            [
                event
                for event in self._events
                if event.get("event_type") == "legal_hold_imposed"
                and event.get("scope_fingerprint") == scope_fingerprint
            ]
        )

    def _approvals_for(self, request_id: str) -> list[dict[str, Any]]:
        return self._events_of("approval", request_id)

    def _request_events(self, request_id: str) -> list[dict[str, Any]]:
        return self._events_of("request", request_id)

    def _preflight_events(self, request_id: str) -> list[dict[str, Any]]:
        return self._events_of("preflight", request_id)

    def _job_for_request(self, request_id: str) -> dict[str, Any] | None:
        for job in self._jobs.values():
            if job.get("request_id") == request_id:
                return job
        return None

    def _require_governance(self, principal: Any) -> str:
        subject = getattr(principal, "subject", None)
        if subject != self._governance_subject:
            raise S16Forbidden("governed deletion governance identity required")
        return str(subject)

    def _require_approver(self, principal: Any) -> str:
        subject = getattr(principal, "subject", None)
        if subject not in self._approvers:
            raise S16Forbidden("registered S16 deletion approver required")
        return str(subject)

    def _idempotency_binding(self, key: str) -> tuple[str, dict[str, Any]] | None:
        for event in self._events:
            if (
                event.get("event_type") == "idempotency_binding"
                and event.get("binding_key") == key
            ):
                return str(event.get("fingerprint") or ""), event
        return None

    def _record_idempotency(
        self,
        connection: sqlite3.Connection,
        *,
        key: str,
        fingerprint: str,
        result: dict[str, Any],
    ) -> None:
        self._ledger._append_event(
            connection,
            {
                "event_type": "idempotency_binding",
                "binding_key": key,
                "fingerprint": fingerprint,
                "result": result,
                "event_id": self._event_id("idempotency", key),
                "appended_at": self._now(),
            },
            self._now(),
        )

    def _binding_key(self, principal_subject: str, idempotency_key: str) -> str:
        return _digest(
            {"subject": principal_subject, "idempotency_key": idempotency_key}
        )

    def _reload(self) -> None:
        self._events = self._ledger._load_events()
        self._jobs = self._ledger._load_jobs()
        self._receipts = self._ledger._load_receipts()

    def _validate_principal(self, principal: Any, role: str) -> str:
        if not isinstance(principal, dict) and not hasattr(principal, "subject"):
            raise S16Forbidden("principal is invalid")
        subject = getattr(principal, "subject", principal.get("subject"))
        if not isinstance(subject, str) or not subject:
            raise S16Forbidden("principal is invalid")
        return subject

    # -- preflight ----------------------------------------------------------

    def preflight(
        self,
        *,
        application_reference: str,
        principal: Any,
        idempotency_key: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        subject = self._require_governance(principal)
        if not isinstance(application_reference, str) or not application_reference:
            raise S16Blocked("S16_INVALID_REFERENCE")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise S16Blocked("S16_INVALID_IDEMPOTENCY_KEY")
        request_id = request_id or _stable_id(
            "s16req", f"{subject}:{application_reference}:{secrets.token_hex(4)}"
        )
        if len(request_id) > 200 or request_id.strip() != request_id:
            raise S16Blocked("S16_INVALID_REQUEST_ID")
        binding_key = self._binding_key(subject, idempotency_key)
        command_fingerprint = _digest(
            {
                "action": "preflight",
                "application_reference_fingerprint": hashlib.sha256(
                    application_reference.encode("utf-8")
                ).hexdigest(),
                "subject": subject,
            }
        )
        with self._lock:
            self._reload()
            previous = self._idempotency_binding(binding_key)
            if previous is not None:
                previous_fingerprint, event = previous
                if previous_fingerprint == command_fingerprint:
                    # Same key + same content: return the original outcome.
                    # The ledger stores only the value-free facts; the
                    # manifest itself is re-derived from the same scope so
                    # the caller can continue approvals in this session.
                    stored = event.get("result") or {}
                    replayed_request_id = str(stored.get("request_id") or "")
                    scope = self._service_scope(principal)
                    application_id = self._owners[
                        "s01"
                    ]._service.s16_resolve_application(
                        upstream_application_reference=application_reference,
                        scope=scope,
                    )
                    if application_id is None:
                        raise S16NotFound(application_reference)
                    manifest = self._build_manifest(application_id, scope)
                    return self._preflight_response(
                        manifest=manifest,
                        request_id=replayed_request_id,
                        application_reference=application_reference,
                        replayed=True,
                    )
                raise S16Conflict(
                    "S16 idempotency conflict: same key different content"
                )
            scope = self._service_scope(principal)
            application_id = self._owners["s01"]._service.s16_resolve_application(
                upstream_application_reference=application_reference,
                scope=scope,
            )
            if application_id is None:
                raise S16NotFound(application_reference)
            manifest = self._build_manifest(application_id, scope)
            request_event = {
                "event_type": "request",
                "request_id": request_id,
                "subject": subject,
                "role": "governance_owner",
                "action": "preflight",
                "application_reference_fingerprint": hashlib.sha256(
                    application_reference.encode("utf-8")
                ).hexdigest(),
                "scope_fingerprint": manifest["scope_fingerprint"],
                "appended_at": self._now(),
                "event_id": self._event_id("request", request_id),
            }
            preflight_event = {
                "event_type": "preflight",
                "request_id": request_id,
                "scope_fingerprint": manifest["scope_fingerprint"],
                "manifest_digest": manifest["manifest_digest"],
                "entries_digest": manifest["entries_digest"],
                "owner_registry_digest": manifest["owner_registry_digest"],
                "s01_revision": manifest["s01_revision"],
                "s12_revision": manifest["s12_revision"],
                "policy_digest": manifest["policy_digest"],
                "retention_due": manifest["retention_due"],
                "early_deletion": manifest["early_deletion"],
                "retained_scan_clean": manifest["retained_scan_clean"],
                "retained_scan_digest": manifest["retained_scan_digest"],
                "appended_at": self._now(),
                "event_id": self._event_id("preflight", request_id),
            }
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._ledger._append_event(connection, request_event, self._now())
                self._ledger._append_event(connection, preflight_event, self._now())
                self._record_idempotency(
                    connection,
                    key=binding_key,
                    fingerprint=command_fingerprint,
                    result={"status": "accepted", "request_id": request_id},
                )
                connection.commit()
            self._reload()
        return self._preflight_response(
            manifest=manifest,
            request_id=request_id,
            application_reference=application_reference,
        )

    def _preflight_response(
        self,
        *,
        manifest: dict[str, Any],
        request_id: str,
        application_reference: str,
        replayed: bool = False,
    ) -> dict[str, Any]:
        return {
            "status": "accepted",
            "request_id": request_id,
            "application_reference": application_reference,
            "scope_fingerprint": manifest["scope_fingerprint"],
            "manifest_digest": manifest["manifest_digest"],
            "entries_digest": manifest["entries_digest"],
            "owner_registry_digest": manifest["owner_registry_digest"],
            "s01_revision": manifest["s01_revision"],
            "s12_revision": manifest["s12_revision"],
            "policy_digest": manifest["policy_digest"],
            "retention_due": manifest["retention_due"],
            "early_deletion": manifest["early_deletion"],
            "retained_scan_clean": manifest["retained_scan_clean"],
            "entries": [
                {
                    "owner_id": entry.owner_id,
                    "copy_class": entry.copy_class,
                    "classification": entry.classification,
                    "content_sha256": entry.content_sha256,
                    "identity_fingerprint": entry.identity_fingerprint,
                    "retention_policy_id": entry.retention_policy_id,
                    "retention_policy_version": entry.retention_policy_version,
                    "retention_due_at": entry.retention_due_at,
                    "legal_hold_generation": self._hold_generation(
                        manifest["scope_fingerprint"]
                    ),
                    "hold_state": (
                        "held"
                        if self._active_hold_union(manifest["scope_fingerprint"])
                        else "none"
                    ),
                    "shared_state": entry.shared_state,
                    "planned_action": entry.planned_action,
                    "count": entry.count,
                }
                for entry in manifest["entries"]
            ],
            "replayed": replayed,
        }

    def _service_scope(self, principal: Any) -> str:
        scope = getattr(principal, "scope", None)
        if not isinstance(scope, str) or not scope:
            raise S16Forbidden("governance scope is invalid")
        return scope

    def _build_manifest(self, application_id: str, scope: str) -> dict[str, Any]:
        del scope
        s01_owner = self._owners["s01"]
        scope_fingerprint = scope_fingerprint_for(application_id)
        entries: list[CopyInventoryEntry] = []
        for owner_id in sorted(self._owners):
            try:
                owner_entries = self._owners[owner_id].inventory(scope_fingerprint)
            except S16NotFound:
                raise
            except S16Unavailable:
                raise
            except Exception as error:
                raise S16Unavailable(
                    f"S16 owner inventory failed: {owner_id}"
                ) from error
            entries.extend(owner_entries)
        entries = self._fill_retention_due(entries, application_id)
        classes_seen = {entry.copy_class for entry in entries}
        if set(COPY_CLASSES) - classes_seen:
            raise S16Unavailable(
                "S16 manifest is missing registered copy classes"
            )
        for entry in entries:
            if entry.owner_id != OWNER_REGISTRY.get(entry.copy_class):
                raise S16Unavailable(
                    "S16 manifest copy class owner disagrees with the registry"
                )
        entries_digest = _digest(
            [
                {
                    "owner_id": entry.owner_id,
                    "copy_class": entry.copy_class,
                    "content_sha256": entry.content_sha256,
                    "shared_state": entry.shared_state,
                    "planned_action": entry.planned_action,
                    "count": entry.count,
                }
                for entry in entries
            ]
        )
        s01_revision = int(s01_owner._service.s16_store_revision())
        s12_revision = str(
            self._owners["s12"]._service.s16_store_revision()
        )
        retained_scan = s01_owner._service.s16_inventory(
            application_id
        )["retained_scan"]
        manifest_digest = _digest(
            {
                "schema_version": S16_SCHEMA_VERSION,
                "scope_fingerprint": scope_fingerprint,
                "entries_digest": entries_digest,
                "owner_registry_digest": s16_owner_registry_digest(),
                "s01_revision": s01_revision,
                "s12_revision": s12_revision,
                "policy_digest": self._retention.digest(),
                "retained_scan_digest": retained_scan["digest"],
            }
        )
        due_ats = [entry.retention_due_at for entry in entries if entry.retention_due_at is not None]
        early_deletion = any(
            entry.count > 0
            and entry.planned_action == "delete"
            and (
                entry.retention_due_at is None
                or entry.retention_due_at > self._now()
            )
            for entry in entries
        )
        return {
            "schema_version": S16_SCHEMA_VERSION,
            "scope_fingerprint": scope_fingerprint,
            "entries": entries,
            "entries_digest": entries_digest,
            "owner_registry_digest": s16_owner_registry_digest(),
            "s01_revision": s01_revision,
            "s12_revision": s12_revision,
            "policy_digest": self._retention.digest(),
            "manifest_digest": manifest_digest,
            "retention_due": min(due_ats) if due_ats else None,
            "early_deletion": early_deletion,
            "retained_scan_clean": bool(retained_scan["clean"]),
            "retained_scan_digest": retained_scan["digest"],
        }

    def _fill_retention_due(
        self,
        entries: list[CopyInventoryEntry],
        application_id: str,
    ) -> list[CopyInventoryEntry]:
        """Non-S01 owners (S02 objects, evaluation copies, backup copies)
        share the application's retention clock: their due time is derived
        from the S01 terminated fact so an early-deletion decision is
        uniform across every owner."""
        terminated_at = self._owners["s01"]._service.s16_application_terminated_at(
            application_id
        )
        if terminated_at is None:
            return entries
        due_at = self._retention.due_at(int(terminated_at))
        return [
            (
                replace(entry, retention_due_at=due_at)
                if entry.count > 0
                and entry.planned_action == "delete"
                and entry.retention_due_at is None
                else entry
            )
            for entry in entries
        ]

    # -- legal hold ---------------------------------------------------------

    def impose_legal_hold(
        self,
        *,
        scope_fingerprint: str,
        principal: Any,
        reason_code: str,
        owner: str,
        effective_time: int,
        expiry: int | None = None,
    ) -> dict[str, Any]:
        subject = self._require_governance(principal)
        if not isinstance(reason_code, str) or not reason_code:
            raise S16Blocked("S16_INVALID_HOLD")
        if not self.audit_available:
            raise S16Blocked(S16_AUDIT_UNAVAILABLE)
        with self._lock:
            self._reload()
            generation = self._hold_generation(scope_fingerprint) + 1
            hold_id = _stable_id("hold", f"{scope_fingerprint}:{generation}")
            event = {
                "event_type": "legal_hold_imposed",
                "event_id": self._event_id("hold", hold_id),
                "hold_id": hold_id,
                "scope_fingerprint": scope_fingerprint,
                "generation": generation,
                "reason_code": reason_code,
                "owner": owner,
                "effective_time": int(effective_time),
                "expiry": int(expiry) if expiry is not None else None,
                "imposed_by": subject,
                "appended_at": self._now(),
            }
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._ledger._append_event(connection, event, self._now())
                connection.commit()
            self._reload()
        return {
            "status": "accepted",
            "hold_id": hold_id,
            "generation": generation,
            "scope_fingerprint": scope_fingerprint,
        }

    def release_legal_hold(
        self,
        *,
        hold_id: str,
        principal: Any,
    ) -> dict[str, Any]:
        subject = self._require_governance(principal)
        with self._lock:
            self._reload()
            hold = next(
                (
                    event
                    for event in self._events
                    if event.get("event_type") == "legal_hold_imposed"
                    and event.get("hold_id") == hold_id
                ),
                None,
            )
            if hold is None:
                raise S16NotFound(hold_id)
            already_released = any(
                event.get("event_type") == "legal_hold_released"
                and event.get("hold_id") == hold_id
                for event in self._events
            )
            if already_released:
                return {"status": "replayed", "hold_id": hold_id}
            event = {
                "event_type": "legal_hold_released",
                "event_id": self._event_id("hold_release", hold_id),
                "hold_id": hold_id,
                "scope_fingerprint": str(hold.get("scope_fingerprint") or ""),
                "released_by": subject,
                "appended_at": self._now(),
            }
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._ledger._append_event(connection, event, self._now())
                connection.commit()
            self._reload()
        return {"status": "accepted", "hold_id": hold_id}

    # -- approvals ----------------------------------------------------------

    def approve(
        self,
        *,
        request_id: str,
        manifest_digest: str,
        principal: Any,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        subject = self._require_approver(principal)
        if not isinstance(manifest_digest, str) or len(manifest_digest) != 64:
            raise S16Blocked("S16_INVALID_MANIFEST_DIGEST")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise S16Blocked("S16_INVALID_IDEMPOTENCY_KEY")
        binding_key = self._binding_key(subject, idempotency_key)
        command_fingerprint = _digest(
            {
                "action": "approve",
                "request_id": request_id,
                "manifest_digest": manifest_digest,
                "subject": subject,
            }
        )
        with self._lock:
            self._reload()
            previous = self._idempotency_binding(binding_key)
            if previous is not None:
                previous_fingerprint, event = previous
                if previous_fingerprint == command_fingerprint:
                    return {**event.get("result", {}), "replayed": True}
                raise S16Conflict(
                    "S16 idempotency conflict: same key different content"
                )
            requests = self._request_events(request_id)
            if not requests:
                raise S16NotFound(request_id)
            requester = str(requests[0].get("subject") or "")
            if subject == requester:
                raise S16Forbidden("an approver must differ from the requester")
            preflights = self._preflight_events(request_id)
            if not preflights or preflights[0].get("manifest_digest") != manifest_digest:
                raise S16Blocked(S16_MANIFEST_STALE)
            existing = [
                event
                for event in self._events_of("approval", request_id)
                if event.get("subject") == subject
            ]
            if existing:
                return {"status": "replayed", "request_id": request_id, "approved_by": subject}
            event = {
                "event_type": "approval",
                "event_id": self._event_id("approval", request_id),
                "request_id": request_id,
                "subject": subject,
                "role": "deletion_approver",
                "manifest_digest": manifest_digest,
                "scope_fingerprint": str(preflights[0].get("scope_fingerprint") or ""),
                "appended_at": self._now(),
            }
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._ledger._append_event(connection, event, self._now())
                self._record_idempotency(
                    connection,
                    key=binding_key,
                    fingerprint=command_fingerprint,
                    result={
                        "status": "accepted",
                        "request_id": request_id,
                        "approved_by": subject,
                    },
                )
                connection.commit()
            self._reload()
        return {
            "status": "accepted",
            "request_id": request_id,
            "approved_by": subject,
        }

    # -- cancel -------------------------------------------------------------

    def cancel(
        self,
        *,
        request_id: str,
        principal: Any,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        subject = self._require_governance(principal)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise S16Blocked("S16_INVALID_IDEMPOTENCY_KEY")
        binding_key = self._binding_key(subject, idempotency_key)
        command_fingerprint = _digest(
            {"action": "cancel", "request_id": request_id, "subject": subject}
        )
        with self._lock:
            self._reload()
            previous = self._idempotency_binding(binding_key)
            if previous is not None:
                previous_fingerprint, event = previous
                if previous_fingerprint == command_fingerprint:
                    return {**event.get("result", {}), "replayed": True}
                raise S16Conflict(
                    "S16 idempotency conflict: same key different content"
                )
            job = self._job_for_request(request_id)
            if job is not None:
                # Commit is the irreversible boundary.
                raise S16Conflict(S16_ALREADY_COMMITTED)
            if not self._request_events(request_id):
                raise S16NotFound(request_id)
            if any(
                event.get("event_type") == "cancel"
                and event.get("request_id") == request_id
                for event in self._events
            ):
                return {"status": "replayed", "request_id": request_id}
            event = {
                "event_type": "cancel",
                "event_id": self._event_id("cancel", request_id),
                "request_id": request_id,
                "subject": subject,
                "role": "governance_owner",
                "appended_at": self._now(),
            }
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._ledger._append_event(connection, event, self._now())
                self._record_idempotency(
                    connection,
                    key=binding_key,
                    fingerprint=command_fingerprint,
                    result={"status": "accepted", "request_id": request_id},
                )
                connection.commit()
            self._reload()
        return {"status": "accepted", "request_id": request_id}

    # -- commit -------------------------------------------------------------

    def commit(
        self,
        *,
        request_id: str,
        principal: Any,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        subject = self._require_governance(principal)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise S16Blocked("S16_INVALID_IDEMPOTENCY_KEY")
        binding_key = self._binding_key(subject, idempotency_key)
        command_fingerprint = _digest(
            {"action": "commit", "request_id": request_id, "subject": subject}
        )
        with self._lock:
            self._reload()
            previous = self._idempotency_binding(binding_key)
            if previous is not None:
                previous_fingerprint, event = previous
                if previous_fingerprint == command_fingerprint:
                    return {**event.get("result", {}), "replayed": True}
                raise S16Conflict(
                    "S16 idempotency conflict: same key different content"
                )
            if not self._request_events(request_id):
                raise S16NotFound(request_id)
            cancelled = any(
                event.get("event_type") == "cancel"
                and event.get("request_id") == request_id
                for event in self._events
            )
            if cancelled:
                raise S16Blocked(S16_ALREADY_CANCELLED)
            existing_job = self._job_for_request(request_id)
            if existing_job is not None:
                return {
                    "status": "replayed",
                    "request_id": request_id,
                    "job_id": str(existing_job["job_id"]),
                }
            preflights = self._preflight_events(request_id)
            if not preflights:
                raise S16NotFound(request_id)
            manifest = self._restore_manifest(preflights[0])
            scope_fingerprint = str(preflights[0].get("scope_fingerprint") or "")
            if not scope_fingerprint:
                raise S16Unavailable("S16 preflight scope is unavailable")
            # Re-check the held facts inside one short ledger transaction.
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    reason = self._commit_block_reason(
                        scope_fingerprint=scope_fingerprint,
                        manifest=manifest,
                        request_id=request_id,
                        requester=str(
                            (self._request_events(request_id) or [{}])[0].get(
                                "subject"
                            )
                            or ""
                        ),
                        subject=subject,
                    )
                    if reason is not None:
                        raise S16Blocked(reason)
                    job_id = _stable_id(
                        "s16job", f"{scope_fingerprint}:{request_id}"
                    )
                    job = {
                        "job_id": job_id,
                        "request_id": request_id,
                        "scope_fingerprint": scope_fingerprint,
                        "manifest_digest": manifest["manifest_digest"],
                        "status": "pending",
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "fence": 0,
                        "attempt": 0,
                        "pending_owner_fingerprints": self._owner_fingerprint_map(
                            manifest
                        ),
                        "owner_results": {},
                        "stable_failure": None,
                        "created_at": self._now(),
                        "updated_at": self._now(),
                    }
                    commit_event = {
                        "event_type": "commit",
                        "event_id": self._event_id("commit", request_id),
                        "request_id": request_id,
                        "job_id": job_id,
                        "scope_fingerprint": scope_fingerprint,
                        "manifest_digest": manifest["manifest_digest"],
                        "subject": subject,
                        "role": "governance_owner",
                        "appended_at": self._now(),
                    }
                    self._ledger._append_event(connection, commit_event, self._now())
                    self._ledger._upsert_job(connection, job, self._now())
                    self._record_idempotency(
                        connection,
                        key=binding_key,
                        fingerprint=command_fingerprint,
                        result={
                            "status": "accepted",
                            "request_id": request_id,
                            "job_id": job_id,
                        },
                    )
                    connection.commit()
                except S16Blocked:
                    connection.rollback()
                    raise
                except Exception:
                    connection.rollback()
                    raise
            self._reload()
        return {
            "status": "accepted",
            "request_id": request_id,
            "job_id": job_id,
        }

    def _restore_manifest(self, preflight_event: dict[str, Any]) -> dict[str, Any]:
        scope_fingerprint = str(preflight_event.get("scope_fingerprint") or "")
        application_id = self._owners["s01"]._service.s16_resolve_by_scope_fingerprint(
            scope_fingerprint
        )
        if application_id is None:
            raise S16Blocked(S16_MANIFEST_STALE)
        entries: list[CopyInventoryEntry] = []
        for owner_id in sorted(self._owners):
            entries.extend(self._owners[owner_id].inventory(scope_fingerprint))
        application_id_for_due = self._owners["s01"]._service.s16_resolve_by_scope_fingerprint(
            scope_fingerprint
        )
        if application_id_for_due is not None:
            entries = self._fill_retention_due(entries, application_id_for_due)
        entries_digest = _digest(
            [
                {
                    "owner_id": entry.owner_id,
                    "copy_class": entry.copy_class,
                    "content_sha256": entry.content_sha256,
                    "shared_state": entry.shared_state,
                    "planned_action": entry.planned_action,
                    "count": entry.count,
                }
                for entry in entries
            ]
        )
        if entries_digest != str(preflight_event.get("entries_digest") or ""):
            raise S16Blocked(S16_MANIFEST_STALE)
        retained_scan = self._owners["s01"]._service.s16_inventory(
            application_id
        )["retained_scan"]
        if not retained_scan["clean"]:
            raise S16Blocked(S16_RETAINED_VALUE)
        if retained_scan["digest"] != str(
            preflight_event.get("retained_scan_digest") or ""
        ):
            raise S16Blocked(S16_MANIFEST_STALE)
        return {
            "schema_version": S16_SCHEMA_VERSION,
            "scope_fingerprint": scope_fingerprint,
            "entries": entries,
            "entries_digest": entries_digest,
            "owner_registry_digest": s16_owner_registry_digest(),
            "s01_revision": int(preflight_event.get("s01_revision") or 0),
            "s12_revision": str(preflight_event.get("s12_revision") or ""),
            "policy_digest": self._retention.digest(),
            "manifest_digest": str(preflight_event.get("manifest_digest") or ""),
            "retention_due": preflight_event.get("retention_due"),
            "early_deletion": bool(preflight_event.get("early_deletion")),
            "retained_scan_clean": bool(
                preflight_event.get("retained_scan_clean")
            ),
        }

    def _commit_block_reason(
        self,
        *,
        scope_fingerprint: str,
        manifest: dict[str, Any],
        request_id: str,
        requester: str,
        subject: str,
    ) -> str | None:
        if self._active_hold_union(scope_fingerprint):
            return S16_ACTIVE_LEGAL_HOLD
        if self._owners["s01"]._service.s16_store_revision() != manifest["s01_revision"]:
            return S16_REVISION_CHANGED
        if self._owners["s12"]._service.s16_store_revision() != manifest["s12_revision"]:
            return S16_REVISION_CHANGED
        if not self.audit_available:
            return S16_AUDIT_UNAVAILABLE
        if not self.storage_available:
            return S16_STORAGE_UNAVAILABLE
        if not self._owners["s01"]._service.s16_owner_healthy():
            return S16_OWNER_INTEGRITY
        if not self._owners["s12"]._service.s16_owner_healthy():
            return S16_OWNER_INTEGRITY
        if not bool(manifest.get("retained_scan_clean")):
            return S16_RETAINED_VALUE
        shared = [
            entry
            for entry in manifest["entries"]
            if entry.shared_state == "shared" and entry.planned_action == S16_SHARED_COPY_REQUIRES_REPACK
        ]
        if shared:
            return S16_SHARED_COPY_REQUIRES_REPACK
        terminated = self._owners["s01"]._service.s16_is_terminated_by_fingerprint(
            scope_fingerprint
        )
        if not terminated:
            return S16_ACTIVE_APPLICATION
        early = bool(manifest.get("early_deletion"))
        if early:
            approvals = self._approvals_for(request_id)
            approved_subjects = {str(item.get("subject") or "") for item in approvals}
            approved_digests = {
                str(item.get("manifest_digest") or "") for item in approvals
            }
            if (
                len(approved_subjects) < 2
                or requester in approved_subjects
                or approved_digests != {manifest["manifest_digest"]}
            ):
                return S16_APPROVALS_INCOMPLETE
        return None

    def _owner_fingerprint_map(self, manifest: dict[str, Any]) -> dict[str, list[str]]:
        mapping: dict[str, list[str]] = {}
        for entry in manifest["entries"]:
            if entry.planned_action != "delete":
                continue
            mapping.setdefault(entry.owner_id, []).append(entry.identity_fingerprint)
        return {
            owner_id: sorted(set(fingerprints))
            for owner_id, fingerprints in mapping.items()
            if fingerprints
        }

    # -- worker -------------------------------------------------------------

    def process_next_deletion_job(
        self,
        *,
        worker_id: str | None = None,
        now: int | None = None,
    ) -> dict[str, Any]:
        """Claim at most one job and execute one bounded attempt.

        Claiming happens in a short ledger transaction; owner external
        calls run outside the transaction; results publish in one
        transaction (ADR-0003/0004).
        """
        worker = worker_id or self._worker_id
        observed_now = int(now if now is not None else self._clock())
        job = self._claim_job(worker, observed_now)
        if job is None:
            return {"status": "idle", "job_id": None}
        job_id = str(job["job_id"])
        scope_fingerprint = str(job["scope_fingerprint"])
        pending = dict(job.get("pending_owner_fingerprints") or {})
        owner_results: dict[str, Any] = dict(job.get("owner_results") or {})
        try:
            for owner_id in EXECUTION_ORDER:
                if owner_id == "s17-disabled":
                    continue
                if owner_id not in pending:
                    continue
                fingerprints = pending[owner_id]
                if not fingerprints:
                    continue
                try:
                    if self._fault_injector is not None:
                        self._fault_injector(owner_id)
                    outcome = self._owners[owner_id].delete(
                        fingerprints,
                        scope_fingerprint=scope_fingerprint,
                        operation_id=job_id,
                        fence=int(job["fence"]),
                    )
                except S16OwnerFailure as error:
                    raise error
                except S16Blocked as error:
                    raise S16OwnerFailure(
                        owner_id,
                        error.reason_code,
                        retryable=False,
                        responsible_party="platform_storage_owner",
                        recovery_action="resolve_the_blocking_condition_and_repair",
                    ) from error
                except Exception as error:
                    raise S16OwnerFailure(
                        owner_id,
                        S16_OWNER_DELETE_FAILED,
                        retryable=True,
                        responsible_party="runtime_operations_owner",
                        recovery_action="repair_owner_and_resume_the_same_job",
                    ) from error
                owner_results[owner_id] = {
                    **outcome,
                    "operation_id": job_id,
                    "fence": int(job["fence"]),
                }
                del pending[owner_id]
            if pending:
                raise S16OwnerFailure(
                    sorted(pending)[0],
                    S16_OWNER_DELETE_FAILED,
                    retryable=True,
                    responsible_party="runtime_operations_owner",
                    recovery_action="repair_owner_and_resume_the_same_job",
                )
            # Verification phase: every owner must prove absence.
            for owner_id in EXECUTION_ORDER:
                if owner_id == "s17-disabled":
                    continue
                if owner_id not in owner_results:
                    continue
                try:
                    self._owners[owner_id].verify_absent(
                        job.get("pending_owner_fingerprints", {}).get(owner_id, []),
                        scope_fingerprint=scope_fingerprint,
                    )
                except S16OwnerFailure:
                    raise
            return self._publish_success(
                job, owner_results, worker, observed_now
            )
        except S16OwnerFailure as error:
            return self._publish_failure(
                job,
                owner_results,
                error,
                worker,
                observed_now,
            )
        except Exception as error:
            return self._publish_failure(
                job,
                owner_results,
                S16OwnerFailure(
                    "runtime",
                    S16_OWNER_DELETE_FAILED,
                    retryable=True,
                    responsible_party="runtime_operations_owner",
                    recovery_action="repair_owner_and_resume_the_same_job",
                ),
                worker,
                observed_now,
            )

    def _claim_job(
        self, worker: str, now: int
    ) -> dict[str, Any] | None:
        with self._lock:
            self._reload()
            candidate = None
            for job in self._jobs.values():
                status = str(job.get("status") or "")
                if status not in {"pending", "running", "repair_required"}:
                    continue
                if status == "repair_required":
                    continue
                lease_expires = job.get("lease_expires_at")
                if (
                    lease_expires is not None
                    and isinstance(lease_expires, (int, float))
                    and not isinstance(lease_expires, bool)
                    and int(lease_expires) > now
                ):
                    continue
                candidate = job
                break
            if candidate is None:
                return None
            job_id = str(candidate["job_id"])
            claimed = {
                **candidate,
                "status": "running",
                "lease_owner": worker,
                "lease_expires_at": now + LEASE_SECONDS,
                "fence": int(candidate.get("fence") or 0) + 1,
                "attempt": int(candidate.get("attempt") or 0) + 1,
                "updated_at": now,
            }
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._ledger._upsert_job(connection, claimed, now)
                connection.commit()
            self._reload()
            return claimed

    def _publish_success(
        self,
        job: dict[str, Any],
        owner_results: dict[str, Any],
        worker: str,
        now: int,
    ) -> dict[str, Any]:
        job_id = str(job["job_id"])
        request_id = str(job["request_id"])
        scope_fingerprint = str(job["scope_fingerprint"])
        receipt = {
            "receipt_id": _stable_id("s16receipt", job_id),
            "schema_version": S16_RECEIPT_SCHEMA,
            "action": "governed_deletion",
            "policy": "s16-governed-deletion/1",
            "scope_fingerprint": scope_fingerprint,
            "completed_at": now,
            "authority": "s16-governance",
            "result": "deleted",
            "restore_replay_status": "pending",
            "subject": worker,
            "role": "system",
        }
        # Rebuild the real per-owner counts from the inventory fingerprints.
        owner_counts: dict[str, int] = {}
        for owner_id, fingerprints in (job.get("pending_owner_fingerprints") or {}).items():
            if owner_id == "s17-disabled":
                continue
            owner_counts[owner_id] = len(fingerprints or [])
        receipt["owner_counts"] = owner_counts
        final_job = {
            **job,
            "status": "complete",
            "lease_owner": None,
            "lease_expires_at": None,
            "owner_results": owner_results,
            "stable_failure": None,
            "updated_at": now,
            "completed_at": now,
        }
        with self._lock:
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                event = {
                    "event_type": "complete",
                    "event_id": self._event_id("complete", job_id),
                    "request_id": request_id,
                    "job_id": job_id,
                    "scope_fingerprint": scope_fingerprint,
                    "owner_results": {
                        owner_id: result.get("status") or "unknown"
                        for owner_id, result in owner_results.items()
                    },
                    "appended_at": now,
                }
                self._ledger._append_event(connection, event, now)
                self._ledger._upsert_job(connection, final_job, now)
                self._ledger._append_receipt(connection, receipt, now)
                connection.commit()
            self._reload()
        return {
            "status": "complete",
            "job_id": job_id,
            "request_id": request_id,
        }

    def _publish_failure(
        self,
        job: dict[str, Any],
        owner_results: dict[str, Any],
        error: S16OwnerFailure,
        worker: str,
        now: int,
    ) -> dict[str, Any]:
        job_id = str(job["job_id"])
        attempt = int(job.get("attempt") or 0)
        exhausted = attempt >= self._max_owner_attempts or not error.retryable
        status = "repair_required" if exhausted else "pending"
        stable_failure = (
            {
                "owner_id": error.owner_id,
                "reason_code": error.reason_code,
                "responsible_party": error.responsible_party,
                "recovery_action": error.recovery_action,
                "attempt": attempt,
            }
            if exhausted
            else None
        )
        updated = {
            **job,
            "status": status,
            "lease_owner": None,
            "lease_expires_at": None,
            "owner_results": owner_results,
            "stable_failure": stable_failure,
            "updated_at": now,
        }
        with self._lock:
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                event = {
                    "event_type": "owner_result",
                    "event_id": self._event_id("owner_result", job_id),
                    "request_id": str(job.get("request_id") or ""),
                    "job_id": job_id,
                    "scope_fingerprint": str(job.get("scope_fingerprint") or ""),
                    "owner_id": error.owner_id,
                    "reason_code": error.reason_code,
                    "status": "failed",
                    "retryable": error.retryable,
                    "attempt": attempt,
                    "appended_at": now,
                }
                self._ledger._append_event(connection, event, now)
                self._ledger._upsert_job(connection, updated, now)
                connection.commit()
            self._reload()
        return {
            "status": status,
            "job_id": job_id,
            "request_id": str(job.get("request_id") or ""),
            "reason_code": error.reason_code,
            "owner_id": error.owner_id,
            "attempt": attempt,
        }

    # -- repair -------------------------------------------------------------

    def repair(
        self,
        *,
        request_id: str,
        owner_id: str,
        repair_fact: str,
        principal: Any,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        subject = self._require_governance(principal)
        if not isinstance(repair_fact, str) or not repair_fact:
            raise S16Blocked("S16_INVALID_REPAIR_FACT")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise S16Blocked("S16_INVALID_IDEMPOTENCY_KEY")
        binding_key = self._binding_key(subject, idempotency_key)
        command_fingerprint = _digest(
            {
                "action": "repair",
                "request_id": request_id,
                "owner_id": owner_id,
                "repair_fact": repair_fact,
                "subject": subject,
            }
        )
        with self._lock:
            self._reload()
            previous = self._idempotency_binding(binding_key)
            if previous is not None:
                previous_fingerprint, event = previous
                if previous_fingerprint == command_fingerprint:
                    return {**event.get("result", {}), "replayed": True}
                raise S16Conflict(
                    "S16 idempotency conflict: same key different content"
                )
            job = self._job_for_request(request_id)
            if job is None:
                raise S16NotFound(request_id)
            if job.get("status") != "repair_required":
                raise S16Blocked(S16_REPAIR_REQUIRED)
            owner = self._owners.get(owner_id)
            if owner is None or not owner.verify_repair(owner_id, repair_fact):
                raise S16Blocked(S16_REPAIR_NOT_VERIFIED)
            updated = {
                **job,
                "status": "pending",
                "stable_failure": None,
                "updated_at": self._now(),
            }
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                event = {
                    "event_type": "repair",
                    "event_id": self._event_id("repair", request_id),
                    "request_id": request_id,
                    "job_id": str(job["job_id"]),
                    "owner_id": owner_id,
                    "subject": subject,
                    "role": "governance_owner",
                    "appended_at": self._now(),
                }
                self._ledger._append_event(connection, event, self._now())
                self._ledger._upsert_job(connection, updated, self._now())
                self._record_idempotency(
                    connection,
                    key=binding_key,
                    fingerprint=command_fingerprint,
                    result={
                        "status": "accepted",
                        "request_id": request_id,
                        "job_id": str(job["job_id"]),
                    },
                )
                connection.commit()
            self._reload()
        return {
            "status": "accepted",
            "request_id": request_id,
            "job_id": str(updated["job_id"]),
        }

    # -- query / receipt ----------------------------------------------------

    def query(self, *, request_id: str, principal: Any) -> dict[str, Any]:
        subject = self._require_governance(principal)
        del subject
        with self._lock:
            self._reload()
            requests = self._request_events(request_id)
            if not requests:
                raise S16NotFound(request_id)
            preflights = self._preflight_events(request_id)
            preflight = preflights[0] if preflights else None
            job = self._job_for_request(request_id)
            approvals = [
                {
                    "subject_fingerprint": hashlib.sha256(
                        str(event.get("subject") or "").encode("utf-8")
                    ).hexdigest(),
                    "manifest_digest": str(event.get("manifest_digest") or ""),
                    "appended_at": int(event.get("appended_at") or 0),
                }
                for event in self._approvals_for(request_id)
            ]
            holds = [
                {
                    "hold_id": str(event.get("hold_id") or ""),
                    "generation": int(event.get("generation") or 0),
                    "reason_code": str(event.get("reason_code") or ""),
                    "owner": str(event.get("owner") or ""),
                    "effective_time": int(event.get("effective_time") or 0),
                    "expiry": event.get("expiry"),
                    "released": any(
                        item.get("event_type") == "legal_hold_released"
                        and item.get("hold_id") == event.get("hold_id")
                        for item in self._events
                    ),
                }
                for event in self._events_of("legal_hold_imposed")
                if event.get("scope_fingerprint")
                == (preflight or {}).get("scope_fingerprint")
            ]
            cancelled = any(
                event.get("event_type") == "cancel"
                and event.get("request_id") == request_id
                for event in self._events
            )
            return {
                "schema_version": "s16-query/1",
                "request_id": request_id,
                "scope_fingerprint": str(
                    (preflight or {}).get("scope_fingerprint") or ""
                ),
                "manifest_digest": str(
                    (preflight or {}).get("manifest_digest") or ""
                ),
                "owner_registry_digest": str(
                    (preflight or {}).get("owner_registry_digest") or ""
                ),
                "s01_revision": int((preflight or {}).get("s01_revision") or 0),
                "s12_revision": str((preflight or {}).get("s12_revision") or ""),
                "policy_digest": str((preflight or {}).get("policy_digest") or ""),
                "retention_due": (preflight or {}).get("retention_due"),
                "early_deletion": bool(
                    (preflight or {}).get("early_deletion")
                ),
                "cancelled": cancelled,
                "approvals": approvals,
                "legal_holds": holds,
                "job": self._query_job_summary(job),
            }

    def _query_job_summary(self, job: dict[str, Any] | None) -> dict[str, Any] | None:
        if job is None:
            return None
        return {
            "job_id": str(job["job_id"]),
            "status": str(job.get("status") or ""),
            "attempt": int(job.get("attempt") or 0),
            "fence": int(job.get("fence") or 0),
            "lease_owner": job.get("lease_owner"),
            "pending_owner_fingerprints": {
                owner_id: len(fingerprints)
                for owner_id, fingerprints in (
                    job.get("pending_owner_fingerprints") or {}
                ).items()
            },
            "owner_results": {
                owner_id: str(result.get("status") or "unknown")
                for owner_id, result in (job.get("owner_results") or {}).items()
            },
            "stable_failure": job.get("stable_failure"),
            "completed_at": job.get("completed_at"),
        }

    def receipt(self, *, request_id: str, principal: Any) -> dict[str, Any]:
        subject = self._require_governance(principal)
        del subject
        with self._lock:
            self._reload()
            job = self._job_for_request(request_id)
            if job is None or job.get("status") != "complete":
                raise S16NotFound(request_id)
            receipt = self._receipts.get(
                _stable_id("s16receipt", str(job["job_id"]))
            )
            if receipt is None:
                raise S16NotFound(request_id)
            return receipt

    # -- readiness / restore replay ----------------------------------------

    def ready(self) -> bool:
        try:
            self._reload()
            if not self._events:
                return True
            for job in self._jobs.values():
                if job.get("status") != "complete":
                    continue
                receipt = self._receipts.get(
                    _stable_id("s16receipt", str(job["job_id"]))
                )
                if (
                    receipt is None
                    or receipt.get("restore_replay_status") != "verified"
                ):
                    return False
            return True
        except Exception:
            return False

    def _replay_restore(self) -> None:
        """Startup restore replay: re-delete every completed manifest's copies
        on every owner, then verify absence; readiness stays closed until
        every completed manifest is verified (ADR-0008)."""
        with self._lock:
            for job in self._jobs.values():
                if job.get("status") != "complete":
                    continue
                job_id = str(job["job_id"])
                receipt = self._receipts.get(_stable_id("s16receipt", job_id))
                if receipt is None:
                    continue
                if receipt.get("restore_replay_status") == "verified":
                    continue
                fingerprints_by_owner = job.get("pending_owner_fingerprints") or {}
                for owner_id, fingerprints in fingerprints_by_owner.items():
                    if owner_id == "s17-disabled" or not fingerprints:
                        continue
                    self._owners[owner_id].replay(
                        fingerprints,
                        scope_fingerprint=str(job.get("scope_fingerprint") or ""),
                    )
                for owner_id, fingerprints in fingerprints_by_owner.items():
                    if owner_id == "s17-disabled" or not fingerprints:
                        continue
                    self._owners[owner_id].verify_absent(
                        fingerprints,
                        scope_fingerprint=str(job.get("scope_fingerprint") or ""),
                    )
                updated_receipt = {
                    **receipt,
                    "restore_replay_status": "verified",
                    "restore_replayed_at": self._now(),
                }
                with self._ledger._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    event = {
                        "event_type": "restore_replay",
                        "event_id": self._event_id("restore_replay", job_id),
                        "request_id": str(job.get("request_id") or ""),
                        "job_id": job_id,
                        "scope_fingerprint": str(job.get("scope_fingerprint") or ""),
                        "result": "verified",
                        "appended_at": self._now(),
                    }
                    self._ledger._append_event(connection, event, self._now())
                    self._ledger._append_receipt(connection, updated_receipt, self._now())
                    connection.commit()
            self._reload()
