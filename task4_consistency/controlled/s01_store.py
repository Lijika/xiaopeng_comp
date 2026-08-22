"""Durable target-owned storage for the S01 controlled demonstration.

The public demo uses SQLite as the local durable adapter permitted by ADR-0008.
The domain service owns the state shape; this adapter owns transactions and
recovery across process instances.  The table set is intentionally private to
S01 and is not a legacy file or report authority.
"""

from __future__ import annotations

import copy
import contextlib
import hashlib
import hmac
import json
import sqlite3
from enum import Enum
from pathlib import Path
from typing import Any


_TABLES = (
    "applications",
    "receipts",
    "lifecycle_events",
    "evidence_events",
    "audit_events",
    "jobs",
    "idempotency",
    "attempts",
    "runs",
    "findings",
    "work_items",
    "review_records",
    "recovery_events",
    "inbox",
    "outbox",
    "projections",
    "sessions",
    "demo_sessions",
    "deletion_receipts",
    "policy_artifacts",
    "policy_manifests",
    "policy_governance_events",
    "policy_attempts",
    "policy_drafts",
    "policy_jobs",
    "policy_active_projections",
    "policy_schedule_reservations",
    "delivery_obligations",
    "delivery_attempts",
    "delivery_reconciliations",
    "delivery_compensations",
)

_INTEGRITY_SCHEMA = "s01-immutable-row/v1"
_IMMUTABLE_MAP_TABLES = frozenset({"receipts"})
_IMMUTABLE_LIST_IDS = {
    "lifecycle_events": "event_id",
    "evidence_events": "event_id",
    "audit_events": "event_id",
    "attempts": "attempt_id",
    "runs": "run_record_id",
    "findings": "finding_id",
    "work_items": "work_item_id",
    "review_records": "record_id",
    "recovery_events": "event_id",
    "inbox": "message_id",
    "demo_sessions": "demo_session_id",
    "deletion_receipts": "deletion_receipt_id",
    "policy_artifacts": "artifact_id",
    "policy_manifests": "manifest_id",
    "policy_governance_events": "event_id",
    "policy_attempts": "attempt_id",
    "delivery_obligations": "obligation_id",
    "delivery_attempts": "attempt_id",
    "delivery_reconciliations": "reconciliation_id",
    "delivery_compensations": "compensation_id",
}
_IMMUTABLE_TABLES = frozenset(
    {*_IMMUTABLE_MAP_TABLES, *_IMMUTABLE_LIST_IDS, "idempotency", "outbox"}
)
_MUTABLE_MAP_TABLES = frozenset(
    {
        "applications",
        "projections",
        "sessions",
        "policy_drafts",
        "policy_active_projections",
        "policy_schedule_reservations",
    }
)
_MUTABLE_LIST_IDS = {
    "jobs": "job_id",
    "outbox": "event_id",
    "policy_jobs": "policy_job_id",
}
_GOVERNED_DELETION_IDS = {
    "applications": "item_id",
    "receipts": "item_id",
    "lifecycle_events": "item_id",
    "evidence_events": "item_id",
    "audit_events": "item_id",
    "jobs": "item_id",
    "idempotency": "command_key",
    "attempts": "item_id",
    "runs": "item_id",
    "findings": "item_id",
    "work_items": "item_id",
    "review_records": "item_id",
    "recovery_events": "item_id",
    "inbox": "item_id",
    "outbox": "item_id",
    "projections": "item_id",
    "sessions": "item_id",
    "demo_sessions": "item_id",
}


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dict__"):
        return value.__dict__
    raise TypeError(f"unsupported S01 state value: {type(value).__name__}")


