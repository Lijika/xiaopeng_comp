"""S16 governed deletion ledger and orchestration (Ticket #32).

The data-governance plane owns one independent SQLite ledger (events, jobs,
receipts) that never shares a failure domain with the business backup.  It
orchestrates complete-aggregate deletion of one terminated application
across the registered copy owners through a narrow value-free Protocol:

- authority facts: ``resolve_application_reference``, ``scope_exists``,
  ``is_terminated``, ``terminated_at``, ``store_revision``,
  ``owner_healthy``, ``retained_scan``, ``referenced_object_digests``,
  ``all_scope_fingerprints``;
- copy lifecycle: ``inventory``, ``delete``, ``verify_absent``, ``replay``,
  ``verify_repair``.

No orchestrator code touches another owner's private service, tables, paths
or singletons (ADR-0008 module ownership).  Owners resolve their own copies
by scanning their own stores and matching the value-free scope fingerprint,
so the ledger never stores application ids, object refs, paths, raw values
or credentials (ADR-0003 minimization).

Copy classes are fixed at nine: ``source_object``, ``derived_object``,
``evidence``, ``run_or_finding``, ``projection_or_cache``,
``export_or_temp``, ``evaluation_copy``, ``replica``, ``backup_manifest``.
``export_or_temp`` stays closed while S17 is disabled: the disabled owner
returns a documented zero-inventory proof so the class still appears in
every manifest.

Semantics pinned by ADR-0003/0004/0007/0008 and the ROUND32 plan:

- commit is the only irreversible boundary; every command is bound to
  subject, role, scope, source, action, request id and a bounded expiry;
- Legal Hold lives in this ledger as a typed command (closed reason/owner
  vocabulary, request id + idempotency binding) and impose/release/commit
  race inside one SQLite transaction per command;
- the deletion worker uses a finite lease, a monotonic fence, bounded
  attempts and owner-level CAS bindings; publish uses a lease/fence/attempt
  CAS so a stale worker can never overwrite newer state; after retry
  exhaustion a job enters ``repair_required`` with a stable failure,
  responsible party and recovery action, and ``repair`` resumes the same
  job;
- completion writes a value-free append-only receipt; restore replay
  appends immutable replay facts and readiness derives from them;
- startup replay re-deletes every completed manifest's copies on every
  owner and only then opens readiness; while readiness is false every
  restricted read stays closed (the shared web gate);
- an injected security-audit owner receives value-free facts for protected
  commands; when security audit availability is off, protected commands
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
S16_HOLD_GENERATION_CHANGED = "S16_HOLD_GENERATION_CHANGED"
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
S16_STALE_WORKER = "S16_STALE_WORKER"
S16_OWNER_STALE_FENCE = "S16_OWNER_STALE_FENCE"
S16_OWNER_BINDING_CONFLICT = "S16_OWNER_BINDING_CONFLICT"
S16_AUDIT_SEAM_UNAVAILABLE = "S16_AUDIT_SEAM_UNAVAILABLE"
S16_RESTORE_READINESS_UNAVAILABLE = "S16_RESTORE_READINESS_UNAVAILABLE"

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

# Closed Legal Hold vocabularies (R1: no free text in retained ledger).
LEGAL_HOLD_REASON_CODES = frozenset(
    {"litigation", "regulatory", "internal_investigation"}
)
LEGAL_HOLD_OWNERS = frozenset(
    {"s01", "s02", "s12", "backup", "s17-disabled", "all"}
)

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

    # -- value-free authority facts -------------------------------------
    # The orchestrator only ever calls protocol methods; owner adapters
    # translate to their own authorities internally (ADR-0008 module
    # ownership).  No private service, table, path or singleton is part of
    # this interface.

    def resolve_application_reference(
        self, reference: str, scope: str
    ) -> str | None:
        """Resolve one application reference to its value-free scope
        fingerprint, or ``None`` (existence-hiding)."""

    def scope_exists(self, scope_fingerprint: str) -> bool:
        """True when a live application resolves to this scope."""

    def is_terminated(self, scope_fingerprint: str) -> bool:
        """True when the scoped application reached L14 Terminated."""

    def terminated_at(self, scope_fingerprint: str) -> int | None:
        """The L14 Terminated time, when known."""

    def store_revision(self) -> int | str:
        """The owner's current revision (int for S01, digest for S12)."""

    def owner_healthy(self) -> bool:
        """True when the owner authority can be proven."""

    def retained_scan(self, scope_fingerprint: str) -> dict[str, Any]:
        """The value-free retained-history scan fact for one scope."""

    def referenced_object_digests(
        self, scope_fingerprint: str
    ) -> frozenset[str]:
        """Content digests the scoped application references on the S02
        object owner (for shared-copy detection)."""

    def all_scope_fingerprints(self) -> frozenset[str]:
        """Every live application scope fingerprint (for cross-application
        shared-copy detection)."""

    # -- copy lifecycle --------------------------------------------------

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
        """Delete the named copies under an owner-level CAS binding
        (operation id + fence); same binding replays the original result
        and a stale fence is a stable stale outcome."""

    def verify_absent(
        self,
        copy_fingerprints: Iterable[str],
        *,
        scope_fingerprint: str,
        operation_id: str | None = None,
        fence: int | None = None,
    ) -> dict[str, Any]:
        """Prove the named copies are absent."""

    def replay(
        self,
        copy_fingerprints: Iterable[str],
        *,
        scope_fingerprint: str,
        operation_id: str,
        fence: int,
    ) -> dict[str, Any]:
        """Restore-time replay: idempotently re-delete the named copies
        under an owner-level operation/fence binding (R3 P0-1)."""

    def verify_repair(self, owner_id: str, repair_fact: str) -> bool:
        """True when the owner accepts the operator repair fact."""


# ---------------------------------------------------------------------------
# The independent ledger
# ---------------------------------------------------------------------------