def _encode(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _integrity_digest(table: str, item_id: str, payload: str) -> str:
    material = "\0".join((_INTEGRITY_SCHEMA, table, item_id, payload)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _integrity_error(table: str, item_id: str, reason: str) -> RuntimeError:
    return RuntimeError(f"immutable S01 integrity: {table}/{item_id}: {reason}")


def _delivery_digest(item_id: str, status: str, revision: int) -> str:
    payload = _encode({"revision": revision, "status": status})
    return _integrity_digest("outbox_delivery", item_id, payload)


class StaleStoreRevision(RuntimeError):
    """A command attempted to publish state loaded before another commit."""


class ScheduleReservationConflict(RuntimeError):
    """One scope may hold only one pending schedule reservation."""


class AuditOutboxOwner:
    """Owns the ``audit_events`` and ``outbox`` collections of a store
    snapshot.

    Modules holding business state (S08 governance) never append to these
    collections directly: they submit immutable command records to this
    seam, and the owner applies them to the same SQLite snapshot so audit
    records, governance facts, idempotency results, projection updates and
    outbox messages commit or fail together in one short transaction.
    """

    def __init__(self, store: SQLiteTargetStore) -> None:
        self._store = store

    def append_audit(self, record: dict[str, Any]) -> dict[str, Any]:
        self._store.audit_events.append(record)
        return record

    def append_outbox(self, record: dict[str, Any]) -> dict[str, Any]:
        self._store.outbox.append(record)
        return record


class SQLiteTargetStore:
    """Copyable S01 state backed by immutable facts and mutable owners."""

    def __init__(self, state_path: str | Path) -> None:
        self.state_path = str(state_path)
        self.applications: dict[str, dict[str, Any]] = {}
        self.receipts: dict[str, Any] = {}
        self.lifecycle_events: list[dict[str, Any]] = []
        self.evidence_events: list[dict[str, Any]] = []
        self.audit_events: list[dict[str, Any]] = []
        self.jobs: list[dict[str, Any]] = []
        self.idempotency: dict[str, tuple[str, Any]] = {}
        self.attempts: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.findings: list[dict[str, Any]] = []
        self.work_items: list[dict[str, Any]] = []
        self.review_records: list[dict[str, Any]] = []
        self.recovery_events: list[dict[str, Any]] = []
        self.inbox: list[dict[str, Any]] = []
        self.outbox: list[dict[str, Any]] = []
        self.projections: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.demo_sessions: list[dict[str, Any]] = []
        self.deletion_receipts: list[dict[str, Any]] = []
        self.policy_artifacts: list[dict[str, Any]] = []
        self.policy_manifests: list[dict[str, Any]] = []
        self.policy_governance_events: list[dict[str, Any]] = []
        self.policy_attempts: list[dict[str, Any]] = []
        self.policy_drafts: dict[str, dict[str, Any]] = {}
        self.policy_jobs: list[dict[str, Any]] = []
        self.policy_active_projections: dict[str, dict[str, Any]] = {}
        self.policy_schedule_reservations: dict[str, dict[str, Any]] = {}
        self.delivery_obligations: list[dict[str, Any]] = []
        self.delivery_attempts: list[dict[str, Any]] = []
        self.delivery_reconciliations: list[dict[str, Any]] = []
        self.delivery_compensations: list[dict[str, Any]] = []
        self.projection_watermark = 0
        self.cohort_stop: dict[str, Any] | None = None
        self._store_revision = 0
        # Verified-row cache keyed by (table, item_id, payload,
        # integrity_sha256): reload/persist re-verify every immutable row on
        # every call, which dominates store I/O as the ledger grows.  A hit
        # re-parses the already-verified canonical payload into a fresh
        # object graph, so cache values are never shared with a live store
        # collection: mutating a loaded row cannot poison a later reload.
        # Any tamper (payload or seal change) misses the key and still runs
        # the full verification, so the integrity contract is unchanged --
        # only the repeated re-verification of unchanged rows is skipped.
        # Idempotency rows additionally bind the persisted fingerprint in
        # the cache identity: the seal covers {fingerprint, result}, so a
        # warm hit must never bypass that binding.
        self._integrity_cache: dict[tuple[str, ...], str] = {}
        self._ensure_schema()
        self.reload()

    def __deepcopy__(self, memo: dict[int, Any]) -> "SQLiteTargetStore":
        del memo
        cloned = object.__new__(type(self))
        cloned.state_path = self.state_path
        for name in (
            "applications",
            "receipts",
            "lifecycle_events",
            "evidence_events",
            "audit_events",
            "jobs",
            "idempotency",
            "attempts",
            "runs",
            "findings",
            "work_items",
            "review_records",
            "recovery_events",
            "inbox",
            "outbox",
            "projections",
            "sessions",
            "demo_sessions",
            "deletion_receipts",
            "policy_artifacts",
            "policy_manifests",
            "policy_governance_events",
            "policy_attempts",
            "policy_drafts",
            "policy_jobs",
            "policy_active_projections",
            "policy_schedule_reservations",
            "delivery_obligations",
            "delivery_attempts",
            "delivery_reconciliations",
            "delivery_compensations",
            "cohort_stop",
        ):
            setattr(cloned, name, copy.deepcopy(getattr(self, name)))
        cloned.projection_watermark = self.projection_watermark
        cloned._store_revision = self._store_revision
        cloned._integrity_cache = dict(self._integrity_cache)
        return cloned

    def _connect(self) -> sqlite3.Connection:
        path = Path(self.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.state_path, timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextlib.contextmanager
    def revision_fence(self, expected_revision: int):
        """Hold the store revision stable across an external publication."""
        if isinstance(expected_revision, bool) or not isinstance(
            expected_revision, int
        ):
            raise ValueError("authority revision must be an integer")
        with contextlib.closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT store_revision FROM s01_meta WHERE id = 1"
                ).fetchone()
            except sqlite3.Error as error:
                raise RuntimeError("authority revision fence is unavailable") from error
            current_revision = int(row[0]) if row else 0
            if current_revision != expected_revision:
                connection.execute("ROLLBACK")
                raise StaleStoreRevision(
                    "authority revision advanced before S12 publication: "
                    f"expected {expected_revision}, found {current_revision}"
                )
            try:
                yield
            finally:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS s01_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    projection_watermark INTEGER NOT NULL DEFAULT 0,
                    cohort_stop TEXT,
                    store_revision INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO s01_meta(id) VALUES (1)"
            )
            self._ensure_column(
                connection,
                "s01_meta",
                "store_revision",
                "INTEGER NOT NULL DEFAULT 0",
            )
            for table in _TABLES:
                if table == "idempotency":
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS idempotency ("
                        "command_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                        "payload TEXT NOT NULL, integrity_sha256 TEXT)"
                    )
                    self._ensure_column(connection, table, "integrity_sha256", "TEXT")
                    continue
                immutable_payload = (
                    table in _IMMUTABLE_MAP_TABLES
                    or table in _IMMUTABLE_LIST_IDS
                    or table == "outbox"
                )
                integrity_column = (
                    ", integrity_sha256 TEXT"
                    if immutable_payload
                    else ""
                )
                delivery_column = (
                    ", delivery_status TEXT, delivery_revision INTEGER, "
                    "delivery_integrity_sha256 TEXT"
                    if table == "outbox"
                    else ""
                )
                connection.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    f"item_id TEXT PRIMARY KEY, payload TEXT NOT NULL"
                    f"{integrity_column}{delivery_column})"
                )
                if immutable_payload:
                    self._ensure_column(connection, table, "integrity_sha256", "TEXT")
                if table == "outbox":
                    self._ensure_column(connection, table, "delivery_status", "TEXT")
                    self._ensure_column(connection, table, "delivery_revision", "INTEGER")
                    self._ensure_column(
                        connection, table, "delivery_integrity_sha256", "TEXT"
                    )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS s01_immutable_catalog ("
                "table_name TEXT NOT NULL, item_id TEXT NOT NULL, "
                "integrity_sha256 TEXT NOT NULL, PRIMARY KEY(table_name, item_id))"
            )
            self._ensure_column(
                connection, "policy_schedule_reservations", "scope_key", "TEXT"
            )
            self._ensure_column(
                connection, "policy_schedule_reservations", "status", "TEXT"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS policy_schedule_reservations_one_pending "
                "ON policy_schedule_reservations(scope_key) WHERE status = 'pending'"
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, declaration: str
    ) -> None:
        columns = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @staticmethod
    def _rows(connection: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
        return connection.execute(
            f"SELECT item_id, payload FROM {table} ORDER BY rowid"
        ).fetchall()

    @staticmethod
    def _decode_immutable(
        table: str,
        item_id: str,
        payload: str,
        integrity_sha256: str | None,
        cache: dict[tuple[str, ...], Any] | None = None,
    ) -> Any:
        if integrity_sha256 is None:
            raise _integrity_error(table, item_id, "missing seal")
        if cache is not None:
            key = (table, item_id, payload, integrity_sha256)
            cached = cache.get(key)
            if cached is not None:
                # The cached value is the verified canonical payload; parsing
                # it again yields a fresh object graph that shares nothing
                # with any previously loaded collection.
                return json.loads(cached)
        try:
            value = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as error:
            raise _integrity_error(table, item_id, "invalid payload") from error
        if _encode(value) != payload:
            raise _integrity_error(table, item_id, "non-canonical payload")
        expected = _integrity_digest(table, item_id, payload)
        if not hmac.compare_digest(integrity_sha256, expected):
            raise _integrity_error(table, item_id, "seal mismatch")
        if cache is not None:
            cache[(table, item_id, payload, integrity_sha256)] = payload
        return value

    @classmethod
    def _immutable_rows(
        cls,
        connection: sqlite3.Connection,
        table: str,
        cache: dict[tuple[str, ...], Any] | None = None,
    ) -> list[tuple[str, Any]]:
        rows = connection.execute(
            f"SELECT item_id, payload, integrity_sha256 FROM {table} ORDER BY rowid"
        ).fetchall()
        return [
            (
                item_id,
                cls._decode_immutable(table, item_id, payload, digest, cache),
            )
            for item_id, payload, digest in rows
        ]

    @classmethod
    def _idempotency_rows(
        cls,
        connection: sqlite3.Connection,
        cache: dict[tuple[str, ...], Any] | None = None,
    ) -> list[tuple[str, str, Any]]:
        rows = connection.execute(
            "SELECT command_key, fingerprint, payload, integrity_sha256 "
            "FROM idempotency ORDER BY rowid"
        ).fetchall()
        values = []
        for key, fingerprint, payload, digest in rows:
            if cache is not None:
                # The persisted fingerprint is part of the binding the seal
                # covers ({fingerprint, result}); a warm hit must therefore
                # key on it too, or a fingerprint tamper after warm-up would
                # bypass verification exactly like a fresh owner rejects it.
                cache_key = ("idempotency", key, fingerprint, payload, digest)
                cached = cache.get(cache_key)
                if cached is not None:
                    values.append((key, fingerprint, json.loads(cached)))
                    continue
            try:
                result = json.loads(payload)
            except (TypeError, json.JSONDecodeError) as error:
                raise _integrity_error("idempotency", key, "invalid payload") from error
            if _encode(result) != payload:
                raise _integrity_error("idempotency", key, "non-canonical payload")
            binding = _encode({"fingerprint": fingerprint, "result": result})
            expected = _integrity_digest("idempotency", key, binding)
            if not digest:
                raise _integrity_error("idempotency", key, "missing seal")
            if not hmac.compare_digest(digest, expected):
                raise _integrity_error("idempotency", key, "seal mismatch")
            if cache is not None:
                cache[("idempotency", key, fingerprint, payload, digest)] = payload
            values.append((key, fingerprint, result))
        return values

    @classmethod
    def _outbox_rows(
        cls,
        connection: sqlite3.Connection,
        cache: dict[tuple[str, ...], Any] | None = None,
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT item_id, payload, integrity_sha256, delivery_status, "
            "delivery_revision, delivery_integrity_sha256 "
            "FROM outbox ORDER BY rowid"
        ).fetchall()
        values = []
        for item_id, payload, digest, delivery_status, delivery_revision, delivery_digest in rows:
            event = cls._decode_immutable(
                "outbox", item_id, payload, digest, cache
            )
            if event.get("event_id") != item_id or "status" in event:
                raise _integrity_error("outbox", item_id, "invalid event body")
            cls._verify_delivery(
                item_id, delivery_status, delivery_revision, delivery_digest
            )
            values.append({**event, "status": delivery_status})
        return values

    def reload(self) -> None:
        """Reload authoritative facts before each public command/query."""
        with self._connect() as connection:
            connection.execute("BEGIN")
            self._verify_immutable_catalog(connection)
            meta = connection.execute(
                "SELECT projection_watermark, cohort_stop, store_revision "
                "FROM s01_meta WHERE id=1"
            ).fetchone()
            self.projection_watermark = int(meta[0]) if meta else 0
            self.cohort_stop = json.loads(meta[1]) if meta and meta[1] else None
            self._store_revision = int(meta[2]) if meta else 0

            for table in _TABLES:
                if table == "idempotency":
                    self.idempotency = {
                        key: (fingerprint, payload)
                        for key, fingerprint, payload in self._idempotency_rows(
                            connection, self._integrity_cache
                        )
                    }
                    continue
                if table == "outbox":
                    self.outbox = self._outbox_rows(
                        connection, self._integrity_cache
                    )
                    continue
                values = (
                    self._immutable_rows(connection, table, self._integrity_cache)
                    if table in _IMMUTABLE_MAP_TABLES or table in _IMMUTABLE_LIST_IDS
                    else self._rows(connection, table)
                )
                if table == "applications":
                    self.applications = {key: json.loads(payload) for key, payload in values}
                elif table == "receipts":
                    self.receipts = {key: payload for key, payload in values}
                elif table == "lifecycle_events":
                    self.lifecycle_events = [payload for _, payload in values]
                elif table == "evidence_events":
                    self.evidence_events = [payload for _, payload in values]
                elif table == "audit_events":
                    self.audit_events = [payload for _, payload in values]
                elif table == "jobs":
                    self.jobs = [json.loads(payload) for _, payload in values]
                elif table == "attempts":
                    self.attempts = [payload for _, payload in values]
                elif table == "runs":
                    self.runs = [payload for _, payload in values]
                elif table == "findings":
                    self.findings = [payload for _, payload in values]
                elif table == "work_items":
                    self.work_items = [payload for _, payload in values]
                elif table == "review_records":
                    self.review_records = [payload for _, payload in values]
                elif table == "recovery_events":
                    self.recovery_events = [payload for _, payload in values]
                elif table == "inbox":
                    self.inbox = [payload for _, payload in values]
                elif table == "projections":
                    self.projections = {
                        key: json.loads(payload) for key, payload in values
                    }
                elif table == "sessions":
                    self.sessions = {key: json.loads(payload) for key, payload in values}
                elif table == "demo_sessions":
                    self.demo_sessions = [payload for _, payload in values]
                elif table == "deletion_receipts":
                    self.deletion_receipts = [payload for _, payload in values]
                elif table == "policy_artifacts":
                    self.policy_artifacts = [payload for _, payload in values]
                elif table == "policy_manifests":
                    self.policy_manifests = [payload for _, payload in values]
                elif table == "policy_governance_events":
                    self.policy_governance_events = [payload for _, payload in values]
                elif table == "policy_attempts":
                    self.policy_attempts = [payload for _, payload in values]
                elif table == "policy_drafts":
                    self.policy_drafts = {
                        key: json.loads(payload) for key, payload in values
                    }
                elif table == "policy_jobs":
                    self.policy_jobs = [json.loads(payload) for _, payload in values]
                elif table == "policy_active_projections":
                    self.policy_active_projections = {
                        key: json.loads(payload) for key, payload in values
                    }
                elif table == "policy_schedule_reservations":
                    self.policy_schedule_reservations = {
                        key: json.loads(payload) for key, payload in values
                    }
                elif table == "delivery_obligations":
                    self.delivery_obligations = [payload for _, payload in values]
                elif table == "delivery_attempts":
                    self.delivery_attempts = [payload for _, payload in values]
                elif table == "delivery_reconciliations":
                    self.delivery_reconciliations = [payload for _, payload in values]
                elif table == "delivery_compensations":
                    self.delivery_compensations = [payload for _, payload in values]

    def persist(self) -> None:
        """Append facts and publish mutable owners in one transaction."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._verify_immutable_catalog(connection)
                persisted_revision = connection.execute(
                    "SELECT store_revision FROM s01_meta WHERE id = 1"
                ).fetchone()
                current_revision = int(persisted_revision[0]) if persisted_revision else 0
                if current_revision != self._store_revision:
                    raise StaleStoreRevision(
                        "stale S01 store revision: "
                        f"expected {self._store_revision}, found {current_revision}"
                    )
                next_revision = current_revision + 1
                updated = connection.execute(
                    "UPDATE s01_meta SET projection_watermark = ?, cohort_stop = ?, "
                    "store_revision = ? WHERE id = 1 AND store_revision = ?",
                    (
                        self.projection_watermark,
                        _encode(self.cohort_stop) if self.cohort_stop is not None else None,
                        next_revision,
                        current_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise StaleStoreRevision(
                        f"stale S01 store revision: expected {current_revision}"
                    )

                self._upsert_map(connection, "applications", self.applications)
                self._sync_immutable_map(
                    connection, "receipts", self.receipts, self._integrity_cache
                )
                self._sync_immutable_list(
                    connection,
                    "lifecycle_events",
                    self.lifecycle_events,
                    self._integrity_cache,
                )
                self._sync_immutable_list(
                    connection,
                    "evidence_events",
                    self.evidence_events,
                    self._integrity_cache,
                )
                self._sync_immutable_list(
                    connection,
                    "audit_events",
                    self.audit_events,
                    self._integrity_cache,
                )
                self._upsert_list(connection, "jobs", self.jobs, "job_id")
                self._sync_idempotency(connection)
                self._sync_immutable_list(
                    connection, "attempts", self.attempts, self._integrity_cache
                )
                self._sync_immutable_list(
                    connection, "runs", self.runs, self._integrity_cache
                )
                self._sync_immutable_list(
                    connection, "findings", self.findings, self._integrity_cache
                )
                self._sync_immutable_list(
                    connection, "work_items", self.work_items, self._integrity_cache
                )
                self._sync_immutable_list(
                    connection,
                    "review_records",
                    self.review_records,
                    self._integrity_cache,
                )
                self._sync_immutable_list(
                    connection,
                    "recovery_events",
                    self.recovery_events,
                    self._integrity_cache,
                )
                self._sync_immutable_list(
                    connection, "inbox", self.inbox, self._integrity_cache
                )
                self._sync_outbox(connection)
                self._sync_mutable_map(connection, "projections", self.projections)
                self._sync_mutable_map(connection, "sessions", self.sessions)
                self._sync_immutable_list(
                    connection,
                    "demo_sessions",
                    self.demo_sessions,
                    self._integrity_cache,
                )
                self._sync_immutable_list(
                    connection,
                    "deletion_receipts",
                    self.deletion_receipts,
                    self._integrity_cache,
                )
                self._sync_immutable_list(
                    connection,
                    "policy_artifacts",
                    self.policy_artifacts,
                    self._integrity_cache,
                )
                self._sync_immutable_list(
                    connection,
                    "policy_manifests",
                    self.policy_manifests,
                    self._integrity_cache,
                )
                self._sync_immutable_list(
                    connection,
                    "policy_governance_events",
                    self.policy_governance_events,
                    self._integrity_cache,
                )
                self._sync_immutable_list(
                    connection,
                    "policy_attempts",
                    self.policy_attempts,
                    self._integrity_cache,
                )
                self._sync_mutable_map(connection, "policy_drafts", self.policy_drafts)
                self._upsert_list(connection, "policy_jobs", self.policy_jobs, "policy_job_id")
                self._sync_mutable_map(
                    connection, "policy_active_projections",
                    self.policy_active_projections,
                )
                self._sync_policy_schedule_reservations(connection)
                self._sync_immutable_list(
                    connection,
                    "delivery_obligations",
                    self.delivery_obligations,
                    self._integrity_cache,
                )
                self._sync_immutable_list(
                    connection,
                    "delivery_attempts",
                    self.delivery_attempts,
                    self._integrity_cache,
                )
                self._sync_immutable_list(
                    connection,
                    "delivery_reconciliations",
                    self.delivery_reconciliations,
                    self._integrity_cache,
                )
                self._sync_immutable_list(
                    connection,
                    "delivery_compensations",
                    self.delivery_compensations,
                    self._integrity_cache,
                )
                connection.commit()
                self._store_revision = next_revision
            except Exception:
                connection.rollback()
                raise

    def governed_delete(
        self,
        plan: dict[str, set[str]],
        receipt: dict[str, Any],
    ) -> None:
        """Delete one expired public-demo aggregate and seal its receipt."""
        receipt_id = str(receipt.get("deletion_receipt_id") or "")
        if not receipt_id:
            raise ValueError("governed deletion requires a receipt identity")
        if set(plan).difference(_GOVERNED_DELETION_IDS):
            raise ValueError("governed deletion plan contains an unsupported table")
        normalized = {
            table: {str(item_id) for item_id in item_ids if str(item_id)}
            for table, item_ids in plan.items()
        }
        if receipt.get("deleted_counts") != {
            table: len(item_ids) for table, item_ids in sorted(normalized.items())
        }:
            raise ValueError("governed deletion receipt counts do not match its plan")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._verify_immutable_catalog(connection)
                persisted_revision = connection.execute(
                    "SELECT store_revision FROM s01_meta WHERE id = 1"
                ).fetchone()
                current_revision = int(persisted_revision[0]) if persisted_revision else 0
                if current_revision != self._store_revision:
                    raise StaleStoreRevision(
                        "stale S01 store revision: "
                        f"expected {self._store_revision}, found {current_revision}"
                    )
                for table, item_ids in normalized.items():
                    if not item_ids:
                        continue
                    id_column = _GOVERNED_DELETION_IDS[table]
                    persisted_ids = {
                        str(row[0])
                        for row in connection.execute(
                            f"SELECT {id_column} FROM {table}"
                        ).fetchall()
                    }
                    if not item_ids.issubset(persisted_ids):
                        raise RuntimeError(
                            f"governed deletion authority changed for {table}"
                        )
                    connection.executemany(
                        f"DELETE FROM {table} WHERE {id_column} = ?",
                        ((item_id,) for item_id in sorted(item_ids)),
                    )
                    if table in _IMMUTABLE_TABLES:
                        connection.executemany(
                            "DELETE FROM s01_immutable_catalog "
                            "WHERE table_name = ? AND item_id = ?",
                            ((table, item_id) for item_id in sorted(item_ids)),
                        )

                payload = _encode(receipt)
                digest = _integrity_digest("deletion_receipts", receipt_id, payload)
                connection.execute(
                    "INSERT INTO deletion_receipts(item_id, payload, integrity_sha256) "
                    "VALUES (?, ?, ?)",
                    (receipt_id, payload, digest),
                )
                connection.execute(
                    "INSERT INTO s01_immutable_catalog(table_name, item_id, integrity_sha256) "
                    "VALUES ('deletion_receipts', ?, ?)",
                    (receipt_id, digest),
                )
                next_revision = current_revision + 1
                updated = connection.execute(
                    "UPDATE s01_meta SET projection_watermark = ?, cohort_stop = ?, "
                    "store_revision = ? WHERE id = 1 AND store_revision = ?",
                    (
                        self.projection_watermark,
                        _encode(self.cohort_stop)
                        if self.cohort_stop is not None
                        else None,
                        next_revision,
                        current_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise StaleStoreRevision(
                        f"stale S01 store revision: expected {current_revision}"
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        self.reload()

    @staticmethod
    def _upsert_map(
        connection: sqlite3.Connection, table: str, values: dict[str, Any]
    ) -> None:
        connection.executemany(
            f"INSERT INTO {table}(item_id, payload) VALUES (?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET payload = excluded.payload",
            ((key, _encode(value)) for key, value in values.items()),
        )

    @classmethod
    def _sync_mutable_map(
        cls,
        connection: sqlite3.Connection,
        table: str,
        values: dict[str, Any],
    ) -> None:
        if table not in _MUTABLE_MAP_TABLES:
            raise ValueError(f"{table} is not a mutable map table")
        persisted_ids = {
            str(row[0]) for row in connection.execute(f"SELECT item_id FROM {table}")
        }
        removed_ids = persisted_ids.difference(values)
        connection.executemany(
            f"DELETE FROM {table} WHERE item_id = ?",
            ((item_id,) for item_id in sorted(removed_ids)),
        )
        cls._upsert_map(connection, table, values)

    @staticmethod
    def _upsert_list(
        connection: sqlite3.Connection,
        table: str,
        values: list[dict[str, Any]],
        id_field: str,
    ) -> None:
        rows: dict[str, str] = {}
        for value in values:
            item_id = str(value.get(id_field) or "")
            if not item_id:
                raise ValueError(f"{table} requires stable {id_field}")
            payload = _encode(value)
            if item_id in rows and rows[item_id] != payload:
                raise ValueError(f"{table} has conflicting {id_field}: {item_id}")
            rows[item_id] = payload
        connection.executemany(
            f"INSERT INTO {table}(item_id, payload) VALUES (?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET payload = excluded.payload",
            rows.items(),
        )

    @classmethod
    def _sync_immutable_map(
        cls,
        connection: sqlite3.Connection,
        table: str,
        values: dict[str, Any],
        cache: dict[tuple[str, str, str, str], Any] | None = None,
    ) -> None:
        cls._sync_immutable_rows(
            connection,
            table,
            {str(item_id): _encode(value) for item_id, value in values.items()},
            cache,
        )

    @classmethod
    def _sync_immutable_list(
        cls,
        connection: sqlite3.Connection,
        table: str,
        values: list[dict[str, Any]],
        cache: dict[tuple[str, str, str, str], Any] | None = None,
    ) -> None:
        id_field = _IMMUTABLE_LIST_IDS[table]
        rows: dict[str, str] = {}
        for value in values:
            item_id = str(value.get(id_field) or "")
            if not item_id:
                raise ValueError(f"{table} requires stable {id_field}")
            payload = _encode(value)
            previous = rows.get(item_id)
            if previous is not None and previous != payload:
                raise _integrity_error(table, item_id, "conflicting staged payload")
            rows[item_id] = payload
        cls._sync_immutable_rows(connection, table, rows, cache)

    @classmethod
    def _sync_immutable_rows(
        cls,
        connection: sqlite3.Connection,
        table: str,
        staged: dict[str, str],
        cache: dict[tuple[str, str, str, str], Any] | None = None,
    ) -> None:
        persisted = {
            item_id: (payload, digest)
            for item_id, payload, digest in connection.execute(
                f"SELECT item_id, payload, integrity_sha256 FROM {table}"
            ).fetchall()
        }
        missing = persisted.keys() - staged.keys()
        if missing:
            item_id = sorted(missing)[0]
            raise _integrity_error(table, item_id, "history omitted from staged state")
        for item_id, payload in staged.items():
            existing = persisted.get(item_id)
            if existing is not None:
                existing_payload, digest = existing
                cls._decode_immutable(
                    table, item_id, existing_payload, digest, cache
                )
                if existing_payload != payload:
                    raise _integrity_error(table, item_id, "immutable payload changed")
                continue
            digest = _integrity_digest(table, item_id, payload)
            connection.execute(
                f"INSERT INTO {table}(item_id, payload, integrity_sha256) "
                "VALUES (?, ?, ?)",
                (item_id, payload, digest),
            )
            connection.execute(
                "INSERT INTO s01_immutable_catalog(table_name, item_id, integrity_sha256) "
                "VALUES (?, ?, ?)",
                (table, item_id, digest),
            )

    def _sync_idempotency(self, connection: sqlite3.Connection) -> None:
        staged: dict[str, tuple[str, str, str]] = {}
        for key, (fingerprint, result) in self.idempotency.items():
            payload = _encode(result)
            binding = _encode(
                {"fingerprint": fingerprint, "result": json.loads(payload)}
            )
            staged[key] = (
                fingerprint,
                payload,
                _integrity_digest("idempotency", key, binding),
            )
        persisted = {
            key: (fingerprint, payload, digest)
            for key, fingerprint, payload, digest in connection.execute(
                "SELECT command_key, fingerprint, payload, integrity_sha256 FROM idempotency"
            ).fetchall()
        }
        missing = persisted.keys() - staged.keys()
        if missing:
            key = sorted(missing)[0]
            raise _integrity_error("idempotency", key, "binding omitted from staged state")
        for key, row in staged.items():
            existing = persisted.get(key)
            if existing is not None:
                fingerprint, payload, digest = existing
                result = self._decode_idempotency(key, fingerprint, payload, digest)
                existing_binding = (fingerprint, _encode(result), digest)
                if existing_binding != row:
                    raise _integrity_error("idempotency", key, "immutable binding changed")
                continue
            connection.execute(
                "INSERT INTO idempotency(command_key, fingerprint, payload, integrity_sha256) "
                "VALUES (?, ?, ?, ?)",
                (key, *row),
            )
            connection.execute(
                "INSERT INTO s01_immutable_catalog(table_name, item_id, integrity_sha256) "
                "VALUES (?, ?, ?)",
                ("idempotency", key, row[2]),
            )

    def _sync_outbox(self, connection: sqlite3.Connection) -> None:
        staged: dict[str, tuple[str, str]] = {}
        for event in self.outbox:
            item_id = str(event.get("event_id") or "")
            status = str(event.get("status") or "")
            if not item_id:
                raise ValueError("outbox requires stable event_id")
            if status not in {"pending", "published"}:
                raise ValueError(f"outbox has invalid delivery status: {status}")
            body = {key: value for key, value in event.items() if key != "status"}
            payload = _encode(body)
            previous = staged.get(item_id)
            if previous is not None and previous != (payload, status):
                raise _integrity_error("outbox", item_id, "conflicting staged event")
            staged[item_id] = (payload, status)

        persisted = {
            item_id: (payload, digest, status, revision, delivery_digest)
            for item_id, payload, digest, status, revision, delivery_digest in connection.execute(
                "SELECT item_id, payload, integrity_sha256, delivery_status, "
                "delivery_revision, delivery_integrity_sha256 FROM outbox"
            ).fetchall()
        }
        missing = persisted.keys() - staged.keys()
        if missing:
            item_id = sorted(missing)[0]
            raise _integrity_error("outbox", item_id, "event omitted from staged state")

        for item_id, (payload, status) in staged.items():
            existing = persisted.get(item_id)
            if existing is None:
                if status != "pending":
                    raise _integrity_error(
                        "outbox", item_id, "new event is not pending"
                    )
                digest = _integrity_digest("outbox", item_id, payload)
                delivery_revision = 1
                delivery_digest = _delivery_digest(
                    item_id, status, delivery_revision
                )
                connection.execute(
                    "INSERT INTO outbox("
                    "item_id, payload, integrity_sha256, delivery_status, "
                    "delivery_revision, delivery_integrity_sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        item_id,
                        payload,
                        digest,
                        status,
                        delivery_revision,
                        delivery_digest,
                    ),
                )
                connection.execute(
                    "INSERT INTO s01_immutable_catalog(table_name, item_id, integrity_sha256) "
                    "VALUES (?, ?, ?)",
                    ("outbox", item_id, digest),
                )
                continue

            (
                existing_payload,
                digest,
                existing_status,
                delivery_revision,
                delivery_digest,
            ) = existing
            self._decode_immutable(
                "outbox", item_id, existing_payload, digest, self._integrity_cache
            )
            self._verify_delivery(
                item_id, existing_status, delivery_revision, delivery_digest
            )
            if existing_payload != payload:
                raise _integrity_error("outbox", item_id, "immutable event body changed")
            if existing_status == status:
                continue
            is_s13_delivery = False
            try:
                is_s13_delivery = (
                    json.loads(existing_payload).get("kind") == "delivery_requested"
                )
            except (TypeError, ValueError):
                is_s13_delivery = False
            if (
                existing_status != "pending" or status != "published"
            ) and not (
                is_s13_delivery
                and existing_status == "published"
                and status == "pending"
            ):
                raise _integrity_error("outbox", item_id, "delivery status regression")
            next_revision = delivery_revision + 1
            connection.execute(
                "UPDATE outbox SET delivery_status = ?, delivery_revision = ?, "
                "delivery_integrity_sha256 = ? WHERE item_id = ?",
                (
                    status,
                    next_revision,
                    _delivery_digest(item_id, status, next_revision),
                    item_id,
                ),
            )

    @staticmethod
    def _verify_delivery(
        item_id: str,
        status: Any,
        revision: Any,
        digest: Any,
    ) -> None:
        if status not in {"pending", "published"}:
            raise _integrity_error("outbox", item_id, "invalid delivery status")
        if not isinstance(revision, int) or revision < 1:
            raise _integrity_error("outbox", item_id, "invalid delivery revision")
        expected = _delivery_digest(item_id, status, revision)
        if not isinstance(digest, str) or not hmac.compare_digest(digest, expected):
            raise _integrity_error("outbox", item_id, "delivery seal mismatch")

    def _sync_policy_schedule_reservations(
        self, connection: sqlite3.Connection
    ) -> None:
        """Publish policy schedule reservations with the one-pending unique
        scope constraint enforced mechanically by a partial unique index."""
        staged: dict[str, tuple[str, str, str]] = {}
        for reservation_id, value in self.policy_schedule_reservations.items():
            scope_key = str(value.get("scope") or "")
            status = str(value.get("status") or "")
            if not scope_key or status not in {"pending", "completed", "cancelled"}:
                raise ValueError(
                    "policy schedule reservation requires scope and valid status"
                )
            staged[str(reservation_id)] = (scope_key, status, _encode(value))
        persisted_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT item_id FROM policy_schedule_reservations"
            )
        }
        removed_ids = persisted_ids.difference(staged)
        connection.executemany(
            "DELETE FROM policy_schedule_reservations WHERE item_id = ?",
            ((item_id,) for item_id in sorted(removed_ids)),
        )
        for reservation_id, (scope_key, status, payload) in staged.items():
            try:
                connection.execute(
                    "INSERT INTO policy_schedule_reservations("
                    "item_id, scope_key, status, payload) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(item_id) DO UPDATE SET "
                    "scope_key = excluded.scope_key, status = excluded.status, "
                    "payload = excluded.payload",
                    (reservation_id, scope_key, status, payload),
                )
            except sqlite3.IntegrityError as error:
                raise ScheduleReservationConflict(
                    f"overlapping pending schedule reservation for {scope_key}"
                ) from error

    @staticmethod
    def _decode_idempotency(
        key: str, fingerprint: str, payload: str, digest: str | None
    ) -> Any:
        try:
            result = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as error:
            raise _integrity_error("idempotency", key, "invalid payload") from error
        if _encode(result) != payload:
            raise _integrity_error("idempotency", key, "non-canonical payload")
        binding = _encode({"fingerprint": fingerprint, "result": result})
        expected = _integrity_digest("idempotency", key, binding)
        if not digest:
            raise _integrity_error("idempotency", key, "missing seal")
        if not hmac.compare_digest(digest, expected):
            raise _integrity_error("idempotency", key, "seal mismatch")
        return result

    @classmethod
    def _verify_immutable_catalog(cls, connection: sqlite3.Connection) -> None:
        catalog: dict[str, dict[str, str]] = {table: {} for table in _IMMUTABLE_TABLES}
        for table, item_id, digest in connection.execute(
            "SELECT table_name, item_id, integrity_sha256 FROM s01_immutable_catalog"
        ).fetchall():
            if table not in catalog:
                raise _integrity_error(str(table), str(item_id), "unknown catalog table")
            catalog[table][item_id] = digest

        for table in sorted(_IMMUTABLE_TABLES):
            id_column = "command_key" if table == "idempotency" else "item_id"
            persisted = {
                item_id: digest
                for item_id, digest in connection.execute(
                    f"SELECT {id_column}, integrity_sha256 FROM {table}"
                ).fetchall()
            }
            persisted_ids = set(persisted)
            catalog_ids = set(catalog[table])
            if persisted_ids != catalog_ids:
                item_id = sorted(persisted_ids ^ catalog_ids)[0]
                reason = (
                    "fact missing from catalog"
                    if item_id in persisted_ids
                    else "cataloged fact missing"
                )
                raise _integrity_error(table, item_id, reason)
            for item_id, digest in persisted.items():
                catalog_digest = catalog[table][item_id]
                if not digest or not hmac.compare_digest(digest, catalog_digest):
                    raise _integrity_error(table, item_id, "catalog seal mismatch")