class S16Ledger:
    """Independent SQLite ledger for S16: append-only events, mutable jobs
    with CAS publish, append-only receipts and append-only replay facts.
    One short transaction writes command event, audit fact, idempotency
    binding and job together (ADR-0003)."""

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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS s16_replays (
                    replay_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    integrity_sha256 TEXT NOT NULL,
                    appended_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS s16_bindings (
                    binding_key TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    result TEXT NOT NULL,
                    appended_at INTEGER NOT NULL
                )
                """
            )
            # R2 (P1-9): the worker lease columns live beside the payload so
            # claim/publish run as database compare-and-set, never as a
            # process lock.  Older databases get the columns added.
            for column_sql in (
                "ALTER TABLE s16_jobs ADD COLUMN status TEXT",
                "ALTER TABLE s16_jobs ADD COLUMN lease_owner TEXT",
                "ALTER TABLE s16_jobs ADD COLUMN lease_expires_at INTEGER",
                "ALTER TABLE s16_jobs ADD COLUMN fence INTEGER",
                "ALTER TABLE s16_jobs ADD COLUMN attempt INTEGER",
            ):
                try:
                    connection.execute(column_sql)
                except sqlite3.OperationalError:
                    # Duplicate column on an already-migrated database.
                    pass
            # R3 (P1-12): backfill the new columns from the authoritative
            # payload so pre-R2 pending/running jobs stay claimable and the
            # column view never disagrees with the payload.
            job_rows = connection.execute(
                "SELECT job_id, payload FROM s16_jobs"
            ).fetchall()
            for job_id, payload in job_rows:
                try:
                    job = json.loads(payload)
                except (TypeError, ValueError):
                    continue
                if not isinstance(job, dict):
                    continue
                connection.execute(
                    "UPDATE s16_jobs SET status = ?, lease_owner = ?, "
                    "lease_expires_at = ?, fence = ?, attempt = ? "
                    "WHERE job_id = ? AND (status IS NULL OR status = '')",
                    (
                        str(job.get("status") or ""),
                        job.get("lease_owner"),
                        job.get("lease_expires_at"),
                        int(job.get("fence") or 0),
                        int(job.get("attempt") or 0),
                        job_id,
                    ),
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS s16_meta_facts (
                    fact_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    appended_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO s16_meta_facts("
                "fact_key, payload, appended_at) VALUES (?, ?, ?)",
                (
                    "s16_jobs_schema_migration",
                    _canonical(
                        {
                            "schema_version": "s16-jobs-schema/2",
                            "backfilled": True,
                        }
                    ),
                    int(time.time()),
                ),
            )
            try:
                connection.execute(
                    "ALTER TABLE backup_deletion_intents "
                    "ADD COLUMN identities_json TEXT NOT NULL DEFAULT '[]'"
                )
            except sqlite3.OperationalError:
                # Column already present on a migrated database.
                pass
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

    def _load_jobs(self) -> dict[str, dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id, payload FROM s16_jobs"
            ).fetchall()
        return {job_id: json.loads(payload) for job_id, payload in rows}

    def _upsert_job(
        self, connection: sqlite3.Connection, job: dict[str, Any], now: int
    ) -> None:
        connection.execute(
            "INSERT INTO s16_jobs("
            "job_id, payload, updated_at, status, lease_owner, "
            "lease_expires_at, fence, attempt) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET payload = excluded.payload, "
            "updated_at = excluded.updated_at, status = excluded.status, "
            "lease_owner = excluded.lease_owner, "
            "lease_expires_at = excluded.lease_expires_at, "
            "fence = excluded.fence, attempt = excluded.attempt",
            (
                str(job["job_id"]),
                _canonical(job),
                now,
                str(job.get("status") or ""),
                job.get("lease_owner"),
                job.get("lease_expires_at"),
                int(job.get("fence") or 0),
                int(job.get("attempt") or 0),
            ),
        )

    def _cas_publish_job(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        *,
        expected_lease_owner: str | None,
        expected_fence: int,
        expected_attempt: int,
        new_job: dict[str, Any],
        now: int,
    ) -> bool:
        """Compare-and-set publish: only the worker holding the current
        lease and fence may publish; a stale worker's write is rejected
        (ADR-0008, no last-write-wins)."""
        row = connection.execute(
            "SELECT payload FROM s16_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return False
        current = json.loads(row[0])
        if (
            current.get("lease_owner") != expected_lease_owner
            or int(current.get("fence") or 0) != expected_fence
            or int(current.get("attempt") or 0) != expected_attempt
        ):
            return False
        connection.execute(
            "UPDATE s16_jobs SET payload = ?, updated_at = ?, status = ?, "
            "lease_owner = ?, lease_expires_at = ?, fence = ?, attempt = ? "
            "WHERE job_id = ?",
            (
                _canonical(new_job),
                now,
                str(new_job.get("status") or ""),
                new_job.get("lease_owner"),
                new_job.get("lease_expires_at"),
                int(new_job.get("fence") or 0),
                int(new_job.get("attempt") or 0),
                job_id,
            ),
        )
        return True

    def _claim_job_cas(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        worker: str,
        now: int,
        lease_seconds: int,
        claimed_payload: dict[str, Any],
        claimed_fence: int,
        claimed_attempt: int,
        expected_status: str,
        expected_lease_expires_at: int | None,
        expected_fence: int,
        expected_attempt: int,
    ) -> bool:
        """Database-level claim CAS (R2 P1-9, R3 P1-10): the conditional
        update compares ALL five original values — job id, status, lease
        expiry, fence and attempt — so a stale snapshot can never advance
        the row; only one worker wins the lease."""
        updated = connection.execute(
            "UPDATE s16_jobs SET "
            "payload = ?, updated_at = ?, status = ?, lease_owner = ?, "
            "lease_expires_at = ?, fence = ?, attempt = ? "
            "WHERE job_id = ? "
            "AND status = ? "
            "AND (lease_expires_at IS ? OR lease_expires_at = ?) "
            "AND fence = ? AND attempt = ?",
            (
                _canonical(claimed_payload),
                now,
                "running",
                worker,
                now + lease_seconds,
                claimed_fence,
                claimed_attempt,
                job_id,
                expected_status,
                (
                    None
                    if expected_lease_expires_at is None
                    else int(expected_lease_expires_at)
                ),
                (
                    None
                    if expected_lease_expires_at is None
                    else int(expected_lease_expires_at)
                ),
                expected_fence,
                expected_attempt,
            ),
        )
        return updated.rowcount == 1

    def _binding_read(
        self, connection: sqlite3.Connection, binding_key: str
    ) -> tuple[str, dict[str, Any]] | None:
        row = connection.execute(
            "SELECT fingerprint, result FROM s16_bindings WHERE binding_key = ?",
            (binding_key,),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), json.loads(row[1])

    def _binding_insert(
        self,
        connection: sqlite3.Connection,
        *,
        binding_key: str,
        fingerprint: str,
        result: dict[str, Any],
        now: int,
    ) -> str:
        """Insert-if-absent; returns 'inserted' or 'existing'."""
        cursor = connection.execute(
            "INSERT OR IGNORE INTO s16_bindings("
            "binding_key, fingerprint, result, appended_at) VALUES (?, ?, ?, ?)",
            (binding_key, fingerprint, _canonical(result), now),
        )
        return "inserted" if cursor.rowcount == 1 else "existing"

    def _append_receipt(
        self, connection: sqlite3.Connection, receipt: dict[str, Any], now: int
    ) -> None:
        """Append-only: an existing receipt id is never rewritten."""
        receipt_id = str(receipt.get("receipt_id") or "")
        if not receipt_id:
            raise ValueError("S16 receipt requires a stable receipt id")
        payload = _canonical(receipt)
        digest = self._integrity_digest("s16_receipts", receipt_id, payload)
        connection.execute(
            "INSERT INTO s16_receipts(receipt_id, payload, integrity_sha256, appended_at) "
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

    def _append_replay(
        self, connection: sqlite3.Connection, replay: dict[str, Any], now: int
    ) -> None:
        """Append-only restore-replay fact: readiness derives from these
        immutable rows, never from rewriting the completion receipt."""
        replay_id = str(replay.get("replay_id") or "")
        if not replay_id:
            raise ValueError("S16 replay fact requires a stable replay id")
        payload = _canonical(replay)
        digest = self._integrity_digest("s16_replays", replay_id, payload)
        connection.execute(
            "INSERT INTO s16_replays(replay_id, payload, integrity_sha256, appended_at) "
            "VALUES (?, ?, ?, ?)",
            (replay_id, payload, digest, now),
        )

    def _load_replays(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT replay_id, payload, integrity_sha256 FROM s16_replays"
            ).fetchall()
        replays: list[dict[str, Any]] = []
        for replay_id, payload, declared_digest in rows:
            if (
                self._integrity_digest("s16_replays", replay_id, payload)
                != declared_digest
            ):
                raise S16Unavailable("S16 ledger replay integrity failed")
            replays.append(json.loads(payload))
        replays.sort(key=lambda replay: int(replay.get("appended_at") or 0))
        return replays


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

    # -- value-free authority facts (narrow protocol) ---------------------

    def resolve_application_reference(
        self, reference: str, scope: str
    ) -> str | None:
        application_id = self._service.s16_resolve_application(
            upstream_application_reference=reference, scope=scope
        )
        if application_id is None:
            return None
        return scope_fingerprint_for(application_id)

    def scope_exists(self, scope_fingerprint: str) -> bool:
        return (
            self._service.s16_resolve_by_scope_fingerprint(scope_fingerprint)
            is not None
        )

    def is_terminated(self, scope_fingerprint: str) -> bool:
        application_id = self._service.s16_resolve_by_scope_fingerprint(
            scope_fingerprint
        )
        if application_id is None:
            return False
        return self._service.s16_is_terminated(application_id)

    def terminated_at(self, scope_fingerprint: str) -> int | None:
        application_id = self._service.s16_resolve_by_scope_fingerprint(
            scope_fingerprint
        )
        if application_id is None:
            return None
        return self._service.s16_application_terminated_at(application_id)

    def store_revision(self) -> int:
        return int(self._service.s16_store_revision())

    def owner_healthy(self) -> bool:
        return bool(self._service.s16_owner_healthy())

    def retained_scan(self, scope_fingerprint: str) -> dict[str, Any]:
        application_id = self._resolve_application_id(scope_fingerprint)
        return dict(self._service.s16_inventory(application_id)["retained_scan"])

    def referenced_object_digests(
        self, scope_fingerprint: str
    ) -> frozenset[str]:
        application_id = self._resolve_application_id(scope_fingerprint)
        return frozenset(
            self._service.s16_referenced_object_digests(application_id)
        )

    def all_scope_fingerprints(self) -> frozenset[str]:
        return frozenset(
            scope_fingerprint_for(application_id)
            for application_id in self._service.s16_application_ids()
        )

    def inventory(self, scope_fingerprint: str) -> list[CopyInventoryEntry]:
        if not self.scope_exists(scope_fingerprint):
            raise S16NotFound(scope_fingerprint)
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
            if self._service.s16_tombstone_verified(
                scope_fingerprint,
                operation_id=operation_id,
                fence=fence,
            ):
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
        self,
        copy_fingerprints: Iterable[str],
        *,
        scope_fingerprint: str,
        operation_id: str | None = None,
        fence: int | None = None,
    ) -> dict[str, Any]:
        if not set(copy_fingerprints):
            return {"owner_id": self.owner_id, "status": "verified"}
        if operation_id is None:
            result = self._service.s16_verify_absent(scope_fingerprint)
        else:
            result = self._service.s16_verify_absent(
                scope_fingerprint,
                operation_id=operation_id,
                fence=fence,
            )
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
        self,
        copy_fingerprints: Iterable[str],
        *,
        scope_fingerprint: str,
        operation_id: str = "s16-replay",
        fence: int = 0,
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
            operation_id=operation_id,
            fence=fence,
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

    def __init__(self, boundary: Any, s01_owner: Any) -> None:
        self._boundary = boundary
        self._s01_owner = s01_owner

    def store_revision(self) -> int:
        return int(self._boundary.s02_store_revision())

    def owner_healthy(self) -> bool:
        return bool(self._boundary.s02_owner_healthy())

    def inventory(self, scope_fingerprint: str) -> list[CopyInventoryEntry]:
        if not self._s01_owner.scope_exists(scope_fingerprint):
            raise S16NotFound(scope_fingerprint)
        target_digests = self._s01_owner.referenced_object_digests(
            scope_fingerprint
        )
        other_digests: set[str] = set()
        for other_scope in self._s01_owner.all_scope_fingerprints():
            if other_scope == scope_fingerprint:
                continue
            other_digests.update(
                self._s01_owner.referenced_object_digests(other_scope)
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
        fingerprints = sorted(set(copy_fingerprints))
        result = self._boundary.s02_delete(
            fingerprints,
            operation_id=operation_id,
            fence=fence,
            scope_fingerprint=scope_fingerprint,
        )
        if result.get("status") == "conflict":
            raise S16OwnerFailure(
                self.owner_id,
                S16_OWNER_BINDING_CONFLICT,
                retryable=False,
                responsible_party="runtime_operations_owner",
                recovery_action="reconcile_scope_binding_and_resume",
            )
        if result.get("status") == "stale":
            raise S16OwnerFailure(
                self.owner_id,
                S16_OWNER_STALE_FENCE,
                retryable=False,
                responsible_party="runtime_operations_owner",
                recovery_action="reconcile_worker_fence_and_resume",
            )
        return {
            "owner_id": self.owner_id,
            "status": result.get("status", "complete"),
            "deleted_counts": result.get("deleted_counts", {}),
        }

    def verify_absent(
        self,
        copy_fingerprints: Iterable[str],
        *,
        scope_fingerprint: str,
        operation_id: str | None = None,
        fence: int | None = None,
    ) -> dict[str, Any]:
        fingerprints = sorted(set(copy_fingerprints))
        result = self._boundary.s02_verify_absent(
            fingerprints, scope_fingerprint=scope_fingerprint
        )
        if result.get("scope_mismatch"):
            raise S16OwnerFailure(
                self.owner_id,
                S16_OWNER_BINDING_CONFLICT,
                retryable=False,
                responsible_party="runtime_operations_owner",
                recovery_action="reconcile_scope_binding_and_resume",
            )
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
        self,
        copy_fingerprints: Iterable[str],
        *,
        scope_fingerprint: str,
        operation_id: str,
        fence: int,
    ) -> dict[str, Any]:
        fingerprints = sorted(set(copy_fingerprints))
        result = self._boundary.s02_replay(
            fingerprints,
            operation_id=operation_id,
            fence=fence,
            scope_fingerprint=scope_fingerprint,
        )
        if result.get("status") == "conflict":
            raise S16OwnerFailure(
                self.owner_id,
                S16_OWNER_BINDING_CONFLICT,
                retryable=False,
                responsible_party="runtime_operations_owner",
                recovery_action="reconcile_scope_binding_and_resume",
            )
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

    def store_revision(self) -> str:
        return str(self._service.s16_store_revision())

    def owner_healthy(self) -> bool:
        return bool(self._service.s16_owner_healthy())

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
        fingerprints = sorted(set(copy_fingerprints))
        result = self._service.s16_delete_scope(
            fingerprints,
            operation_id=operation_id,
            fence=fence,
            scope_fingerprint=scope_fingerprint,
        )
        if result.get("status") == "conflict":
            raise S16OwnerFailure(
                self.owner_id,
                S16_OWNER_BINDING_CONFLICT,
                retryable=False,
                responsible_party="runtime_operations_owner",
                recovery_action="reconcile_scope_binding_and_resume",
            )
        if result.get("status") == "stale":
            raise S16OwnerFailure(
                self.owner_id,
                S16_OWNER_STALE_FENCE,
                retryable=False,
                responsible_party="runtime_operations_owner",
                recovery_action="reconcile_worker_fence_and_resume",
            )
        return {
            "owner_id": self.owner_id,
            "status": "complete",
            "deleted_counts": result.get("deleted_counts", {}),
        }

    def verify_absent(
        self,
        copy_fingerprints: Iterable[str],
        *,
        scope_fingerprint: str,
        operation_id: str | None = None,
        fence: int | None = None,
    ) -> dict[str, Any]:
        fingerprints = sorted(set(copy_fingerprints))
        result = self._service.s16_verify_absent(
            fingerprints, scope_fingerprint=scope_fingerprint
        )
        if result.get("scope_mismatch"):
            raise S16OwnerFailure(
                self.owner_id,
                S16_OWNER_BINDING_CONFLICT,
                retryable=False,
                responsible_party="runtime_operations_owner",
                recovery_action="reconcile_scope_binding_and_resume",
            )
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
        self,
        copy_fingerprints: Iterable[str],
        *,
        scope_fingerprint: str,
        operation_id: str,
        fence: int,
    ) -> dict[str, Any]:
        fingerprints = sorted(set(copy_fingerprints))
        result = self._service.s16_replay_scope(
            fingerprints,
            operation_id=operation_id,
            fence=fence,
            scope_fingerprint=scope_fingerprint,
        )
        if result.get("status") == "conflict":
            raise S16OwnerFailure(
                self.owner_id,
                S16_OWNER_BINDING_CONFLICT,
                retryable=False,
                responsible_party="runtime_operations_owner",
                recovery_action="reconcile_scope_binding_and_resume",
            )
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

    ``capture`` is called by the backup system at backup time.  The manifest
    (the only record the ledger/audit ever sees) stores value-free
    connector identities and content digests only — never handles, paths or
    locators (R2 P1-1).  The identity-to-object mapping lives in an
    owner-internal registry beside the backup root.  ``delete`` binds the
    operation id + fence + scope + fingerprints digest in one SQLite
    transaction, verifies every file by digest, unlinks it, re-verifies
    absence and only then removes the manifest and the registry rows; any
    failure keeps the manifest and raises an owner failure.  ``verify_absent``
    is scope-aware.  ``replay`` re-deletes after an old backup restore so
    readiness stays gated until absence is verified."""

    owner_id = "backup"

    def __init__(self, root: str | Path, *, clock: Callable[[], int]) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._registry_path = self._root / "backup_owner.sqlite3"
        self._ensure_registry_schema()

    def _registry_connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._registry_path, timeout=10.0)
        connection.isolation_level = None
        return connection

    def _ensure_registry_schema(self) -> None:
        with self._registry_connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS backup_registry (
                    connector_identity TEXT PRIMARY KEY,
                    handle TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    captured_at INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS backup_deletion_bindings (
                    operation_id TEXT NOT NULL,
                    fence INTEGER NOT NULL,
                    scope_fingerprint TEXT NOT NULL,
                    fingerprints_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    completed_at INTEGER NOT NULL,
                    PRIMARY KEY (operation_id, fence)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS backup_deletion_intents (
                    operation_id TEXT NOT NULL,
                    fence INTEGER NOT NULL,
                    scope_fingerprint TEXT NOT NULL,
                    fingerprints_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    staged_at INTEGER NOT NULL,
                    identities_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY (operation_id, fence)
                )
                """
            )
            try:
                connection.execute(
                    "ALTER TABLE backup_deletion_intents "
                    "ADD COLUMN identities_json TEXT NOT NULL DEFAULT '[]'"
                )
            except sqlite3.OperationalError:
                # Column already present on a migrated database.
                pass
            connection.commit()

    def _manifest_path(self, manifest_id: str) -> Path:
        return self._root / f"{manifest_id}.json"

    @classmethod
    def _connector_identity(cls, handle: str, content_sha256: str) -> str:
        """Value-free connector identity: digest of the capture handle and
        content digest — no path, name or locator is recoverable."""
        return _digest({"handle": handle, "content_sha256": content_sha256})

    def capture(
        self,
        *,
        scope_fingerprint: str,
        copy_files: Iterable[tuple[str, str]],
    ) -> dict[str, Any]:
        """Record one scope-scoped backup: safe file handle + content digest
        pairs.  Absolute paths, separators and root-escape targets are
        rejected; the manifest stores connector identities and digests only."""
        files: list[dict[str, str]] = []
        registry_rows: list[tuple[str, str, str, int]] = []
        for name, content_sha256 in sorted(copy_files):
            handle = self._validate_capture_handle(name)
            identity = self._connector_identity(handle, content_sha256)
            files.append(
                {"connector_identity": identity, "content_sha256": content_sha256}
            )
            registry_rows.append(
                (identity, handle, content_sha256, int(self._clock()))
            )
        if not files:
            raise ValueError("S16 backup capture requires at least one file")
        manifest_id = _stable_id(
            "backup",
            f"{scope_fingerprint}:{int(self._clock())}:{secrets.token_hex(6)}",
        )
        manifest_path = self._manifest_path(manifest_id)
        if manifest_path.exists():
            raise S16Unavailable(
                "S16 backup manifest identity collides with an existing capture"
            )
        manifest = {
            "schema_version": "s16-backup-manifest/1",
            "manifest_id": manifest_id,
            "scope_fingerprint": scope_fingerprint,
            "captured_at": int(self._clock()),
            "files": files,
            "entries_digest": _digest(files),
        }
        manifest_path.write_text(_canonical(manifest), encoding="utf-8")
        try:
            with self._registry_connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.executemany(
                    "INSERT OR REPLACE INTO backup_registry("
                    "connector_identity, handle, content_sha256, captured_at) "
                    "VALUES (?, ?, ?, ?)",
                    registry_rows,
                )
                connection.commit()
        except Exception:
            if manifest_path.exists():
                manifest_path.unlink()
            raise
        return manifest

    @classmethod
    def _validate_capture_handle(cls, name: str) -> str:
        if not isinstance(name, str) or not name:
            raise ValueError("S16 backup capture handle is invalid")
        if name != name.strip() or "/" in name or "\\" in name:
            raise ValueError("S16 backup capture handle must be a bare file name")
        if name in {".", ".."} or name.startswith("."):
            raise ValueError("S16 backup capture handle is not a plain file name")
        candidate = Path(name)
        if candidate.is_absolute() or any(
            part in {"..", "."} for part in candidate.parts
        ):
            raise ValueError("S16 backup capture handle escapes the backup root")
        return name

    def _capture_target(self, handle: str) -> Path:
        target = (self._root / handle).resolve()
        if self._root != target.parent:
            raise S16Unavailable("S16 backup capture target escapes the backup root")
        return target

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

    def _backup_reconciliation(self) -> dict[str, Any]:
        """Value-free bidirectional backup integrity check (R4 P1-4):
        every manifest entry must be backed by a registry row whose handle
        stays inside the backup root with a matching captured file and
        content digest, and every registry row must be referenced by a
        manifest.  Orphans, missing rows, missing files, schema or digest
        mismatches raise a stable ``S16Unavailable`` so inventory,
        ``owner_healthy``, commit preflight and readiness all fail closed."""
        manifests = self._load_manifests()
        with self._registry_connect() as connection:
            rows = connection.execute(
                "SELECT connector_identity, handle, content_sha256 "
                "FROM backup_registry"
            ).fetchall()
        registry = {str(r[0]): (str(r[1]), str(r[2])) for r in rows}
        referenced: set[str] = set()
        for manifest in manifests:
            files = manifest.get("files")
            if not isinstance(files, list):
                raise S16Unavailable(
                    "S16 backup manifest schema is invalid"
                )
            if _digest(files) != str(manifest.get("entries_digest") or ""):
                raise S16Unavailable(
                    "S16 backup manifest entries digest mismatch"
                )
            for file_entry in files:
                if not isinstance(file_entry, dict):
                    raise S16Unavailable(
                        "S16 backup manifest schema is invalid"
                    )
                identity = str(file_entry.get("connector_identity") or "")
                if not identity:
                    raise S16Unavailable(
                        "S16 backup manifest schema is invalid"
                    )
                referenced.add(identity)
                registry_row = registry.get(identity)
                if registry_row is None:
                    raise S16Unavailable(
                        "S16 backup manifest references a missing registry row"
                    )
                handle, expected_digest = registry_row
                if expected_digest != str(
                    file_entry.get("content_sha256") or ""
                ):
                    raise S16Unavailable(
                        "S16 backup registry digest mismatch"
                    )
                target = self._capture_target(handle)
                if not target.is_file():
                    raise S16Unavailable(
                        "S16 backup captured file is missing"
                    )
                if (
                    hashlib.sha256(target.read_bytes()).hexdigest()
                    != expected_digest
                ):
                    raise S16Unavailable(
                        "S16 backup captured file digest mismatch"
                    )
        orphans = sorted(set(registry) - referenced)
        if orphans:
            raise S16Unavailable(
                "S16 backup registry contains orphan rows"
            )
        return {"manifests": manifests, "registry_rows": registry}

    def inventory(self, scope_fingerprint: str) -> list[CopyInventoryEntry]:
        entries: list[CopyInventoryEntry] = []
        for manifest in self._backup_reconciliation()["manifests"]:
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
        self, copy_fingerprints: set[str], scope_fingerprint: str = ""
    ) -> list[dict[str, Any]]:
        if not copy_fingerprints:
            return []
        return [
            manifest
            for manifest in self._load_manifests()
            if (
                not scope_fingerprint
                or manifest.get("scope_fingerprint") == scope_fingerprint
            )
            and (
                copy_identity_fingerprint(
                    self.owner_id, COPY_CLASS_REPLICA, _digest(manifest["files"])
                )
                in copy_fingerprints
                or copy_identity_fingerprint(
                    self.owner_id, COPY_CLASS_BACKUP_MANIFEST, _digest(manifest)
                )
                in copy_fingerprints
            )
        ]

    def _bindings_digest(self, fingerprints: Iterable[str]) -> str:
        return hashlib.sha256(
            "\0".join(sorted(set(fingerprints))).encode("utf-8")
        ).hexdigest()

    def delete(
        self,
        copy_fingerprints: Iterable[str],
        *,
        scope_fingerprint: str,
        operation_id: str,
        fence: int,
    ) -> dict[str, Any]:
        """Crash-recoverable deletion (R3 P1-14): the deletion intent is
        durably staged BEFORE any file is unlinked; a crash between unlink
        and the registry commit leaves a staged intent that the next attempt
        (or replay) resumes instead of failing verification forever."""
        fingerprints = set(copy_fingerprints)
        fingerprints_digest = self._bindings_digest(fingerprints)
        with self._registry_connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            binding = connection.execute(
                "SELECT status, scope_fingerprint, fingerprints_digest "
                "FROM backup_deletion_bindings "
                "WHERE operation_id = ? AND fence = ?",
                (operation_id, int(fence)),
            ).fetchone()
            if binding is not None:
                connection.execute("COMMIT")
                if (
                    str(binding[1]) != scope_fingerprint
                    or str(binding[2]) != fingerprints_digest
                ):
                    return {
                        "status": "conflict",
                        "deleted_counts": {},
                        "reason_code": "S16_OWNER_BINDING_CONFLICT",
                    }
                return {
                    "status": "complete",
                    "deleted_counts": {},
                    "already_absent": True,
                    "replayed": True,
                }
            highest = connection.execute(
                "SELECT MAX(fence) FROM backup_deletion_bindings "
                "WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if highest[0] is not None and int(highest[0]) > int(fence):
                connection.execute("COMMIT")
                return {"status": "stale", "deleted_counts": {}}
            staged = connection.execute(
                "SELECT scope_fingerprint, fingerprints_digest, "
                "identities_json FROM backup_deletion_intents "
                "WHERE operation_id = ? AND fence = ?",
                (operation_id, int(fence)),
            ).fetchone()
            if staged is not None:
                if (
                    str(staged[0]) != scope_fingerprint
                    or str(staged[1]) != fingerprints_digest
                ):
                    connection.execute("COMMIT")
                    return {
                        "status": "conflict",
                        "deleted_counts": {},
                        "reason_code": "S16_OWNER_BINDING_CONFLICT",
                    }
                # Resume a crashed deletion: some files were already
                # unlinked; complete idempotently from the staged intent.
                identities = set(json.loads(staged[2] or "[]"))
                connection.commit()
                with self._registry_connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    return self._backup_delete_commit(
                        connection, scope_fingerprint, fingerprints_digest,
                        operation_id, fence, identities, resume=True,
                    )
            manifests = self._manifests_for_fingerprints(
                fingerprints, scope_fingerprint=scope_fingerprint
            )
            if not manifests:
                # R4 (P1-3): even the already-absent outcome writes the
                # operation binding so the worker/replay verify can prove
                # the binding instead of a scope-only scan.
                connection.execute(
                    "INSERT INTO backup_deletion_bindings("
                    "operation_id, fence, scope_fingerprint, "
                    "fingerprints_digest, status, completed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        operation_id,
                        int(fence),
                        scope_fingerprint,
                        fingerprints_digest,
                        "complete",
                        int(self._clock()),
                    ),
                )
                connection.execute("COMMIT")
                return {
                    "status": "complete",
                    "deleted_counts": {},
                    "already_absent": True,
                }
            # Stage the durable intent BEFORE any unlink (T16 P1-14).
            identities = {
                str(file_entry["connector_identity"])
                for manifest in manifests
                for file_entry in manifest["files"]
            }
            connection.execute(
                "INSERT INTO backup_deletion_intents("
                "operation_id, fence, scope_fingerprint, fingerprints_digest, "
                "status, staged_at, identities_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    operation_id,
                    int(fence),
                    scope_fingerprint,
                    fingerprints_digest,
                    "staged",
                    int(self._clock()),
                    json.dumps(sorted(identities), separators=(",", ":")),
                ),
            )
            try:
                connection.execute(
                    "ALTER TABLE backup_deletion_intents "
                    "ADD COLUMN identities_json TEXT NOT NULL DEFAULT '[]'"
                )
            except sqlite3.OperationalError:
                # Column already present on a migrated database.
                pass
            connection.commit()
        with self._registry_connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._backup_delete_commit(
                connection, scope_fingerprint, fingerprints_digest,
                operation_id, fence, identities, resume=False,
            )

    def _backup_delete_commit(
        self,
        connection: sqlite3.Connection,
        scope_fingerprint: str,
        fingerprints_digest: str,
        operation_id: str,
        fence: int,
        identities: set[str],
        *,
        resume: bool,
    ) -> dict[str, Any]:
        """Complete a staged deletion inside the open registry transaction:
        every captured file is digest-verified and removed (a resume
        tolerates files the crashed pass already removed), manifests are
        removed, then registry rows, the intent and the operation binding
        commit together.  Any integrity mismatch rolls back and raises an
        owner failure (R3 P1-14 crash-recoverable deletion)."""
        deleted = 0
        manifest_ids: set[str] = set()
        for identity in sorted(identities):
            row = connection.execute(
                "SELECT handle, content_sha256 FROM backup_registry "
                "WHERE connector_identity = ?",
                (identity,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise S16OwnerFailure(
                    self.owner_id,
                    S16_VERIFY_FAILED,
                    retryable=True,
                    responsible_party="backup_operations_owner",
                    recovery_action="restore_the_captured_copy_registry",
                )
            handle = str(row[0])
            expected_digest = str(row[1])
            target = self._capture_target(handle)
            if target.is_file():
                if (
                    hashlib.sha256(target.read_bytes()).hexdigest()
                    != expected_digest
                ):
                    connection.execute("ROLLBACK")
                    raise S16OwnerFailure(
                        self.owner_id,
                        S16_VERIFY_FAILED,
                        retryable=True,
                        responsible_party="backup_operations_owner",
                        recovery_action="repair_the_captured_copy_digest",
                    )
                target.unlink()
                if target.exists():
                    connection.execute("ROLLBACK")
                    raise S16OwnerFailure(
                        self.owner_id,
                        S16_VERIFY_FAILED,
                        retryable=True,
                        responsible_party="backup_operations_owner",
                        recovery_action="verify_the_captured_copy_removal",
                    )
                deleted += 1
            elif not resume:
                connection.execute("ROLLBACK")
                raise S16OwnerFailure(
                    self.owner_id,
                    S16_VERIFY_FAILED,
                    retryable=True,
                    responsible_party="backup_operations_owner",
                    recovery_action="restore_and_verify_the_captured_copy",
                )
        for manifest in self._load_manifests():
            if str(manifest.get("scope_fingerprint") or "") != scope_fingerprint:
                continue
            manifest_ids.add(str(manifest["manifest_id"]))
        for manifest_id in sorted(manifest_ids):
            manifest_path = self._manifest_path(manifest_id)
            if manifest_path.exists():
                manifest_path.unlink()
                if manifest_path.exists():
                    connection.execute("ROLLBACK")
                    raise S16OwnerFailure(
                        self.owner_id,
                        S16_VERIFY_FAILED,
                        retryable=True,
                        responsible_party="backup_operations_owner",
                        recovery_action="verify_the_backup_manifest_removal",
                    )
            elif not resume:
                connection.execute("ROLLBACK")
                raise S16OwnerFailure(
                    self.owner_id,
                    S16_VERIFY_FAILED,
                    retryable=True,
                    responsible_party="backup_operations_owner",
                    recovery_action="restore_and_verify_the_backup_manifest",
                )
        # Every file and manifest verified gone: commit the registry
        # deletions, the intent completion and the operation binding.
        connection.executemany(
            "DELETE FROM backup_registry WHERE connector_identity = ?",
            ((identity,) for identity in sorted(identities)),
        )
        connection.execute(
            "UPDATE backup_deletion_intents SET status = ? "
            "WHERE operation_id = ? AND fence = ?",
            ("committed", operation_id, int(fence)),
        )
        connection.execute(
            "INSERT INTO backup_deletion_bindings("
            "operation_id, fence, scope_fingerprint, fingerprints_digest, "
            "status, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                operation_id,
                int(fence),
                scope_fingerprint,
                fingerprints_digest,
                "complete",
                int(self._clock()),
            ),
        )
        connection.execute("COMMIT")
        return {
            "status": "complete",
            "deleted_counts": {"replica": deleted},
        }

    def verify_absent(
        self,
        copy_fingerprints: Iterable[str],
        *,
        scope_fingerprint: str,
        operation_id: str | None = None,
        fence: int | None = None,
    ) -> dict[str, Any]:
        fingerprints = set(copy_fingerprints)
        if operation_id is not None and fence is not None:
            # R4 (P1-3): worker/replay absence proofs are BINDING proofs —
            # the exact operation + fence must own this scope and
            # fingerprints digest; a stale fence is a stable stale outcome
            # and a mismatched binding is a stable conflict.  A staged
            # (uncommitted) deletion intent is never verified.
            with self._registry_connect() as connection:
                binding = connection.execute(
                    "SELECT scope_fingerprint, fingerprints_digest "
                    "FROM backup_deletion_bindings "
                    "WHERE operation_id = ? AND fence = ?",
                    (operation_id, int(fence)),
                ).fetchone()
                if binding is not None:
                    if (
                        str(binding[0]) != scope_fingerprint
                        or str(binding[1])
                        != self._bindings_digest(fingerprints)
                    ):
                        raise S16OwnerFailure(
                            self.owner_id,
                            S16_OWNER_BINDING_CONFLICT,
                            retryable=False,
                            responsible_party="runtime_operations_owner",
                            recovery_action=(
                                "reconcile_scope_binding_and_resume"
                            ),
                        )
                    return {
                        "owner_id": self.owner_id,
                        "status": "verified",
                    }
                highest = connection.execute(
                    "SELECT MAX(fence) FROM backup_deletion_bindings "
                    "WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
                if highest[0] is not None and int(highest[0]) > int(fence):
                    raise S16OwnerFailure(
                        self.owner_id,
                        S16_OWNER_STALE_FENCE,
                        retryable=False,
                        responsible_party="runtime_operations_owner",
                        recovery_action="reconcile_operation_fence_and_resume",
                    )
                staged = connection.execute(
                    "SELECT status FROM backup_deletion_intents "
                    "WHERE operation_id = ? AND fence = ?",
                    (operation_id, int(fence)),
                ).fetchone()
                if staged is not None:
                    raise S16OwnerFailure(
                        self.owner_id,
                        S16_VERIFY_FAILED,
                        retryable=True,
                        responsible_party="backup_operations_owner",
                        recovery_action=(
                            "resume_the_staged_backup_deletion"
                        ),
                    )
            raise S16OwnerFailure(
                self.owner_id,
                S16_VERIFY_FAILED,
                retryable=True,
                responsible_party="backup_operations_owner",
                recovery_action="verify_backup_absence_and_resume_the_same_job",
            )
        # Readiness scope probe (no operation/fence): the shared
        # reconciliation first — integrity damage closes readiness — then
        # the scope manifest scan.
        self._backup_reconciliation()
        remaining = self._manifests_for_fingerprints(
            fingerprints, scope_fingerprint=scope_fingerprint
        )
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
        self,
        copy_fingerprints: Iterable[str],
        *,
        scope_fingerprint: str,
        operation_id: str = "s16-backup-replay",
        fence: int = 0,
    ) -> dict[str, Any]:
        return self.delete(
            copy_fingerprints,
            scope_fingerprint=scope_fingerprint,
            operation_id=operation_id,
            fence=fence,
        )

    def store_revision(self) -> str:
        """Value-free owner revision: digest of the registry identities
        (no handles, paths or digests are exposed) (R3 P1-13)."""
        with self._registry_connect() as connection:
            rows = connection.execute(
                "SELECT connector_identity FROM backup_registry"
            ).fetchall()
        return _digest({"backup_registry": sorted(r[0] for r in rows)})

    def owner_healthy(self) -> bool:
        try:
            self._backup_reconciliation()
            return True
        except Exception:
            return False

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

    def store_revision(self) -> str:
        """Fixed zero-proof revision while S17 export stays disabled
        (R3 P1-13): the owner provably holds no copies."""
        return _digest({"owner_id": self.owner_id, "export_disabled": True})

    def owner_healthy(self) -> bool:
        return True

    def verify_repair(self, owner_id: str, repair_fact: str) -> bool:
        return owner_id == self.owner_id


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class GovernedDeletionService:
    """The S16 orchestrator: preflight, legal hold, approvals, commit,
    durable worker, repair, receipts and startup restore replay."""

    GOVERNANCE_ROLE = "operator"
    GOVERNANCE_SOURCE_ID = "s16-governance-console"
    APPROVER_ROLE = "operator"
    APPROVER_SOURCE_ID = "s16-approval-desk"
    WORKER_ROLE = "system"

    def __init__(
        self,
        *,
        ledger_path: str | Path,
        owners: dict[str, DeletionOwner],
        retention: RetentionPolicy,
        governance_subject: str,
        approver_subjects: Iterable[str],
        governance_scope: str = "C-DEMO",
        worker_id: str = "s16-deletion-worker",
        audit_available: bool = True,
        storage_available: bool = True,
        security_audit_available: bool = True,
        security_audit_writer: Callable[[dict[str, Any]], bool] | None = None,
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
        # R1: the system worker identity must be isolated from every
        # governance/approver identity.
        if worker_id in {governance_subject, *approvers}:
            raise ValueError(
                "S16 worker identity must not alias governance or approver subjects"
            )
        if not isinstance(governance_scope, str) or not governance_scope:
            raise ValueError("S16 governance scope must be canonical")
        missing = REQUIRED_OWNERS.difference(owners)
        if missing:
            raise ValueError(
                f"S16 required owners are missing: {sorted(missing)}"
            )
        self._ledger = S16Ledger(ledger_path)
        self._owners = dict(owners)
        self._retention = retention
        self._governance_subject = governance_subject
        self._governance_scope = governance_scope
        self._approvers = frozenset(approvers)
        self._worker_id = worker_id
        self.audit_available = bool(audit_available)
        self.storage_available = bool(storage_available)
        if (
            security_audit_available
            and security_audit_writer is not None
            and not callable(security_audit_writer)
        ):
            raise ValueError(
                "S16 security audit writer must be callable when configured"
            )
        if (
            not security_audit_available
            and security_audit_writer is not None
            and callable(security_audit_writer)
        ):
            raise ValueError(
                "S16 security audit writer configured without availability"
            )
        if security_audit_available and security_audit_writer is None:
            raise ValueError(
                "S16 security audit availability requires a configured "
                "callable writer (R3 P1-5)"
            )
        self.security_audit_available = bool(
            security_audit_available and callable(security_audit_writer)
        )
        self._security_audit_writer = security_audit_writer
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
        self._replays = self._ledger._load_replays()
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

    _HOLD_TRANSITION_TYPES = frozenset(
        {"legal_hold_imposed", "legal_hold_released", "legal_hold_expired"}
    )

    def _hold_generation(self, scope_fingerprint: str) -> int:
        """Monotonic Legal Hold generation: every impose, release and expiry
        transition advances the generation so stale manifests never survive
        a hold change (R2 P1-7, R3 P1-6)."""
        return len(
            [
                event
                for event in self._events
                if event.get("event_type") in self._HOLD_TRANSITION_TYPES
                and event.get("scope_fingerprint") == scope_fingerprint
            ]
        )

    def _hold_generation_from_connection(
        self, connection: sqlite3.Connection, scope_fingerprint: str
    ) -> int:
        """The generation computed from the authoritative database rows
        inside the command transaction (R3 P1-6: never from a snapshot)."""
        rows = connection.execute(
            "SELECT payload FROM s16_events"
        ).fetchall()
        return len(
            [
                row
                for row in rows
                if (
                    (event := json.loads(row[0])).get("event_type")
                    in self._HOLD_TRANSITION_TYPES
                    and event.get("scope_fingerprint") == scope_fingerprint
                )
            ]
        )

    def _expire_holds_in_transaction(
        self, connection: sqlite3.Connection, scope_fingerprint: str
    ) -> None:
        """Append explicit expiry transitions for holds whose expiry passed
        (R3 P1-6): expiry advances the generation inside the same command
        transaction so stale manifests can never survive an expiry."""
        rows = connection.execute(
            "SELECT payload FROM s16_events"
        ).fetchall()
        events = [json.loads(row[0]) for row in rows]
        for hold in events:
            if not (
                hold.get("event_type") == "legal_hold_imposed"
                and hold.get("scope_fingerprint") == scope_fingerprint
            ):
                continue
            hold_id = str(hold.get("hold_id") or "")
            if not hold_id:
                continue
            released = any(
                item.get("event_type") == "legal_hold_released"
                and item.get("hold_id") == hold_id
                for item in events
            )
            expired = any(
                item.get("event_type") == "legal_hold_expired"
                and item.get("hold_id") == hold_id
                for item in events
            )
            expiry = hold.get("expiry")
            is_expired = (
                isinstance(expiry, (int, float))
                and not isinstance(expiry, bool)
                and int(expiry) <= self._now()
            )
            if released or expired or not is_expired:
                continue
            self._ledger._append_event(
                connection,
                {
                    "event_type": "legal_hold_expired",
                    "event_id": self._event_id("hold_expired", hold_id),
                    "request_id": str(hold.get("request_id") or ""),
                    "hold_id": hold_id,
                    "scope_fingerprint": scope_fingerprint,
                    "generation": self._hold_generation_from_connection(
                        connection, scope_fingerprint
                    )
                    + 1,
                    "expired_at": self._now(),
                    "appended_at": self._now(),
                },
                self._now(),
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

    # -- principal validation (R1: subject + role + scope + source + expiry)
    # ----------------------------------------------------------------

    def _principal_identity(
        self, principal: Any
    ) -> tuple[str, str, str, str, float | None] | None:
        """(subject, role, scope, source_id, expires_at) when the principal
        is a well-formed registered principal, else None."""
        if not hasattr(principal, "subject"):
            return None
        subject = getattr(principal, "subject", None)
        role = getattr(principal, "role", None)
        scope = getattr(principal, "scope", None)
        source_id = getattr(principal, "source_id", None)
        expires_at = getattr(principal, "expires_at", None)
        if not all(
            isinstance(value, str) and value
            for value in (subject, role, scope, source_id)
        ):
            return None
        if expires_at is not None and (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
        ):
            return None
        if expires_at is not None and expires_at <= self._now():
            return None
        return (subject, role, scope, source_id, expires_at)

    def _require_governance(self, principal: Any) -> str:
        identity = self._principal_identity(principal)
        if identity is None:
            raise S16Forbidden("governed deletion governance identity required")
        subject, role, scope, source_id, _expires_at = identity
        if (
            subject != self._governance_subject
            or role != self.GOVERNANCE_ROLE
            or scope != self._governance_scope
            or source_id != self.GOVERNANCE_SOURCE_ID
        ):
            raise S16Forbidden("governed deletion governance identity required")
        return subject

    def _require_approver(self, principal: Any) -> str:
        identity = self._principal_identity(principal)
        if identity is None:
            raise S16Forbidden("registered S16 deletion approver required")
        subject, role, scope, source_id, _expires_at = identity
        if (
            subject not in self._approvers
            or role != self.APPROVER_ROLE
            or scope != self._governance_scope
            or source_id != self.APPROVER_SOURCE_ID
        ):
            raise S16Forbidden("registered S16 deletion approver required")
        return subject

    def _binding_key(
        self,
        principal: Any,
        idempotency_key: str,
    ) -> str:
        """Idempotency binding binds subject, role, scope, source and the
        caller key (R1)."""
        identity = self._principal_identity(principal)
        subject = (
            identity[0]
            if identity is not None
            else str(getattr(principal, "subject", ""))
        )
        role = identity[1] if identity is not None else ""
        scope = identity[2] if identity is not None else ""
        source_id = identity[3] if identity is not None else ""
        return _digest(
            {
                "subject": subject,
                "role": role,
                "scope": scope,
                "source_id": source_id,
                "idempotency_key": idempotency_key,
            }
        )

    # -- security audit seam (R1) ----------------------------------------

    def _write_security_audit(
        self,
        connection: sqlite3.Connection,
        *,
        action: str,
        subject: str,
        role: str,
        scope_fingerprint: str,
        reason_code: str,
        request_id: str = "",
        job_id: str = "",
    ) -> dict[str, Any]:
        """Append one value-free security-audit fact inside the same
        protected-command transaction; the external WORM/SIEM copy happens
        after commit (full-copy semantics)."""
        audit_event_id = self._event_id("security_audit", f"{action}:{subject}")
        fact = {
            "event_type": "security_audit",
            "event_id": audit_event_id,
            "action": action,
            "subject_fingerprint": application_id_fingerprint(subject),
            "role": role,
            "scope_fingerprint": scope_fingerprint,
            "reason_code": reason_code,
            "request_id_fingerprint": application_id_fingerprint(request_id),
            "job_id_fingerprint": application_id_fingerprint(job_id),
            "appended_at": self._now(),
        }
        self._ledger._append_event(connection, fact, self._now())
        return fact

    def _replicate_security_audit(
        self, fact: dict[str, Any]
    ) -> None:
        """Post-commit full-copy replication to the injected security-audit
        owner (WORM/SIEM).  Failure is recorded as a stable replication
        fact; the committed command is never rolled back (full-copy
        semantics)."""
        status = "failed"
        if self._security_audit_writer is not None:
            try:
                writer_ok = self._security_audit_writer(
                    {
                        "schema_version": "s16-security-audit/1",
                        "audit_event_id": str(fact.get("event_id") or ""),
                        "action": str(fact.get("action") or ""),
                        "subject_fingerprint": str(
                            fact.get("subject_fingerprint") or ""
                        ),
                        "role": str(fact.get("role") or ""),
                        "scope_fingerprint": str(
                            fact.get("scope_fingerprint") or ""
                        ),
                        "reason_code": str(fact.get("reason_code") or ""),
                        "appended_at": int(fact.get("appended_at") or 0),
                    }
                )
            except Exception:
                writer_ok = False
            status = "replicated" if writer_ok else "failed"
        elif self.security_audit_available:
            status = "not_configured"
        with self._lock:
            event = {
                "event_type": "security_audit_replication",
                "event_id": self._event_id(
                    "security_audit_replication", str(fact.get("event_id") or "")
                ),
                "audit_event_id": str(fact.get("event_id") or ""),
                "status": status,
                "appended_at": self._now(),
            }
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._ledger._append_event(connection, event, self._now())
                connection.commit()
            self._reload()

    def _require_security_audit(self) -> None:
        if not self.security_audit_available:
            raise S16Blocked(S16_AUDIT_SEAM_UNAVAILABLE)

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
        # R2 (P1-6): preflight is a protected command; the audit seam must
        # be available before any ledger fact is written.
        self._require_security_audit()
        request_id = request_id or _stable_id(
            "s16req", f"{subject}:{application_reference}:{secrets.token_hex(4)}"
        )
        if len(request_id) > 200 or request_id.strip() != request_id:
            raise S16Blocked("S16_INVALID_REQUEST_ID")
        binding_key = self._binding_key(principal, idempotency_key)
        command_fingerprint = _digest(
            {
                "action": "preflight",
                "application_reference_fingerprint": hashlib.sha256(
                    application_reference.encode("utf-8")
                ).hexdigest(),
                "subject": subject,
                "role": self.GOVERNANCE_ROLE,
                "scope": self._governance_scope,
                "source_id": self.GOVERNANCE_SOURCE_ID,
            }
        )
        with self._lock:
            self._reload()
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    # R3 (P1-8): the binding persists the COMPLETE value-free
                    # preflight result, so a same-key retry returns the
                    # original manifest snapshot — never a re-inventory that
                    # could change under later backup/hold/revision churn.
                    existing_binding = self._ledger._binding_read(
                        connection, binding_key
                    )
                    if existing_binding is not None:
                        existing_fingerprint, existing_result = existing_binding
                        if existing_fingerprint != command_fingerprint:
                            connection.rollback()
                            raise S16Conflict(
                                "S16 idempotency conflict: same key different content"
                            )
                        # R3 (P1-8): a same-key retry replays the FIRST
                        # manifest snapshot, but only while the scope still
                        # resolves — after completion the reference
                        # existence-hides (404) exactly like a fresh key.
                        if (
                            self._owners["s01"].resolve_application_reference(
                                application_reference, self._governance_scope
                            )
                            is None
                        ):
                            connection.rollback()
                            raise S16NotFound(application_reference)
                        connection.commit()
                        return {
                            **existing_result,
                            "application_reference": application_reference,
                            "replayed": True,
                        }
                    scope_fingerprint = self._owners[
                        "s01"
                    ].resolve_application_reference(
                        application_reference, self._governance_scope
                    )
                    if scope_fingerprint is None:
                        connection.rollback()
                        raise S16NotFound(application_reference)
                    self._expire_holds_in_transaction(
                        connection, scope_fingerprint
                    )
                    manifest = self._build_manifest(
                        scope_fingerprint,
                        hold_generation_override=(
                            self._hold_generation_from_connection(
                                connection, scope_fingerprint
                            )
                        ),
                    )
                    request_event = {
                        "event_type": "request",
                        "request_id": request_id,
                        "subject": subject,
                        "role": self.GOVERNANCE_ROLE,
                        "scope": self._governance_scope,
                        "source_id": self.GOVERNANCE_SOURCE_ID,
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
                        "owner_registry_digest": manifest[
                            "owner_registry_digest"
                        ],
                        "policy_id": manifest["policy_id"],
                        "policy_version": manifest["policy_version"],
                        "policy_digest": manifest["policy_digest"],
                        "hold_generation": manifest["hold_generation"],
                        "s01_revision": manifest["s01_revision"],
                        "s12_revision": manifest["s12_revision"],
                        "backup_revision": manifest["backup_revision"],
                        "s17_revision": manifest["s17_revision"],
                        "retention_due": manifest["retention_due"],
                        "early_deletion": manifest["early_deletion"],
                        "retained_scan_clean": manifest["retained_scan_clean"],
                        "retained_scan_digest": manifest[
                            "retained_scan_digest"
                        ],
                        "appended_at": self._now(),
                        "event_id": self._event_id("preflight", request_id),
                    }
                    self._ledger._append_event(
                        connection, request_event, self._now()
                    )
                    self._ledger._append_event(
                        connection, preflight_event, self._now()
                    )
                    # R4 (P1-1): the persistent binding stores ONLY the
                    # value-free preflight snapshot — never the application
                    # reference or any recoverable locator.  The authorized
                    # in-memory response echoes the reference; a same-key
                    # replay rebuilds the no-value response from this
                    # snapshot plus the caller-supplied reference.
                    stored_response = self._preflight_response(
                        manifest=manifest,
                        request_id=request_id,
                        application_reference=None,
                    )
                    self._ledger._binding_insert(
                        connection,
                        binding_key=binding_key,
                        fingerprint=command_fingerprint,
                        result=stored_response,
                        now=self._now(),
                    )
                    connection.commit()
                except S16Blocked:
                    connection.rollback()
                    raise
                except S16Conflict:
                    connection.rollback()
                    raise
                except Exception:
                    connection.rollback()
                    raise
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
        application_reference: str | None = None,
        replayed: bool = False,
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "status": "accepted",
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
        if application_reference is not None:
            response["application_reference"] = application_reference
        return response

    def _build_manifest(
        self,
        scope_fingerprint: str,
        *,
        hold_generation_override: int | None = None,
    ) -> dict[str, Any]:
        s01_owner = self._owners["s01"]
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
        entries = self._fill_retention_due(entries, scope_fingerprint)
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
        s01_revision = int(s01_owner.store_revision())
        s12_revision = str(self._owners["s12"].store_revision())
        backup_revision = str(self._owners["backup"].store_revision())
        s17_revision = str(self._owners["s17-disabled"].store_revision())
        retained_scan = s01_owner.retained_scan(scope_fingerprint)
        hold_generation = (
            self._hold_generation(scope_fingerprint)
            if hold_generation_override is None
            else int(hold_generation_override)
        )
        manifest_digest = _digest(
            {
                "schema_version": S16_SCHEMA_VERSION,
                "scope_fingerprint": scope_fingerprint,
                "entries_digest": entries_digest,
                "owner_registry_digest": s16_owner_registry_digest(),
                "policy_digest": self._retention.digest(),
                "hold_generation": hold_generation,
                "s01_revision": s01_revision,
                "s12_revision": s12_revision,
                "backup_revision": backup_revision,
                "s17_revision": s17_revision,
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
            "policy_id": self._retention.policy_id,
            "policy_version": self._retention.policy_version,
            "policy_digest": self._retention.digest(),
            "hold_generation": hold_generation,
            "s01_revision": s01_revision,
            "s12_revision": s12_revision,
            "backup_revision": backup_revision,
            "s17_revision": s17_revision,
            "manifest_digest": manifest_digest,
            "retention_due": min(due_ats) if due_ats else None,
            "early_deletion": early_deletion,
            "retained_scan_clean": bool(retained_scan["clean"]),
            "retained_scan_digest": retained_scan["digest"],
        }

    def _fill_retention_due(
        self,
        entries: list[CopyInventoryEntry],
        scope_fingerprint: str,
    ) -> list[CopyInventoryEntry]:
        """Non-S01 owners (S02 objects, evaluation copies, backup copies)
        share the application's retention clock: their due time is derived
        from the S01 terminated fact so an early-deletion decision is
        uniform across every owner."""
        terminated_at = self._owners["s01"].terminated_at(scope_fingerprint)
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
        idempotency_key: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        subject = self._require_governance(principal)
        if reason_code not in LEGAL_HOLD_REASON_CODES:
            raise S16Blocked("S16_INVALID_HOLD_REASON")
        if owner not in LEGAL_HOLD_OWNERS:
            raise S16Blocked("S16_INVALID_HOLD_OWNER")
        if (
            isinstance(effective_time, bool)
            or not isinstance(effective_time, int)
            or effective_time < 1
        ):
            raise S16Blocked("S16_INVALID_HOLD_TIME")
        if expiry is not None and (
            isinstance(expiry, bool)
            or not isinstance(expiry, int)
            or expiry < effective_time
        ):
            raise S16Blocked("S16_INVALID_HOLD_EXPIRY")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise S16Blocked("S16_INVALID_IDEMPOTENCY_KEY")
        if not isinstance(request_id, str) or not request_id:
            request_id = _stable_id(
                "s16hold", f"{subject}:{scope_fingerprint}:{secrets.token_hex(4)}"
            )
        # Scope existence gate: the hold binds to a live governed
        # application scope (existence-hiding for unknown scopes).
        if not self._owners["s01"].scope_exists(scope_fingerprint):
            raise S16NotFound(scope_fingerprint)
        if not self.audit_available:
            raise S16Blocked(S16_AUDIT_UNAVAILABLE)
        self._require_security_audit()
        binding_key = self._binding_key(principal, idempotency_key)
        command_fingerprint = _digest(
            {
                "action": "impose_legal_hold",
                "request_id": request_id,
                "scope_fingerprint": scope_fingerprint,
                "reason_code": reason_code,
                "owner": owner,
                "effective_time": effective_time,
                "expiry": expiry,
                "subject": subject,
                "role": self.GOVERNANCE_ROLE,
                "scope": self._governance_scope,
                "source_id": self.GOVERNANCE_SOURCE_ID,
            }
        )
        with self._lock:
            self._reload()
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._expire_holds_in_transaction(
                    connection, scope_fingerprint
                )
                generation = (
                    self._hold_generation_from_connection(
                        connection, scope_fingerprint
                    )
                    + 1
                )
                hold_id = _stable_id(
                    "hold", f"{scope_fingerprint}:{generation}"
                )
                binding_outcome, stored_result = self._bind_or_replay(
                    connection,
                    binding_key=binding_key,
                    command_fingerprint=command_fingerprint,
                    result={
                        "status": "accepted",
                        "hold_id": hold_id,
                        "generation": generation,
                        "request_id": request_id,
                    },
                )
                if binding_outcome == "conflict":
                    connection.rollback()
                    raise S16Conflict(
                        "S16 idempotency conflict: same key different content"
                    )
                if binding_outcome == "replayed":
                    connection.commit()
                    return {**stored_result, "replayed": True}
                event = {
                    "event_type": "legal_hold_imposed",
                    "event_id": self._event_id("hold", hold_id),
                    "request_id": request_id,
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
                self._ledger._append_event(connection, event, self._now())
                audit_fact = self._write_security_audit(
                    connection,
                    action="legal_hold_imposed",
                    subject=subject,
                    role=self.GOVERNANCE_ROLE,
                    scope_fingerprint=scope_fingerprint,
                    reason_code=reason_code,
                    request_id=request_id,
                )
                connection.commit()
            self._reload()
        self._replicate_security_audit(audit_fact)
        return {
            "status": "accepted",
            "hold_id": hold_id,
            "generation": generation,
            "scope_fingerprint": scope_fingerprint,
            "request_id": request_id,
        }

    def release_legal_hold(
        self,
        *,
        hold_id: str,
        principal: Any,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        subject = self._require_governance(principal)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise S16Blocked("S16_INVALID_IDEMPOTENCY_KEY")
        if not self.audit_available:
            raise S16Blocked(S16_AUDIT_UNAVAILABLE)
        self._require_security_audit()
        binding_key = self._binding_key(principal, idempotency_key)
        command_fingerprint = _digest(
            {
                "action": "release_legal_hold",
                "hold_id": hold_id,
                "subject": subject,
                "role": self.GOVERNANCE_ROLE,
                "scope": self._governance_scope,
                "source_id": self.GOVERNANCE_SOURCE_ID,
            }
        )
        release_request_id = _stable_id(
            "s16release", f"{subject}:{hold_id}:{secrets.token_hex(4)}"
        )
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
            scope_fingerprint = str(hold.get("scope_fingerprint") or "")
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._expire_holds_in_transaction(
                    connection, scope_fingerprint
                )
                release_rows = connection.execute(
                    "SELECT payload FROM s16_events"
                ).fetchall()
                release_events = [
                    json.loads(row[0]) for row in release_rows
                ]
                already_released = any(
                    event.get("event_type") == "legal_hold_released"
                    and event.get("hold_id") == hold_id
                    for event in release_events
                )
                if already_released:
                    # R3 (P1-6): the terminal fact is the REAL prior release
                    # (its generation and request id), never a fabricated
                    # next generation; the new key is bound to that fact.
                    prior = [
                        event
                        for event in release_events
                        if event.get("event_type") == "legal_hold_released"
                        and event.get("hold_id") == hold_id
                    ][-1]
                    release_generation = int(prior.get("generation") or 0)
                    release_request_id = str(prior.get("request_id") or "")
                    terminal_result = {
                        "status": "accepted",
                        "hold_id": hold_id,
                        "generation": release_generation,
                        "request_id": release_request_id,
                    }
                    binding_outcome, stored_result = self._bind_or_replay(
                        connection,
                        binding_key=binding_key,
                        command_fingerprint=command_fingerprint,
                        result=terminal_result,
                    )
                    if binding_outcome == "conflict":
                        connection.rollback()
                        raise S16Conflict(
                            "S16 idempotency conflict: same key different content"
                        )
                    connection.commit()
                    return {
                        **terminal_result,
                        "status": "replayed",
                        "replayed": True,
                    }
                release_generation = (
                    self._hold_generation_from_connection(
                        connection, scope_fingerprint
                    )
                    + 1
                )
                binding_outcome, stored_result = self._bind_or_replay(
                    connection,
                    binding_key=binding_key,
                    command_fingerprint=command_fingerprint,
                    result={
                        "status": "accepted",
                        "hold_id": hold_id,
                        "generation": release_generation,
                        "request_id": release_request_id,
                    },
                )
                if binding_outcome == "conflict":
                    connection.rollback()
                    raise S16Conflict(
                        "S16 idempotency conflict: same key different content"
                    )
                if binding_outcome == "replayed":
                    connection.commit()
                    return {**stored_result, "replayed": True}
                event = {
                    "event_type": "legal_hold_released",
                    "event_id": self._event_id("hold_release", hold_id),
                    "request_id": release_request_id,
                    "hold_id": hold_id,
                    "scope_fingerprint": scope_fingerprint,
                    "generation": release_generation,
                    "released_by": subject,
                    "appended_at": self._now(),
                }
                self._ledger._append_event(connection, event, self._now())
                audit_fact = self._write_security_audit(
                    connection,
                    action="legal_hold_released",
                    subject=subject,
                    role=self.GOVERNANCE_ROLE,
                    scope_fingerprint=scope_fingerprint,
                    reason_code="legal_hold_released",
                    # R4 (P1-2): the audit fact reconciles with the release
                    # event and the HTTP response — never the impose
                    # request id.
                    request_id=release_request_id,
                )
                connection.commit()
            self._reload()
        self._replicate_security_audit(audit_fact)
        return {
            "status": "accepted",
            "hold_id": hold_id,
            "request_id": release_request_id,
            "generation": release_generation,
        }

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
        if not self.audit_available:
            raise S16Blocked(S16_AUDIT_UNAVAILABLE)
        self._require_security_audit()
        binding_key = self._binding_key(principal, idempotency_key)
        command_fingerprint = _digest(
            {
                "action": "approve",
                "request_id": request_id,
                "manifest_digest": manifest_digest,
                "subject": subject,
                "role": self.APPROVER_ROLE,
                "scope": self._governance_scope,
                "source_id": self.APPROVER_SOURCE_ID,
            }
        )
        with self._lock:
            self._reload()
            requests = self._request_events(request_id)
            if not requests:
                raise S16NotFound(request_id)
            requester = str(requests[0].get("subject") or "")
            if subject == requester:
                raise S16Forbidden("an approver must differ from the requester")
            approval_result = {
                "status": "accepted",
                "request_id": request_id,
                "approved_by": subject,
            }
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    binding_outcome, stored_result = self._bind_or_replay(
                        connection,
                        binding_key=binding_key,
                        command_fingerprint=command_fingerprint,
                        result=approval_result,
                    )
                    if binding_outcome == "conflict":
                        connection.rollback()
                        raise S16Conflict(
                            "S16 idempotency conflict: same key different content"
                        )
                    if binding_outcome == "replayed":
                        connection.commit()
                        return {**stored_result, "replayed": True}
                    preflights = self._preflight_events(request_id)
                    if (
                        not preflights
                        or preflights[0].get("manifest_digest")
                        != manifest_digest
                    ):
                        connection.rollback()
                        raise S16Blocked(S16_MANIFEST_STALE)
                    existing = [
                        event
                        for event in self._events_of("approval", request_id)
                        if event.get("subject") == subject
                    ]
                    if existing:
                        # Terminal action bound to the CURRENT key.
                        connection.commit()
                        return {
                            **approval_result,
                            "status": "replayed",
                            "replayed": True,
                        }
                    event = {
                        "event_type": "approval",
                        "event_id": self._event_id("approval", request_id),
                        "request_id": request_id,
                        "subject": subject,
                        "role": self.APPROVER_ROLE,
                        "manifest_digest": manifest_digest,
                        "scope_fingerprint": str(
                            preflights[0].get("scope_fingerprint") or ""
                        ),
                        "appended_at": self._now(),
                    }
                    self._ledger._append_event(connection, event, self._now())
                    audit_fact = self._write_security_audit(
                        connection,
                        action="approval",
                        subject=subject,
                        role=self.APPROVER_ROLE,
                        scope_fingerprint=str(
                            preflights[0].get("scope_fingerprint") or ""
                        ),
                        reason_code="deletion_approval",
                        request_id=request_id,
                    )
                    connection.commit()
                except S16Conflict:
                    connection.rollback()
                    raise
                except Exception:
                    connection.rollback()
                    raise
            self._reload()
        self._replicate_security_audit(audit_fact)
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
        if not self.audit_available:
            raise S16Blocked(S16_AUDIT_UNAVAILABLE)
        self._require_security_audit()
        binding_key = self._binding_key(principal, idempotency_key)
        command_fingerprint = _digest(
            {
                "action": "cancel",
                "request_id": request_id,
                "subject": subject,
                "role": self.GOVERNANCE_ROLE,
                "scope": self._governance_scope,
                "source_id": self.GOVERNANCE_SOURCE_ID,
            }
        )
        with self._lock:
            self._reload()
            job = self._job_for_request(request_id)
            if job is not None:
                # Commit is the irreversible boundary.
                raise S16Conflict(S16_ALREADY_COMMITTED)
            if not self._request_events(request_id):
                raise S16NotFound(request_id)
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    cancel_result = {
                        "status": "accepted",
                        "request_id": request_id,
                    }
                    binding_outcome, stored_result = self._bind_or_replay(
                        connection,
                        binding_key=binding_key,
                        command_fingerprint=command_fingerprint,
                        result=cancel_result,
                    )
                    if binding_outcome == "conflict":
                        connection.rollback()
                        raise S16Conflict(
                            "S16 idempotency conflict: same key different content"
                        )
                    if binding_outcome == "replayed":
                        connection.commit()
                        return {**stored_result, "replayed": True}
                    already_cancelled = any(
                        event.get("event_type") == "cancel"
                        and event.get("request_id") == request_id
                        for event in self._events
                    )
                    if already_cancelled:
                        # Terminal action bound to the CURRENT key.
                        connection.commit()
                        return {
                            **cancel_result,
                            "status": "replayed",
                            "replayed": True,
                        }
                    event = {
                        "event_type": "cancel",
                        "event_id": self._event_id("cancel", request_id),
                        "request_id": request_id,
                        "subject": subject,
                        "role": self.GOVERNANCE_ROLE,
                        "appended_at": self._now(),
                    }
                    self._ledger._append_event(connection, event, self._now())
                    audit_fact = self._write_security_audit(
                        connection,
                        action="cancel",
                        subject=subject,
                        role=self.GOVERNANCE_ROLE,
                        scope_fingerprint=str(
                            (
                                self._preflight_events(request_id) or [{}]
                            )[0].get("scope_fingerprint")
                            or ""
                        ),
                        reason_code="deletion_cancel",
                        request_id=request_id,
                    )
                    connection.commit()
                except S16Conflict:
                    connection.rollback()
                    raise
                except Exception:
                    connection.rollback()
                    raise
            self._reload()
        self._replicate_security_audit(audit_fact)
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
        if not self.audit_available:
            raise S16Blocked(S16_AUDIT_UNAVAILABLE)
        self._require_security_audit()
        binding_key = self._binding_key(principal, idempotency_key)
        command_fingerprint = _digest(
            {
                "action": "commit",
                "request_id": request_id,
                "subject": subject,
                "role": self.GOVERNANCE_ROLE,
                "scope": self._governance_scope,
                "source_id": self.GOVERNANCE_SOURCE_ID,
            }
        )
        with self._lock:
            self._reload()
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
            preflights = self._preflight_events(request_id)
            if not preflights:
                raise S16NotFound(request_id)
            # Re-check the held facts inside one short ledger transaction:
            # the transaction re-reads the ledger facts from the database so
            # impose/release/commit race is arbitrated by SQLite itself.
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    commit_result = {
                        "status": "accepted",
                        "request_id": request_id,
                        "job_id": (
                            str(existing_job["job_id"])
                            if existing_job is not None
                            else None
                        ),
                    }
                    binding_outcome, stored_result = self._bind_or_replay(
                        connection,
                        binding_key=binding_key,
                        command_fingerprint=command_fingerprint,
                        result=commit_result,
                    )
                    if binding_outcome == "conflict":
                        connection.rollback()
                        raise S16Conflict(
                            "S16 idempotency conflict: same key different content"
                        )
                    if binding_outcome == "replayed":
                        connection.commit()
                        return {**stored_result, "replayed": True}
                    if existing_job is not None:
                        # Terminal action bound to the CURRENT key.
                        connection.commit()
                        return {
                            **commit_result,
                            "status": "replayed",
                            "replayed": True,
                        }
                    ledger_facts = self._ledger_facts_in_transaction(
                        connection, request_id
                    )
                    reason = self._commit_block_reason(
                        scope_fingerprint=ledger_facts["scope_fingerprint"],
                        manifest=ledger_facts["manifest"],
                        request_id=request_id,
                        requester=ledger_facts["requester"],
                        subject=subject,
                        hold_generation=ledger_facts["hold_generation"],
                        active_holds=ledger_facts["active_holds"],
                    )
                    if reason is not None:
                        raise S16Blocked(reason)
                    job_id = _stable_id(
                        "s16job",
                        f"{ledger_facts['scope_fingerprint']}:{request_id}",
                    )
                    job = {
                        "job_id": job_id,
                        "request_id": request_id,
                        "scope_fingerprint": ledger_facts["scope_fingerprint"],
                        "manifest_digest": ledger_facts["manifest"]["manifest_digest"],
                        "status": "pending",
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "fence": 0,
                        "attempt": 0,
                        "pending_owner_fingerprints": self._owner_fingerprint_map(
                            ledger_facts["manifest"]
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
                        "scope_fingerprint": ledger_facts["scope_fingerprint"],
                        "manifest_digest": ledger_facts["manifest"][
                            "manifest_digest"
                        ],
                        "subject": subject,
                        "role": self.GOVERNANCE_ROLE,
                        "appended_at": self._now(),
                    }
                    self._ledger._append_event(connection, commit_event, self._now())
                    self._ledger._upsert_job(connection, job, self._now())
                    audit_fact = self._write_security_audit(
                        connection,
                        action="commit",
                        subject=subject,
                        role=self.GOVERNANCE_ROLE,
                        scope_fingerprint=ledger_facts["scope_fingerprint"],
                        reason_code="deletion_commit",
                        request_id=request_id,
                        job_id=job_id,
                    )
                    connection.commit()
                except S16Blocked:
                    connection.rollback()
                    raise
                except Exception:
                    connection.rollback()
                    raise
            self._reload()
        self._replicate_security_audit(audit_fact)
        return {
            "status": "accepted",
            "request_id": request_id,
            "job_id": job_id,
        }

    def _ledger_facts_in_transaction(
        self, connection: sqlite3.Connection, request_id: str
    ) -> dict[str, Any]:
        """Re-read the ledger facts from the database inside the commit
        transaction so hold impose/release and commit race on SQLite's
        write lock (no time-ordering).  Every row's integrity digest is
        verified inside the transaction (R3 P1-11): a tampered event can
        never seed a deletion job."""
        rows = connection.execute(
            "SELECT event_id, payload, integrity_sha256 FROM s16_events"
        ).fetchall()
        events = []
        for event_id, payload, declared_digest in rows:
            if (
                self._ledger._integrity_digest("s16_events", event_id, payload)
                != declared_digest
            ):
                raise S16Unavailable("S16 ledger event integrity failed")
            events.append(json.loads(payload))
        scope_hint = ""
        for event in events:
            if (
                event.get("event_type") == "preflight"
                and event.get("request_id") == request_id
            ):
                scope_hint = str(event.get("scope_fingerprint") or "")
                break
        if scope_hint:
            # R3 (P1-6): expiry transitions are appended inside THIS
            # transaction; re-read the authoritative rows afterwards so the
            # generation and hold union reflect the expiry.
            self._expire_holds_in_transaction(connection, scope_hint)
            rows = connection.execute(
                "SELECT event_id, payload, integrity_sha256 FROM s16_events"
            ).fetchall()
            events = []
            for event_id, payload, declared_digest in rows:
                if (
                    self._ledger._integrity_digest(
                        "s16_events", event_id, payload
                    )
                    != declared_digest
                ):
                    raise S16Unavailable("S16 ledger event integrity failed")
                events.append(json.loads(payload))
        preflights = [
            event
            for event in events
            if event.get("event_type") == "preflight"
            and event.get("request_id") == request_id
        ]
        if not preflights:
            raise S16NotFound(request_id)
        preflight = preflights[0]
        scope_fingerprint = str(preflight.get("scope_fingerprint") or "")
        if not scope_fingerprint:
            raise S16Unavailable("S16 preflight scope is unavailable")
        active_holds = [
            event
            for event in events
            if event.get("event_type") == "legal_hold_imposed"
            and event.get("scope_fingerprint") == scope_fingerprint
        ]
        active_holds = [
            event
            for event in active_holds
            if not any(
                item.get("event_type") == "legal_hold_released"
                and item.get("hold_id") == event.get("hold_id")
                for item in events
            )
            and not (
                isinstance(event.get("expiry"), (int, float))
                and not isinstance(event.get("expiry"), bool)
                and int(event["expiry"]) <= self._now()
            )
        ]
        hold_generation = len(
            [
                event
                for event in events
                if event.get("event_type") in self._HOLD_TRANSITION_TYPES
                and event.get("scope_fingerprint") == scope_fingerprint
            ]
        )
        requests = [
            event
            for event in events
            if event.get("event_type") == "request"
            and event.get("request_id") == request_id
        ]
        return {
            "scope_fingerprint": scope_fingerprint,
            "manifest": self._restore_manifest(preflight, connection=connection),
            "hold_generation": hold_generation,
            "active_holds": active_holds,
            "requester": str((requests or [{}])[0].get("subject") or ""),
        }

    def _restore_manifest(
        self,
        preflight_event: dict[str, Any],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        scope_fingerprint = str(preflight_event.get("scope_fingerprint") or "")
        s01_owner = self._owners["s01"]
        if not s01_owner.scope_exists(scope_fingerprint):
            raise S16Blocked(S16_MANIFEST_STALE)
        entries: list[CopyInventoryEntry] = []
        for owner_id in sorted(self._owners):
            entries.extend(self._owners[owner_id].inventory(scope_fingerprint))
        entries = self._fill_retention_due(entries, scope_fingerprint)
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
        # R1: the commit re-checks the pinned governance facts against the
        # current authority inside the same transaction.
        if s16_owner_registry_digest() != str(
            preflight_event.get("owner_registry_digest") or ""
        ):
            raise S16Blocked(S16_OWNER_REGISTRY_STALE)
        if self._retention.digest() != str(
            preflight_event.get("policy_digest") or ""
        ):
            raise S16Blocked(S16_POLICY_STALE)
        if (
            self._retention.policy_id
            != str(preflight_event.get("policy_id") or "")
            or self._retention.policy_version
            != str(preflight_event.get("policy_version") or "")
        ):
            raise S16Blocked(S16_POLICY_STALE)
        hold_generation = self._hold_generation(scope_fingerprint)
        if hold_generation != int(preflight_event.get("hold_generation") or 0):
            raise S16Blocked(S16_HOLD_GENERATION_CHANGED)
        retained_scan = s01_owner.retained_scan(scope_fingerprint)
        if not retained_scan["clean"]:
            raise S16Blocked(S16_RETAINED_VALUE)
        if retained_scan["digest"] != str(
            preflight_event.get("retained_scan_digest") or ""
        ):
            raise S16Blocked(S16_MANIFEST_STALE)
        del connection
        return {
            "schema_version": S16_SCHEMA_VERSION,
            "scope_fingerprint": scope_fingerprint,
            "entries": entries,
            "entries_digest": entries_digest,
            "owner_registry_digest": s16_owner_registry_digest(),
            "policy_id": self._retention.policy_id,
            "policy_version": self._retention.policy_version,
            "policy_digest": self._retention.digest(),
            "hold_generation": hold_generation,
            "s01_revision": int(preflight_event.get("s01_revision") or 0),
            "s12_revision": str(preflight_event.get("s12_revision") or ""),
            "backup_revision": str(
                preflight_event.get("backup_revision") or ""
            ),
            "s17_revision": str(preflight_event.get("s17_revision") or ""),
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
        hold_generation: int,
        active_holds: list[dict[str, Any]],
    ) -> str | None:
        del subject
        if active_holds:
            return S16_ACTIVE_LEGAL_HOLD
        if int(manifest.get("hold_generation") or 0) != int(hold_generation):
            return S16_HOLD_GENERATION_CHANGED
        s01_owner = self._owners["s01"]
        s12_owner = self._owners["s12"]
        backup_owner = self._owners["backup"]
        s17_owner = self._owners["s17-disabled"]
        if int(s01_owner.store_revision()) != int(manifest["s01_revision"]):
            return S16_REVISION_CHANGED
        if str(s12_owner.store_revision()) != str(manifest["s12_revision"]):
            return S16_REVISION_CHANGED
        if str(backup_owner.store_revision()) != str(
            manifest.get("backup_revision") or ""
        ):
            return S16_REVISION_CHANGED
        if str(s17_owner.store_revision()) != str(
            manifest.get("s17_revision") or ""
        ):
            return S16_REVISION_CHANGED
        if not self.audit_available:
            return S16_AUDIT_UNAVAILABLE
        if not self.security_audit_available:
            return S16_AUDIT_SEAM_UNAVAILABLE
        if not self.storage_available:
            return S16_STORAGE_UNAVAILABLE
        if not s01_owner.owner_healthy():
            return S16_OWNER_INTEGRITY
        if not s12_owner.owner_healthy():
            return S16_OWNER_INTEGRITY
        if not backup_owner.owner_healthy():
            return S16_OWNER_INTEGRITY
        if not s17_owner.owner_healthy():
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
        if not s01_owner.is_terminated(scope_fingerprint):
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
        transaction with a lease/fence/attempt CAS (R1: no stale worker can
        overwrite newer state).
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
                        operation_id=job_id,
                        fence=int(job.get("fence") or 0),
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
            if getattr(self, "_debug_traceback", False):
                import traceback
                traceback.print_exc()
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
        """Claim at most one job with a database CAS (R2 P1-9): the
        conditional update on the original status/lease/fence/attempt is
        the cross-process arbitration; the process lock is not the lease."""
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
            claimed_fence = int(candidate.get("fence") or 0) + 1
            claimed_attempt = int(candidate.get("attempt") or 0) + 1
            claimed = {
                **candidate,
                "status": "running",
                "lease_owner": worker,
                "lease_expires_at": now + LEASE_SECONDS,
                "fence": claimed_fence,
                "attempt": claimed_attempt,
                "updated_at": now,
            }
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                claimed_ok = self._ledger._claim_job_cas(
                    connection,
                    job_id=job_id,
                    worker=worker,
                    now=now,
                    lease_seconds=LEASE_SECONDS,
                    claimed_payload=claimed,
                    claimed_fence=claimed_fence,
                    claimed_attempt=claimed_attempt,
                    expected_status=str(candidate.get("status") or ""),
                    expected_lease_expires_at=candidate.get("lease_expires_at"),
                    expected_fence=int(candidate.get("fence") or 0),
                    expected_attempt=int(candidate.get("attempt") or 0),
                )
                if not claimed_ok:
                    # Another worker already advanced the lease/fence:
                    # re-read and abandon the claim.
                    connection.execute("ROLLBACK")
                    return None
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
                published = self._ledger._cas_publish_job(
                    connection,
                    job_id,
                    expected_lease_owner=job.get("lease_owner"),
                    expected_fence=int(job.get("fence") or 0),
                    expected_attempt=int(job.get("attempt") or 0),
                    new_job=final_job,
                    now=now,
                )
                if not published:
                    connection.rollback()
                    self._record_stale_worker(job, now)
                    return {
                        "status": "stale",
                        "job_id": job_id,
                        "request_id": request_id,
                        "reason_code": S16_STALE_WORKER,
                    }
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
                published = self._ledger._cas_publish_job(
                    connection,
                    job_id,
                    expected_lease_owner=job.get("lease_owner"),
                    expected_fence=int(job.get("fence") or 0),
                    expected_attempt=int(job.get("attempt") or 0),
                    new_job=updated,
                    now=now,
                )
                if not published:
                    connection.rollback()
                    self._record_stale_worker(job, now)
                    return {
                        "status": "stale",
                        "job_id": job_id,
                        "request_id": str(job.get("request_id") or ""),
                        "reason_code": S16_STALE_WORKER,
                    }
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

    def _record_stale_worker(self, job: dict[str, Any], now: int) -> None:
        """Append the stable stale-worker fact without mutating the job."""
        with self._lock:
            event = {
                "event_type": "stale_worker",
                "event_id": self._event_id(
                    "stale_worker", str(job.get("job_id") or "")
                ),
                "request_id": str(job.get("request_id") or ""),
                "job_id": str(job.get("job_id") or ""),
                "scope_fingerprint": str(job.get("scope_fingerprint") or ""),
                "observed_fence": int(job.get("fence") or 0),
                "observed_attempt": int(job.get("attempt") or 0),
                "appended_at": now,
            }
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._ledger._append_event(connection, event, now)
                connection.commit()
            self._reload()

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
        if not self.audit_available:
            raise S16Blocked(S16_AUDIT_UNAVAILABLE)
        self._require_security_audit()
        binding_key = self._binding_key(principal, idempotency_key)
        command_fingerprint = _digest(
            {
                "action": "repair",
                "request_id": request_id,
                "owner_id": owner_id,
                "repair_fact": repair_fact,
                "subject": subject,
                "role": self.GOVERNANCE_ROLE,
                "scope": self._governance_scope,
                "source_id": self.GOVERNANCE_SOURCE_ID,
            }
        )
        with self._lock:
            self._reload()
            job = self._job_for_request(request_id)
            if job is None:
                raise S16NotFound(request_id)
            repair_result = {
                "status": "accepted",
                "request_id": request_id,
                "job_id": str(job["job_id"]),
            }
            with self._ledger._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    # R3 (P1-9): the idempotency binding is resolved BEFORE
                    # the current job state is judged, so a retry of the
                    # same key replays the original result even after the
                    # first repair moved the job to pending.
                    binding_outcome, stored_result = self._bind_or_replay(
                        connection,
                        binding_key=binding_key,
                        command_fingerprint=command_fingerprint,
                        result=repair_result,
                    )
                    if binding_outcome == "conflict":
                        connection.rollback()
                        raise S16Conflict(
                            "S16 idempotency conflict: same key different content"
                        )
                    if binding_outcome == "replayed":
                        connection.commit()
                        return {**stored_result, "replayed": True}
                    if job.get("status") != "repair_required":
                        connection.rollback()
                        raise S16Blocked(S16_REPAIR_REQUIRED)
                    owner = self._owners.get(owner_id)
                    if owner is None or not owner.verify_repair(owner_id, repair_fact):
                        connection.rollback()
                        raise S16Blocked(S16_REPAIR_NOT_VERIFIED)
                    updated = {
                        **job,
                        "status": "pending",
                        "stable_failure": None,
                        "updated_at": self._now(),
                    }
                    event = {
                        "event_type": "repair",
                        "event_id": self._event_id("repair", request_id),
                        "request_id": request_id,
                        "job_id": str(job["job_id"]),
                        "owner_id": owner_id,
                        "subject": subject,
                        "role": self.GOVERNANCE_ROLE,
                        "appended_at": self._now(),
                    }
                    self._ledger._append_event(connection, event, self._now())
                    self._ledger._upsert_job(connection, updated, self._now())
                    audit_fact = self._write_security_audit(
                        connection,
                        action="repair",
                        subject=subject,
                        role=self.GOVERNANCE_ROLE,
                        scope_fingerprint=str(
                            job.get("scope_fingerprint") or ""
                        ),
                        reason_code="deletion_repair",
                        request_id=request_id,
                        job_id=str(job["job_id"]),
                    )
                    connection.commit()
                except S16Conflict:
                    connection.rollback()
                    raise
                except Exception:
                    connection.rollback()
                    raise
            self._reload()
        self._replicate_security_audit(audit_fact)
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
                    "subject_fingerprint": application_id_fingerprint(
                        str(event.get("subject") or "")
                    ),
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
                    # R4 (P2-2): the query reports an explicit terminal
                    # state — active, released or expired — so the UI and
                    # commit's active union never disagree.  Expiry is
                    # judged from the ledger transition OR the clock; the
                    # state never maps expired back to released/active.
                    "state": (
                        "expired"
                        if any(
                            item.get("event_type") == "legal_hold_expired"
                            and item.get("hold_id") == event.get("hold_id")
                            for item in self._events
                        )
                        or (
                            isinstance(event.get("expiry"), (int, float))
                            and not isinstance(event.get("expiry"), bool)
                            and int(event.get("expiry")) <= self._now()
                        )
                        else (
                            "released"
                            if any(
                                item.get("event_type") == "legal_hold_released"
                                and item.get("hold_id")
                                == event.get("hold_id")
                                for item in self._events
                            )
                            else "active"
                        )
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
            # The original receipt row is immutable; the restore-replay
            # status derives from the append-only replay facts.
            replay_status = self._replay_status_for_job(str(job["job_id"]))
            return {**receipt, "restore_replay_status": replay_status}

    def _replay_status_for_job(self, job_id: str) -> str:
        replays = [
            replay
            for replay in self._replays
            if replay.get("job_id") == job_id
            and replay.get("result") == "verified"
        ]
        return "verified" if replays else "pending"

    # -- readiness / restore replay ----------------------------------------

    def _replay_operation_id(self, job_id: str, owner_id: str, scope_fingerprint: str) -> str:
        """Per-scope replay operation identity (R2 P1-4): bound to the
        completed job, the scope and the owner, so multiple restored scopes
        never share one owner binding."""
        return f"s16-replay:{job_id}:{scope_fingerprint[:16]}:{owner_id}"

    def _owner_copies_present(
        self, job: dict[str, Any], owner_id: str
    ) -> bool:
        """True when an owner still holds copies for a completed scope:
        absence verification failed, i.e. a restore window is open on this
        owner (R2 P0-1: every owner participates in readiness)."""
        fingerprints = (job.get("pending_owner_fingerprints") or {}).get(
            owner_id, []
        )
        if not fingerprints:
            return False
        try:
            self._owners[owner_id].verify_absent(
                fingerprints,
                scope_fingerprint=str(job.get("scope_fingerprint") or ""),
            )
            return False
        except S16OwnerFailure:
            return True

    def ready(self) -> bool:
        """Restore readiness (shared gate, R2 P0-1): the plane is closed
        exactly when ANY owner of a completed manifest still holds copies
        (an old backup or single-owner restore window).  After a normal
        completion every owner proves absence, so the receipt and query
        remain readable; after a restore the gate closes every restricted
        read until the runtime replay re-deletes and re-verifies each owner
        (startup replay covers the construction path)."""
        try:
            self._reload()
            if not self._events:
                return True
            for job in self._jobs.values():
                if job.get("status") != "complete":
                    continue
                for owner_id in EXECUTION_ORDER:
                    if owner_id == "s17-disabled":
                        continue
                    if self._owner_copies_present(job, owner_id):
                        return False
            return True
        except Exception:
            return False

    def _replay_job_owners(self, job: dict[str, Any]) -> int:
        """Replay every owner that still holds copies of a completed scope;
        returns the number of owners replayed.  The replay fact is appended
        only after EVERY owner verifies absence (R2 P1-4)."""
        job_id = str(job["job_id"])
        scope_fingerprint = str(job.get("scope_fingerprint") or "")
        fingerprints_by_owner = job.get("pending_owner_fingerprints") or {}
        pending_owners = [
            owner_id
            for owner_id in EXECUTION_ORDER
            if owner_id != "s17-disabled"
            and (fingerprints_by_owner.get(owner_id) or [])
            and self._owner_copies_present(job, owner_id)
        ]
        if not pending_owners:
            return 0
        for owner_id in pending_owners:
            self._owners[owner_id].replay(
                fingerprints_by_owner[owner_id],
                scope_fingerprint=scope_fingerprint,
                operation_id=self._replay_operation_id(
                    job_id, owner_id, scope_fingerprint
                ),
                fence=0,
            )
        for owner_id in pending_owners:
            self._owners[owner_id].verify_absent(
                fingerprints_by_owner[owner_id],
                scope_fingerprint=scope_fingerprint,
                operation_id=self._replay_operation_id(
                    job_id, owner_id, scope_fingerprint
                ),
                fence=0,
            )
        replay = {
            "replay_id": _stable_id("s16replay", f"{job_id}:{self._now()}"),
            "schema_version": "s16-restore-replay/1",
            "job_id": job_id,
            "request_id": str(job.get("request_id") or ""),
            "scope_fingerprint": scope_fingerprint,
            "result": "verified",
            "replayed_at": self._now(),
        }
        with self._ledger._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = {
                "event_type": "restore_replay",
                "event_id": self._event_id("restore_replay", job_id),
                "request_id": str(job.get("request_id") or ""),
                "job_id": job_id,
                "scope_fingerprint": scope_fingerprint,
                "result": "verified",
                "appended_at": self._now(),
            }
            self._ledger._append_event(connection, event, self._now())
            self._ledger._append_replay(connection, replay, self._now())
            connection.commit()
        return len(pending_owners)

    def replay_restore_if_needed(self) -> dict[str, Any]:
        """Idempotent runtime restore replay (R2 P0-1): re-deletes every
        completed scope whose OWNERS hold copies again (old backup or
        single-owner restore under a running process).  Returns the number
        of jobs replayed; the shared readiness gate stays closed until every
        owner of every such scope is verified."""
        with self._lock:
            self._reload()
            replayed = 0
            for job in self._jobs.values():
                if job.get("status") != "complete":
                    continue
                replayed += self._replay_job_owners(job)
            self._reload()
        return {"status": "replayed", "jobs": replayed}

    def _replay_restore(self) -> None:
        """Startup restore replay (R2 P0-1/P1-4): every completed manifest's
        owners are verified; owners that still hold copies are replayed with
        per-scope operations, and the append-only replay fact lands only
        after every owner verifies absence (ADR-0008)."""
        with self._lock:
            for job in self._jobs.values():
                if job.get("status") != "complete":
                    continue
                job_id = str(job["job_id"])
                if self._replay_status_for_job(job_id) == "verified":
                    continue
                self._replay_job_owners(job)
            self._reload()

    def _idempotency_binding(self, key: str) -> tuple[str, dict[str, Any]] | None:
        """Snapshot read (preflight replay only).  Command entry points use
        the database table inside their own transaction (R2 P1-10)."""
        for event in self._events:
            if (
                event.get("event_type") == "idempotency_binding"
                and event.get("binding_key") == key
            ):
                return str(event.get("fingerprint") or ""), event
        return None

    def _bind_or_replay(
        self,
        connection: sqlite3.Connection,
        *,
        binding_key: str,
        command_fingerprint: str,
        result: dict[str, Any],
    ) -> tuple[str, dict[str, Any] | None]:
        """Database-backed idempotency: insert-if-absent inside the command
        transaction and read back.  Same key + same fingerprint replays the
        stored result; same key + different content conflicts (R2 P1-10)."""
        existing = self._ledger._binding_read(connection, binding_key)
        if existing is not None:
            existing_fingerprint, existing_result = existing
            if existing_fingerprint == command_fingerprint:
                return "replayed", existing_result
            return "conflict", None
        self._ledger._binding_insert(
            connection,
            binding_key=binding_key,
            fingerprint=command_fingerprint,
            result=result,
            now=self._now(),
        )
        self._ledger._append_event(
            connection,
            {
                "event_type": "idempotency_binding",
                "binding_key": binding_key,
                "fingerprint": command_fingerprint,
                "result": result,
                "event_id": self._event_id("idempotency", binding_key),
                "appended_at": self._now(),
            },
            self._now(),
        )
        return "recorded", None

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

    def _reload(self) -> None:
        self._events = self._ledger._load_events()
        self._jobs = self._ledger._load_jobs()
        self._receipts = self._ledger._load_receipts()
        self._replays = self._ledger._load_replays()
