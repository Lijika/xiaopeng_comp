"""S12 isolated and honest formal evaluation plane (Ticket #28).

The evaluation plane owns its own SQLite database (plans, jobs, attempts,
predictions, bundles) and never attaches to or writes through the S01
business database.  A typed plan freezes cohorts, clusters, splits,
opportunities, independent gold labels, evidence snapshots, release/
checker/build identities, seed, budget and stop rule; a restricted
``s12_runner`` subprocess executes the existing pure ``TargetChecker.run``
over the runner projection (which carries no gold); the parent materializes
explicit ``missing``/``error`` predictions, aggregates R/C and R-E2E/R-T4-
conditional statistics with fixed-seed 10,000-stratified-cluster-bootstrap
and conservative Clopper-Pearson degeneracy fallback, selects one of the
exact run statuses, and seals an immutable content-addressed bundle.
"""

from __future__ import annotations

import copy
import contextlib
import hashlib
import json
import math
import platform
import random
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from task4_consistency.controlled.s12_runner import (
    RUNNER_REQUEST_SCHEMA,
    RUNNER_RESULT_SCHEMA,
    run_s12_runner,
)

PLAN_COMMAND_SCHEMA = "s12-plan-command/1"
PLAN_SCHEMA = "s12-evaluation-plan/1"
JOB_SCHEMA = "s12-job/1"
ATTEMPT_SCHEMA = "s12-attempt/1"
BUNDLE_SCHEMA = "s12-evaluation-bundle/1"
LABEL_MANIFEST_SCHEMA = "s12-label-manifest/1"
EVALUATOR_BUILD = "s12-evaluator/1"

PREDICTION_ALPHABET = (
    "consistent",
    "inconsistent",
    "uncertain",
    "skipped",
    "missing",
    "error",
)
GOLD_ALPHABET = ("consistent", "inconsistent", "indeterminate", "not_applicable")
TRACKS = ("R", "C")
VIEWS = ("R-E2E", "R-T4-conditional")

_BOOTSTRAP_REPLICATES = 10_000
_COVERAGE_GATE = 0.80
_FPR_GATE = 0.05
_FNR_GATE = 0.03
_MISS_GATE = 0.10
_MIN_CONSISTENT_CLUSTERS = 59
_MIN_INCONSISTENT_CLUSTERS = 99

_USAGE_PARTITIONS = ("development", "calibration", "acceptance_holdout")

_ZERO_BUSINESS_DELTAS = {
    "lifecycle_revision": 0,
    "evidence_rows": 0,
    "evidence_digest": None,
    "current_run_pointer": 0,
    "policy_revision": 0,
    "governance_revision": 0,
}


def _business_deltas(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    """Computed deltas over the measured S01/S08 business facts, expressed in
    the fixed formal delta contract."""
    return {
        "lifecycle_revision": int(after.get("lifecycle_revision") or 0)
        - int(before.get("lifecycle_revision") or 0),
        "evidence_rows": int(after.get("evidence_count") or 0)
        - int(before.get("evidence_count") or 0),
        "evidence_digest": (
            after.get("evidence_digest")
            if before.get("evidence_digest") != after.get("evidence_digest")
            else None
        ),
        "current_run_pointer": (
            0
            if before.get("current_run_reference")
            == after.get("current_run_reference")
            else 1
        ),
        "policy_revision": int(after.get("activation_count") or 0)
        - int(before.get("activation_count") or 0),
        "governance_revision": int(after.get("governance_revision") or 0)
        - int(before.get("governance_revision") or 0),
    }


# S12 immutable evaluation rows are keyed (table, item_id) with a payload
# integrity digest, mirroring the S01 immutable-facts pattern on a private
# schema.  The evaluation database never holds business facts.
_EVAL_TABLES = (
    "s12_plans",
    "s12_jobs",
    "s12_attempts",
    "s12_predictions",
    "s12_bundles",
)
_INTEGRITY_SCHEMA = "s12-evaluation-integrity/1"

_MAX_SUBPROCESS_STDOUT_BYTES = 32 * 1024 * 1024
_MAX_SUBPROCESS_STDERR_BYTES = 1 * 1024 * 1024
_SUBPROCESS_TIMEOUT_SECONDS = 120


def canonical_bytes(value: Any) -> bytes:
    """One canonical UTF-8 encoding for S12 content: sorted keys, compact
    separators, no NaN/Infinity.  Key-agnostic by design: plan and bundle
    content legitimately carries evidence field payloads, so the
    manifest-scoped S09 canonicalization does not apply here."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _verify_bundle_content_address(bundle_id: str, bundle: dict[str, Any]) -> None:
    """Recompute the content-addressed digest of a bundle on every read: the
    id must equal the SHA-256 of the canonical complete bundle bytes."""
    if bundle.get("bundle_id") != bundle_id:
        raise S12IntegrityError(
            f"content-addressed S12 bundle id mismatch: {bundle_id}"
        )
    expected = "s12_bundle_sha256_" + content_digest(
        {key: value for key, value in bundle.items() if key != "bundle_id"}
    )
    if bundle_id != expected:
        raise S12IntegrityError(
            f"content-addressed S12 bundle digest mismatch: {bundle_id}"
        )


class S12IntegrityError(RuntimeError):
    """An immutable S12 evaluation row failed its integrity digest."""


class S12Unavailable(RuntimeError):
    """The evaluation authority cannot be proven and fails closed."""


class LabelManifestUnavailable(ValueError):
    """The evaluation-owned label manifest is missing, unregistered, or
    digest-mismatched: caller-supplied label claims cannot impersonate the
    label authority."""


class LabelManifestStore:
    """Evaluation-owned configured label storage.  Manifests are registered
    files named ``<manifest_id>.json`` under one configured root; every
    resolution re-verifies the content-addressed manifest id and digest so
    label custody and split provenance stay outside runner input and outside
    caller claims."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def resolve(
        self, manifest_id: str, manifest_digest: str
    ) -> dict[str, Any]:
        if not isinstance(manifest_id, str) or not manifest_id:
            raise LabelManifestUnavailable("label manifest id is required")
        if not isinstance(manifest_digest, str) or len(manifest_digest) != 64:
            raise LabelManifestUnavailable("label manifest digest is required")
        path = self._root / f"{manifest_id}.json"
        try:
            raw = path.read_bytes()
        except OSError:
            raise LabelManifestUnavailable(
                f"label manifest is not registered: {manifest_id}"
            ) from None
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise LabelManifestUnavailable(
                f"label manifest is not valid JSON: {manifest_id}"
            ) from None
        if not isinstance(manifest, dict):
            raise LabelManifestUnavailable("label manifest must be an object")
        if manifest.get("schema_version") != LABEL_MANIFEST_SCHEMA:
            raise LabelManifestUnavailable("label manifest schema mismatch")
        body = {
            key: value
            for key, value in manifest.items()
            if key not in {"manifest_id"}
        }
        digest = content_digest(body)
        if (
            manifest.get("manifest_id") != manifest_id
            or digest != manifest_digest
            or manifest_id != f"manifest_sha256_{digest}"
        ):
            raise LabelManifestUnavailable(
                f"label manifest digest does not verify: {manifest_id}"
            )
        labels = manifest.get("labels")
        if not isinstance(labels, dict):
            raise LabelManifestUnavailable("label manifest labels are missing")
        for opportunity_id, label in labels.items():
            if (
                not isinstance(opportunity_id, str)
                or not opportunity_id
                or label not in GOLD_ALPHABET
            ):
                raise LabelManifestUnavailable(
                    "label manifest contains an invalid gold label"
                )
        return manifest


def _integrity_digest(table: str, item_id: str, payload: str) -> str:
    material = "\0".join(
        (_INTEGRITY_SCHEMA, table, item_id, payload)
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


class _EvalStore:
    """Private evaluation-owned SQLite store: immutable payload rows with
    integrity digests.  Plans/jobs are upserted as their durable state
    advances; bundles are append-only (an existing bundle id is never
    overwritten)."""

    def __init__(self, state_path: str | Path) -> None:
        self.state_path = str(state_path)
        self.plans: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.attempts: dict[str, dict[str, Any]] = {}
        self.predictions: dict[str, dict[str, Any]] = {}
        self.bundles: dict[str, dict[str, Any]] = {}
        self._reload_once()

    def _connect(self) -> sqlite3.Connection:
        path = Path(self.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.state_path, timeout=5.0)
        connection.isolation_level = None
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _ensure_schema(self) -> None:
        with contextlib.closing(self._connect()) as connection:
            for table in _EVAL_TABLES:
                connection.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        item_id TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        integrity_sha256 TEXT NOT NULL
                    )
                    """
                )

    def _reload_once(self) -> None:
        self._ensure_schema()
        with contextlib.closing(self._connect()) as connection:
            for table, target in (
                ("s12_plans", self.plans),
                ("s12_jobs", self.jobs),
                ("s12_attempts", self.attempts),
                ("s12_predictions", self.predictions),
                ("s12_bundles", self.bundles),
            ):
                rows = connection.execute(
                    f"SELECT item_id, payload, integrity_sha256 FROM {table}"
                ).fetchall()
                for item_id, payload, declared_digest in rows:
                    if _integrity_digest(table, item_id, payload) != declared_digest:
                        raise S12IntegrityError(
                            f"immutable S12 integrity: {table}/{item_id}"
                        )
                    value = json.loads(payload)
                    if table == "s12_bundles":
                        _verify_bundle_content_address(item_id, value)
                    target[item_id] = value

    def reload(self) -> None:
        self.plans.clear()
        self.jobs.clear()
        self.attempts.clear()
        self.predictions.clear()
        self.bundles.clear()
        self._reload_once()

    def claim_job_transaction(
        self, job_id: str, *, worker_id: str | None, now: int
    ) -> dict[str, Any] | tuple[dict[str, Any], int]:
        """Claim one durable job atomically at the SQL level: the write
        transaction reads the authoritative job row, verifies the claim
        precondition, and persists the leased row plus its running attempt in
        one BEGIN IMMEDIATE transaction.  Two service instances sharing one
        state path therefore cannot claim the same fence/attempt: the second
        writer blocks on the write lock and then observes the first claim."""
        self._ensure_schema()
        with contextlib.closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, integrity_sha256 FROM s12_jobs WHERE item_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                return {"status": "failed", "reason_code": "JOB_NOT_FOUND"}
            payload, declared_digest = row
            if _integrity_digest("s12_jobs", job_id, payload) != declared_digest:
                connection.execute("ROLLBACK")
                raise S12IntegrityError(f"immutable S12 integrity: s12_jobs/{job_id}")
            job = json.loads(payload)
            status = job.get("status")
            if status == "complete":
                connection.execute("ROLLBACK")
                return {"status": "failed", "reason_code": "JOB_ALREADY_COMPLETE"}
            if status == "cancelled":
                connection.execute("ROLLBACK")
                return {"status": "failed", "reason_code": "JOB_CANCELLED"}
            if status == "diagnostic":
                connection.execute("ROLLBACK")
                return {"status": "failed", "reason_code": "JOB_DIAGNOSTIC"}
            if status == "leased" and int(job.get("lease_until") or 0) > now:
                connection.execute("ROLLBACK")
                return {"status": "busy", "reason_code": "JOB_LEASE_ACTIVE"}
            job["status"] = "leased"
            job["fence"] = int(job.get("fence", 0)) + 1
            job["attempt_no"] = int(job.get("attempt_no", 0)) + 1
            job["lease_until"] = now + 30
            if worker_id is not None:
                job["worker_id"] = worker_id
            payload = json.dumps(
                job, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            connection.execute(
                "UPDATE s12_jobs SET payload = ?, integrity_sha256 = ? "
                "WHERE item_id = ?",
                (payload, _integrity_digest("s12_jobs", job_id, payload), job_id),
            )
            attempt = {
                "schema_version": ATTEMPT_SCHEMA,
                "job_id": job_id,
                "fence": job["fence"],
                "attempt_no": job["attempt_no"],
                "worker_id": job["worker_id"],
                "status": "running",
                "started_at": now,
            }
            attempt_id = EvaluationService._stable_id(
                "s12attempt", f"{job_id}:{job['fence']}:{job['attempt_no']}"
            )
            attempt_payload = json.dumps(
                attempt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            connection.execute(
                "INSERT OR REPLACE INTO s12_attempts "
                "(item_id, payload, integrity_sha256) VALUES (?, ?, ?)",
                (
                    attempt_id,
                    attempt_payload,
                    _integrity_digest("s12_attempts", attempt_id, attempt_payload),
                ),
            )
            connection.execute("COMMIT")
            return job, now

    def _row_payload(
        self, connection: sqlite3.Connection, table: str, item_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            f"SELECT payload, integrity_sha256 FROM {table} WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            return None
        payload, declared_digest = row
        if _integrity_digest(table, item_id, payload) != declared_digest:
            raise S12IntegrityError(f"immutable S12 integrity: {table}/{item_id}")
        return json.loads(payload)

    @staticmethod
    def _write_row(
        connection: sqlite3.Connection, table: str, item_id: str, value: dict[str, Any]
    ) -> None:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        connection.execute(
            f"INSERT INTO {table} (item_id, payload, integrity_sha256) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET "
            "payload = excluded.payload, integrity_sha256 = excluded.integrity_sha256",
            (item_id, payload, _integrity_digest(table, item_id, payload)),
        )

    def insert_plan(self, plan_id: str, plan: dict[str, Any]) -> None:
        """Row-scoped plan insertion: an existing plan must be byte-identical
        or the insertion fails; no other row is rewritten."""
        with contextlib.closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._row_payload(connection, "s12_plans", plan_id)
            if existing is not None:
                if existing != plan:
                    connection.execute("ROLLBACK")
                    raise ValueError(
                        f"plan {plan_id} already frozen with different content"
                    )
                connection.execute("COMMIT")
                return
            self._write_row(connection, "s12_plans", plan_id, plan)
            connection.execute("COMMIT")

    def insert_job(self, job_id: str, job: dict[str, Any]) -> None:
        """Row-scoped job insertion: only the new job row is written."""
        with contextlib.closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._row_payload(connection, "s12_jobs", job_id)
            if existing is not None:
                if existing != job:
                    connection.execute("ROLLBACK")
                    raise ValueError(f"job {job_id} already exists")
                connection.execute("COMMIT")
                return
            self._write_row(connection, "s12_jobs", job_id, job)
            connection.execute("COMMIT")

    def cancel_job_transaction(self, job_id: str) -> dict[str, Any] | None:
        """Row-scoped cancel: the authoritative row is re-read and verified
        inside the write transaction; terminal jobs are returned unchanged."""
        with contextlib.closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = self._row_payload(connection, "s12_jobs", job_id)
            if job is None:
                connection.execute("ROLLBACK")
                raise ValueError(f"job {job_id} does not exist")
            if job.get("status") in {"complete", "cancelled", "diagnostic"}:
                connection.execute("ROLLBACK")
                return job
            job["status"] = "cancelled"
            job["lease_until"] = None
            self._write_row(connection, "s12_jobs", job_id, job)
            connection.execute("COMMIT")
            return job

    def _owned_row(
        self,
        connection: sqlite3.Connection,
        job: dict[str, Any],
        now: int,
    ) -> dict[str, Any] | None:
        current = self._row_payload(connection, "s12_jobs", job["job_id"])
        if current is None:
            return None
        if (
            current.get("status") != "leased"
            or current.get("worker_id") != job.get("worker_id")
            or current.get("fence") != job.get("fence")
            or current.get("attempt_no") != job.get("attempt_no")
            or int(current.get("lease_until") or 0) <= now
        ):
            return None
        return current

    def settle_stale_attempt_transaction(
        self, job: dict[str, Any], attempt: dict[str, Any]
    ) -> None:
        """Row-scoped stale settlement: only the worker's own attempt row is
        appended; the job row is never touched."""
        with contextlib.closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._write_row(
                connection,
                "s12_attempts",
                attempt["attempt_id"],
                attempt,
            )
            connection.execute("COMMIT")

    def settle_diagnostic_transaction(
        self,
        job: dict[str, Any],
        now: int,
        *,
        reasons: list[str],
        attempt: dict[str, Any],
    ) -> bool:
        """Row-scoped diagnostic settlement: the job row is re-read and
        verified (worker/fence/attempt/live lease) inside the write
        transaction; a reclaimed job settles nothing."""
        with contextlib.closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._owned_row(connection, job, now)
            if current is None:
                connection.execute("ROLLBACK")
                return False
            current["status"] = "diagnostic"
            current.pop("lease_until", None)
            current["reason_codes"] = list(reasons)
            current["result"] = {
                "bundle_id": None,
                "status": "INVALID",
                "reason_codes": list(reasons),
            }
            self._write_row(connection, "s12_jobs", job["job_id"], current)
            self._write_row(
                connection, "s12_attempts", attempt["attempt_id"], attempt
            )
            connection.execute("COMMIT")
            return True

    def publish_bundle_transaction(
        self,
        job: dict[str, Any],
        now: int,
        *,
        bundle: dict[str, Any],
        predictions: dict[str, dict[str, Any]],
        attempt: dict[str, Any],
    ) -> bool:
        """Row-scoped atomic publication: one transaction conditionally owns
        the current leased job by job id, worker identity, fence, attempt and
        unexpired lease, then inserts predictions, the terminal attempt, the
        immutable bundle (append-only, byte-identical replay allowed) and the
        terminal job state.  A failed CAS publishes nothing."""
        with contextlib.closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._owned_row(connection, job, now)
            if current is None:
                connection.execute("ROLLBACK")
                return False
            bundle_id = bundle["bundle_id"]
            existing_bundle = self._row_payload(
                connection, "s12_bundles", bundle_id
            )
            if existing_bundle is not None:
                if existing_bundle != bundle:
                    connection.execute("ROLLBACK")
                    raise S12IntegrityError(
                        "append-only S12 bundle collision: " + bundle_id
                    )
            else:
                self._write_row(connection, "s12_bundles", bundle_id, bundle)
            for prediction_id, prediction in predictions.items():
                self._write_row(
                    connection, "s12_predictions", prediction_id, prediction
                )
            self._write_row(
                connection, "s12_attempts", attempt["attempt_id"], attempt
            )
            current["status"] = "complete"
            current.pop("lease_until", None)
            current["result"] = dict(attempt["result"])
            self._write_row(connection, "s12_jobs", job["job_id"], current)
            connection.execute("COMMIT")
            return True


# ---------------------------------------------------------------------------
# Statistics: fixed-seed stratified cluster bootstrap + Clopper-Pearson
# ---------------------------------------------------------------------------


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = q * (len(ordered) - 1)
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return float(ordered[lower])
    fraction = rank - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz)."""
    max_iterations = 200
    epsilon = 3.0e-12
    tiny = 1.0e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for iteration in range(1, max_iterations + 1):
        m2 = 2 * iteration
        aa = iteration * (b - iteration) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + iteration) * (qab + iteration) * x / (
            (a + m2) * (qap + m2)
        )
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < epsilon:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b) via the continued fraction."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_bt = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    bt = math.exp(log_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _clopper_pearson_upper(k: int, n: int) -> float:
    """One-sided 95% Clopper-Pearson upper bound: k error clusters among n
    independent clusters.  Closed form for the zero-error case, bisection on
    the incomplete beta otherwise (stdlib only)."""
    if n <= 0:
        raise ValueError("Clopper-Pearson requires a positive cluster count")
    if k < 0 or k > n:
        raise ValueError("Clopper-Pearson error count out of range")
    if k == 0:
        return 1.0 - 0.05 ** (1.0 / n)
    a = k + 1.0
    b = n - k
    target = 0.95
    low, high = 0.0, 1.0
    for _ in range(60):
        mid = (low + high) / 2.0
        if _betai(a, b, mid) < target:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def _opportunity_point_metrics(
    opportunities: list[dict[str, Any]], predictions: dict[str, str]
) -> dict[str, Any]:
    """Point estimates over all eligible C/I gold opportunities; denominators
    are fixed and never shrink when a prediction is uncertain/skipped/missing/
    error."""
    eligible = [o for o in opportunities if o["label"] in {"consistent", "inconsistent"}]
    n_consistent = sum(1 for o in eligible if o["label"] == "consistent")
    n_inconsistent = sum(1 for o in eligible if o["label"] == "inconsistent")
    e = len(eligible)
    prediction_counts = {token: 0 for token in PREDICTION_ALPHABET}
    fp = fn = miss = decisive = 0
    for opportunity in eligible:
        prediction = predictions.get(opportunity["opportunity_id"], "missing")
        prediction_counts[prediction] += 1
        gold = opportunity["label"]
        if prediction in {"consistent", "inconsistent"}:
            decisive += 1
        if gold == "consistent" and prediction == "inconsistent":
            fp += 1
        if gold == "inconsistent" and prediction == "consistent":
            fn += 1
        if gold == "inconsistent" and prediction != "inconsistent":
            miss += 1
    return {
        "denominators": {
            "E": e,
            "n_consistent": n_consistent,
            "n_inconsistent": n_inconsistent,
        },
        "prediction_counts": prediction_counts,
        "point": {
            "coverage": (decisive / e) if e else 0.0,
            "false_positive_rate": (fp / n_consistent) if n_consistent else 0.0,
            "false_negative_rate": (fn / n_inconsistent) if n_inconsistent else 0.0,
            "miss_rate": (miss / n_inconsistent) if n_inconsistent else 0.0,
        },
        "_counts": {"fp": fp, "fn": fn, "miss": miss},
    }


def _resample_metrics(
    opportunities_by_cluster: dict[str, list[dict[str, Any]]],
    strata: dict[str, list[str]],
    predictions: dict[str, str],
    rng: Any,
) -> tuple[dict[str, float], bool]:
    """One bootstrap replicate: resample clusters within each stratum with
    replacement and recompute the point metrics over the resampled
    opportunities.  A replicate with a zero class denominator is invalid."""
    chosen: list[dict[str, Any]] = []
    for stratum_clusters in strata.values():
        sampled = rng.choices(stratum_clusters, k=len(stratum_clusters))
        for cluster_id in sampled:
            chosen.extend(opportunities_by_cluster[cluster_id])
    metrics = _opportunity_point_metrics(chosen, predictions)
    denominators = metrics["denominators"]
    if not denominators["E"] or not denominators["n_consistent"] or not denominators["n_inconsistent"]:
        return {}, False
    point = metrics["point"]
    return point, True


def _cluster_statistics(
    opportunities: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
    predictions: dict[str, str],
    seed: int,
    *,
    membership: str,
) -> dict[str, Any]:
    """Per-track/view statistics: fixed-seed 10,000 stratified cluster
    bootstrap, two-sided 95% intervals, one-sided 95% acceptance bounds, and
    the conservative Clopper-Pearson fallback for zero-error degeneracy.
    Independent cluster minima keep underpowered class estimates
    ``not estimable``."""
    if not opportunities:
        return {
            "membership": membership,
            "opportunity_count": 0,
            "denominators": {"E": 0, "n_consistent": 0, "n_inconsistent": 0},
            "prediction_counts": {token: 0 for token in PREDICTION_ALPHABET},
            "point": {
                "coverage": None,
                "false_positive_rate": None,
                "false_negative_rate": None,
                "miss_rate": None,
            },
            "interval_95_two_sided": None,
            "bounds_95_one_sided": None,
            "estimable": False,
            "not_estimable_reasons": ["no_opportunities"],
            "conclusion": "empty",
        }
    base = _opportunity_point_metrics(opportunities, predictions)
    if base["denominators"]["E"] == 0:
        return {
            "membership": membership,
            "opportunity_count": len(opportunities),
            "denominators": base["denominators"],
            "prediction_counts": base["prediction_counts"],
            "point": base["point"],
            "interval_95_two_sided": None,
            "bounds_95_one_sided": None,
            "estimable": False,
            "not_estimable_reasons": ["no_eligible_c_i_gold"],
            "conclusion": "smoke_only",
        }
    opportunities_by_cluster: dict[str, list[dict[str, Any]]] = {}
    for opportunity in opportunities:
        opportunities_by_cluster.setdefault(
            opportunity["cluster"], []
        ).append(opportunity)
    strata: dict[str, list[str]] = {}
    for cluster in clusters:
        if cluster["cluster_id"] in opportunities_by_cluster:
            strata.setdefault(str(cluster.get("stratum") or "default"), []).append(
                cluster["cluster_id"]
            )
    rng = random.Random(seed)
    replicates: dict[str, list[float]] = {
        "coverage": [],
        "false_positive_rate": [],
        "false_negative_rate": [],
        "miss_rate": [],
    }
    valid = 0
    for _ in range(_BOOTSTRAP_REPLICATES):
        point, ok = _resample_metrics(
            opportunities_by_cluster, strata, predictions, rng
        )
        if not ok:
            continue
        valid += 1
        for name, value in point.items():
            replicates[name].append(value)
    not_estimable: list[str] = []
    if valid / _BOOTSTRAP_REPLICATES < 0.95:
        not_estimable.append(
            f"valid_resamples={valid} below 95% of {_BOOTSTRAP_REPLICATES}"
        )
    denominators = base["denominators"]
    counts = base["_counts"]
    point = base["point"]

    def _two_sided(name: str) -> list[float]:
        return [
            round(_percentile(replicates[name], 0.025), 6),
            round(_percentile(replicates[name], 0.975), 6),
        ]

    def _one_sided_lower(name: str) -> float:
        return round(_percentile(replicates[name], 0.05), 6)

    def _one_sided_upper(name: str) -> float:
        return round(_percentile(replicates[name], 0.95), 6)

    if not not_estimable and not replicates["coverage"]:
        not_estimable.append("bootstrap produced no valid replicates")

    # Degeneracy fallback: a zero point error rate makes the bootstrap bound
    # optimistic; use the more conservative one-sided 95% Clopper-Pearson
    # bound over the independent clusters exposed to the class, and require
    # the pinned cluster minima for that class.
    consistent_clusters = {
        opportunity["cluster"]
        for opportunity in opportunities
        if opportunity["label"] == "consistent"
    }
    inconsistent_clusters = {
        opportunity["cluster"]
        for opportunity in opportunities
        if opportunity["label"] == "inconsistent"
    }
    fp_clusters = {
        opportunity["cluster"]
        for opportunity in opportunities
        if opportunity["label"] == "consistent"
        and predictions.get(opportunity["opportunity_id"]) == "inconsistent"
    }
    fn_clusters = {
        opportunity["cluster"]
        for opportunity in opportunities
        if opportunity["label"] == "inconsistent"
        and predictions.get(opportunity["opportunity_id"]) == "consistent"
    }
    miss_clusters = {
        opportunity["cluster"]
        for opportunity in opportunities
        if opportunity["label"] == "inconsistent"
        and predictions.get(opportunity["opportunity_id"]) != "inconsistent"
    }

    bounds: dict[str, float] = {}
    for name, count_key, upper_name, error_clusters, exposed_clusters, minimum in (
        ("false_positive_rate", "fp", "false_positive_rate_upper", fp_clusters,
         consistent_clusters, _MIN_CONSISTENT_CLUSTERS),
        ("false_negative_rate", "fn", "false_negative_rate_upper", fn_clusters,
         inconsistent_clusters, _MIN_INCONSISTENT_CLUSTERS),
        ("miss_rate", "miss", "miss_rate_upper", miss_clusters,
         inconsistent_clusters, _MIN_INCONSISTENT_CLUSTERS),
    ):
        # The independent-cluster minima gate the class bounds
        # unconditionally: an underpowered sample is not estimable even when
        # its point rate is non-zero (ADR-0007 §6).
        exposed = len(exposed_clusters)
        if exposed < minimum:
            not_estimable.append(
                f"{name}: independent {_class_label(name)} clusters "
                f"{exposed} < {minimum}"
            )
            bounds[upper_name] = None
            continue
        bootstrap_upper = (
            _one_sided_upper(name) if replicates[name] else None
        )
        if point[name] == 0.0 or counts[count_key] == 0:
            errors = len(error_clusters)
            cp_upper = _clopper_pearson_upper(errors, exposed)
            bounds[upper_name] = (
                round(max(bootstrap_upper, cp_upper), 6)
                if bootstrap_upper is not None
                else round(cp_upper, 6)
            )
        else:
            bounds[upper_name] = bootstrap_upper

    interval_95 = None
    if replicates["coverage"]:
        interval_95 = {
            "coverage": _two_sided("coverage"),
            "false_positive_rate": _two_sided("false_positive_rate"),
            "false_negative_rate": _two_sided("false_negative_rate"),
            "miss_rate": _two_sided("miss_rate"),
        }
        bounds["coverage_lower"] = _one_sided_lower("coverage")
    else:
        bounds["coverage_lower"] = None

    conclusion = _conclusion(point, bounds, not_estimable)
    estimable = not not_estimable
    if not estimable:
        conclusion = "insufficient"
    return {
        "membership": membership,
        "opportunity_count": len(opportunities),
        "denominators": denominators,
        "prediction_counts": base["prediction_counts"],
        "point": point,
        "interval_95_two_sided": interval_95,
        "bounds_95_one_sided": bounds,
        "estimable": estimable,
        "not_estimable_reasons": sorted(set(not_estimable)),
        "conclusion": conclusion,
    }


def _class_label(name: str) -> str:
    if name == "false_positive_rate":
        return "consistent"
    return "inconsistent"


def _conclusion(
    point: dict[str, float],
    bounds: dict[str, float | None],
    not_estimable: list[str],
) -> str:
    if not_estimable:
        return "insufficient"
    gates = [
        point["coverage"] >= _COVERAGE_GATE
        and (bounds.get("coverage_lower") or 0.0) >= _COVERAGE_GATE,
        point["false_positive_rate"] <= _FPR_GATE
        and (bounds.get("false_positive_rate_upper") or math.inf) <= _FPR_GATE,
        point["false_negative_rate"] <= _FNR_GATE
        and (bounds.get("false_negative_rate_upper") or math.inf) <= _FNR_GATE,
        point["miss_rate"] <= _MISS_GATE
        and (bounds.get("miss_rate_upper") or math.inf) <= _MISS_GATE,
    ]
    if all(gates):
        return "pass"
    return "fail"


def _select_status(
    track_statistics: dict[str, dict[str, Any]],
    view_statistics: dict[str, dict[str, Any]],
    scope: str,
    *,
    holdout_eligible: bool = True,
    mandatory_families_ok: bool = True,
) -> tuple[str, list[str]]:
    """Exact status vocabulary: INVALID / INSUFFICIENT / FAIL / PASS(scope=...)
    / SMOKE_ONLY.  Formal unscoped PASS is prohibited; PASS always carries the
    frozen plan scope.  A formal PASS additionally requires verified holdout
    eligibility and every declared mandatory check family estimable and
    passing; development/calibration evidence stays non-formal (INSUFFICIENT
    with the exact reason), never a formal PASS or FAIL."""
    active = [
        item
        for item in (*track_statistics.values(), *view_statistics.values())
        if item["opportunity_count"] > 0
    ]
    if not active:
        return "SMOKE_ONLY", ["no eligible C/I gold opportunities"]
    if sum(item["denominators"]["E"] for item in active) == 0:
        return "SMOKE_ONLY", ["no eligible C/I gold opportunities"]
    non_formal: list[str] = []
    if not holdout_eligible:
        non_formal.append("non-formal scope: not all opportunities are "
                          "acceptance_holdout with independent label custody")
    if not mandatory_families_ok:
        non_formal.append("non-formal scope: a mandatory check family is "
                          "not estimable and passing")
    if non_formal:
        return "INSUFFICIENT", sorted(set(non_formal))
    reasons: list[str] = []
    for item in active:
        if not item["estimable"]:
            for reason in item["not_estimable_reasons"]:
                reasons.append(f"{item['membership']}: {reason}")
    if reasons:
        return "INSUFFICIENT", sorted(set(reasons))
    failures: list[str] = []
    for item in active:
        if item["conclusion"] != "pass":
            failures.append(
                f"{item['membership']}: point/bound gates not all passing"
            )
    if failures:
        return "FAIL", sorted(failures)
    return f"PASS(scope={scope})", ["all scoped gates pass in one frozen run"]


def _holdout_eligibility(
    opportunities: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    """Formal eligibility: every frozen opportunity must belong to an
    ``acceptance_holdout`` cluster and carry independent label custody.
    Development and calibration evidence can never support a formal result."""
    usage_by_cluster = {
        str(cluster.get("cluster_id") or ""): str(cluster.get("usage") or "")
        for cluster in clusters
    }
    reasons: list[str] = []
    for opportunity in opportunities:
        usage = usage_by_cluster.get(str(opportunity.get("cluster") or ""), "")
        if usage != "acceptance_holdout":
            reasons.append(
                f"opportunity {opportunity.get('opportunity_id')} is in a "
                f"{usage or 'unregistered'} usage cluster"
            )
        if opportunity.get("label_custody") != "independent":
            reasons.append(
                f"opportunity {opportunity.get('opportunity_id')} lacks "
                "independent label custody"
            )
    return (not reasons), sorted(set(reasons))


# ---------------------------------------------------------------------------
# EvaluationService
# ---------------------------------------------------------------------------


class EvaluationService:
    """The concrete deep S12 evaluation authority: freeze/start/cancel/
    process/query/rerun over the evaluation-owned SQLite store.

    The interface accepts immutable references (evidence snapshot id,
    governed release id/digest, label manifest id/digest) plus the declared
    evaluation structure.  The authority resolves the actual content from
    the configured S01/S08/label providers, derives the environment, and
    measures business state before freeze and before terminal publication."""

    def __init__(
        self,
        *,
        state_path: str | Path,
        clock: Callable[[], int] | None = None,
        runner_override: Callable[[dict[str, Any]], dict[str, Any] | None]
        | None = None,
        snapshot_provider: Callable[[str, str], dict[str, Any]] | None = None,
        release_provider: Callable[[str, str], dict[str, Any]] | None = None,
        label_manifest_provider: Callable[[str, str], dict[str, Any]]
        | None = None,
        business_state_provider: Callable[[], dict[str, Any]] | None = None,
        worker_subject: str = "s12-evaluator-worker",
    ) -> None:
        if snapshot_provider is None:
            raise ValueError("snapshot_provider is required")
        if release_provider is None:
            raise ValueError("release_provider is required")
        if label_manifest_provider is None:
            raise ValueError("label_manifest_provider is required")
        if business_state_provider is None:
            raise ValueError("business_state_provider is required")
        self._store = _EvalStore(state_path)
        self._clock = clock or (lambda: int(time.time()))
        self._runner_override = runner_override
        self._snapshot_provider = snapshot_provider
        self._release_provider = release_provider
        self._label_manifest_provider = label_manifest_provider
        self._business_state_provider = business_state_provider
        self._worker_subject = str(worker_subject) or "s12-evaluator-worker"
        self._lock = threading.RLock()

    @staticmethod
    def _stable_id(prefix: str, fingerprint: str) -> str:
        return f"{prefix}_{hashlib.sha256(fingerprint.encode()).hexdigest()[:24]}"

    # -- plan freeze -------------------------------------------------------

    def freeze_plan(self, command: dict[str, Any]) -> dict[str, Any]:
        resolved = self._resolve_freeze_material(command)
        business_before = dict(self._business_state_provider())
        plan = {
            "schema_version": PLAN_SCHEMA,
            "plan_id": resolved["plan_id"],
            "scope": resolved["scope"],
            "seed": resolved["seed"],
            "budget": copy.deepcopy(resolved["budget"]),
            "stop_rule": resolved["stop_rule"],
            "split": copy.deepcopy(resolved["split"]),
            "clusters": copy.deepcopy(resolved["clusters"]),
            "tracks": copy.deepcopy(resolved["tracks"]),
            "views": copy.deepcopy(resolved["views"]),
            "opportunities": copy.deepcopy(resolved["opportunities"]),
            "label_manifest": copy.deepcopy(resolved["label_manifest"]),
            "environment": copy.deepcopy(resolved["environment"]),
            "release": copy.deepcopy(resolved["release"]),
            "checker_artifact": copy.deepcopy(resolved["checker_artifact"]),
            "run_specs": copy.deepcopy(resolved["run_specs"]),
            "evidence_references": copy.deepcopy(
                resolved["evidence_references"]
            ),
            "mandatory_check_families": copy.deepcopy(
                resolved["mandatory_check_families"]
            ),
            "cohort": copy.deepcopy(resolved["cohort"]),
            "business_before": business_before,
            "frozen_at": int(self._clock()),
        }
        digest = content_digest({k: v for k, v in plan.items() if k != "plan_digest"})
        plan["plan_digest"] = digest
        with self._lock:
            self._store.reload()
            self._store.insert_plan(plan["plan_id"], plan)
            self._store.reload()
        return plan

    def _resolve_freeze_material(self, command: dict[str, Any]) -> dict[str, Any]:
        """Validate the reference command and resolve every immutable
        reference from the configured authorities.  Caller-supplied content
        (run specs, checker artifacts, gold labels, environment claims)
        cannot impersonate the S01/S08/label authorities."""
        if not isinstance(command, dict):
            raise ValueError("plan command must be an object")
        if command.get("schema_version") != PLAN_COMMAND_SCHEMA:
            raise ValueError("plan command schema mismatch")
        if (
            "environment" in command
            or "run_specs" in command
            or "checker_artifact" in command
        ):
            raise ValueError(
                "caller-supplied environment, run_specs or checker_artifact "
                "claims are rejected: the authority resolves them"
            )
        plan_id = command.get("plan_id")
        scope = command.get("scope_declared")
        if not isinstance(plan_id, str) or not plan_id:
            raise ValueError("plan_id is required")
        if not isinstance(scope, str) or not scope:
            raise ValueError("scope_declared is required")
        seed = command.get("seed")
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("seed must be a non-negative integer")
        budget = command.get("budget")
        if (
            not isinstance(budget, dict)
            or not isinstance(budget.get("max_opportunities"), int)
            or budget["max_opportunities"] <= 0
            or not isinstance(budget.get("max_runtime_ms"), int)
            or budget["max_runtime_ms"] <= 0
        ):
            raise ValueError(
                "budget requires positive max_opportunities and max_runtime_ms"
            )
        stop_rule = command.get("stop_rule")
        if stop_rule not in {"plan-exhausted", "budget-or-plan"}:
            raise ValueError("stop_rule must be plan-exhausted or budget-or-plan")
        split = command.get("split")
        if not isinstance(split, dict) or not isinstance(split.get("scheme"), str):
            raise ValueError("split scheme is required")
        usage_partitions = split.get("usage_partitions")
        if (
            not isinstance(usage_partitions, list)
            or set(usage_partitions) != set(_USAGE_PARTITIONS)
        ):
            raise ValueError(
                "split usage partitions must be exactly development, "
                "calibration, acceptance_holdout"
            )

        # Release reference -> governed release provider.
        release_reference = command.get("release_reference")
        if (
            not isinstance(release_reference, dict)
            or not isinstance(release_reference.get("release_id"), str)
            or not release_reference["release_id"]
            or not isinstance(release_reference.get("release_digest"), str)
            or len(release_reference["release_digest"]) != 64
        ):
            raise ValueError("release_reference is required")
        try:
            resolved_release = self._release_provider(
                release_reference["release_id"],
                release_reference["release_digest"],
            )
        except Exception as error:
            raise ValueError(
                "release reference does not resolve against the governed authority"
            ) from error

        # Evidence references -> S01 snapshot provider.
        evidence_references = command.get("evidence_references")
        if not isinstance(evidence_references, dict) or not evidence_references:
            raise ValueError("evidence_references are required")
        resolved_snapshots: dict[str, dict[str, Any]] = {}
        for application_id, reference in evidence_references.items():
            if (
                not isinstance(application_id, str)
                or not application_id
                or not isinstance(reference, dict)
                or not isinstance(reference.get("snapshot_id"), str)
                or not reference["snapshot_id"]
                or not isinstance(reference.get("snapshot_digest"), str)
                or len(reference["snapshot_digest"]) != 64
            ):
                raise ValueError("evidence reference is invalid")
            try:
                snapshot = self._snapshot_provider(
                    application_id, reference["snapshot_id"]
                )
            except Exception as error:
                raise ValueError(
                    "evidence reference does not resolve against the S01 authority"
                ) from error
            if (
                snapshot.get("evidence_snapshot_id") != reference["snapshot_id"]
                or snapshot.get("evidence_snapshot_digest")
                != reference["snapshot_digest"]
            ):
                raise ValueError("evidence reference digest does not verify")
            resolved_snapshots[application_id] = snapshot

        # Label manifest reference -> evaluation-owned label store.
        label_reference = command.get("label_manifest")
        if (
            not isinstance(label_reference, dict)
            or not isinstance(label_reference.get("manifest_id"), str)
            or not label_reference["manifest_id"]
            or not isinstance(label_reference.get("manifest_digest"), str)
            or len(label_reference["manifest_digest"]) != 64
        ):
            raise ValueError("label_manifest reference is required")
        label_manifest = self._label_manifest_provider(
            label_reference["manifest_id"], label_reference["manifest_digest"]
        )

        clusters = command.get("clusters")
        if not isinstance(clusters, list) or not clusters:
            raise ValueError("clusters are required")
        cluster_ids: set[str] = set()
        owned_applications: dict[str, str] = {}
        owned_variants: dict[str, str] = {}
        for cluster in clusters:
            if (
                not isinstance(cluster, dict)
                or not isinstance(cluster.get("cluster_id"), str)
                or cluster["cluster_id"] in cluster_ids
                or not isinstance(cluster.get("applications"), list)
                or not cluster["applications"]
                or not isinstance(cluster.get("usage"), str)
                or cluster["usage"] not in usage_partitions
                or not isinstance(cluster.get("stratum"), str)
            ):
                raise ValueError(
                    "clusters must have unique ids, stratum, applications "
                    "and a declared usage partition"
                )
            cluster_ids.add(cluster["cluster_id"])
            for application_id in cluster["applications"]:
                if not isinstance(application_id, str) or not application_id:
                    raise ValueError("cluster application identity is invalid")
                if application_id in owned_applications:
                    raise ValueError(
                        f"application {application_id} belongs to more than "
                        "one base cluster"
                    )
                if application_id not in evidence_references:
                    raise ValueError(
                        f"cluster application {application_id} has no "
                        "evidence reference"
                    )
                owned_applications[application_id] = cluster["cluster_id"]
            variants = cluster.get("variants")
            if variants is not None:
                if not isinstance(variants, list):
                    raise ValueError("cluster variants must be a list")
                for variant_id in variants:
                    if (
                        not isinstance(variant_id, str)
                        or not variant_id
                        or variant_id in owned_variants
                    ):
                        raise ValueError(
                            "cluster variant identity is invalid or duplicated"
                        )
                    owned_variants[variant_id] = cluster["cluster_id"]

        opportunities = command.get("opportunities")
        if not isinstance(opportunities, list) or not opportunities:
            raise ValueError("opportunities are required")
        opportunity_ids: set[str] = set()
        for opportunity in opportunities:
            if not isinstance(opportunity, dict):
                raise ValueError("opportunity must be an object")
            required = (
                "opportunity_id",
                "track",
                "cluster",
                "application_id",
                "cycle",
                "check_id",
                "target_scope",
                "evidence_snapshot_id",
            )
            if any(opportunity.get(key) in (None, "") for key in required):
                raise ValueError("opportunity is missing a required field")
            if opportunity["opportunity_id"] in opportunity_ids:
                raise ValueError("opportunity ids must be unique")
            opportunity_ids.add(opportunity["opportunity_id"])
            if opportunity["track"] not in TRACKS:
                raise ValueError("opportunity track must be R or C")
            if opportunity["cluster"] not in cluster_ids:
                raise ValueError("opportunity cluster is unknown")
            application_id = opportunity["application_id"]
            if application_id not in resolved_snapshots:
                raise ValueError("opportunity application has no evidence reference")
            snapshot = resolved_snapshots[application_id]
            if (
                int(opportunity["cycle"]) != int(snapshot.get("cycle") or 0)
                or opportunity["evidence_snapshot_id"]
                != snapshot["evidence_snapshot_id"]
            ):
                raise ValueError(
                    "opportunity cycle or evidence snapshot does not match "
                    "the frozen run spec"
                )
        if len(opportunities) > budget["max_opportunities"]:
            raise ValueError("opportunities exceed the frozen budget")

        tracks = command.get("tracks")
        views = command.get("views")
        if not isinstance(tracks, dict) or set(tracks) != set(TRACKS):
            raise ValueError("tracks must declare exactly R and C")
        if not isinstance(views, dict) or set(views) != set(VIEWS):
            raise ValueError("views must declare exactly R-E2E and R-T4-conditional")

        def _membership_ids(collection: dict[str, Any], name: str) -> set[str]:
            entries = collection.get("opportunities")
            if not isinstance(entries, list):
                raise ValueError(f"{name} opportunities must be a list")
            member_ids = set(entries)
            unknown = member_ids - opportunity_ids
            if unknown:
                raise ValueError(f"{name} references unknown opportunities")
            return member_ids

        track_members = {
            track: _membership_ids(collection, track)
            for track, collection in tracks.items()
        }
        view_members = {
            view: _membership_ids(collection, view)
            for view, collection in views.items()
        }
        if track_members["R"] & track_members["C"]:
            raise ValueError("R and C tracks share opportunities")
        declared = {
            opportunity["opportunity_id"]
            for opportunity in opportunities
            if opportunity["track"] == "R"
        }
        if track_members["R"] != declared:
            raise ValueError("R track membership disagrees with opportunity tracks")
        declared_c = {
            opportunity["opportunity_id"]
            for opportunity in opportunities
            if opportunity["track"] == "C"
        }
        if track_members["C"] != declared_c:
            raise ValueError("C track membership disagrees with opportunity tracks")
        if view_members["R-E2E"] & view_members["R-T4-conditional"]:
            raise ValueError("R-E2E and R-T4-conditional views share opportunities")
        if (
            view_members["R-E2E"] | view_members["R-T4-conditional"]
            != track_members["R"]
        ):
            raise ValueError(
                "R-E2E and R-T4-conditional views must exactly account for "
                "every registered R opportunity"
            )

        mandatory_check_families = command.get("mandatory_check_families")
        if not isinstance(mandatory_check_families, list):
            raise ValueError("mandatory_check_families are required")
        opportunity_check_ids = {
            str(opportunity["check_id"]) for opportunity in opportunities
        }
        for family in mandatory_check_families:
            if (
                not isinstance(family, dict)
                or not isinstance(family.get("family_id"), str)
                or not family["family_id"]
                or not isinstance(family.get("check_ids"), list)
                or not family["check_ids"]
            ):
                raise ValueError("mandatory check family is invalid")
            unknown_checks = set(family["check_ids"]) - opportunity_check_ids
            if unknown_checks:
                raise ValueError(
                    f"mandatory check family {family['family_id']} has no "
                    "frozen opportunities"
                )
        cohort = command.get("cohort")
        if cohort is not None:
            if not isinstance(cohort, dict) or not isinstance(
                cohort.get("exclusions"), list
            ):
                raise ValueError("cohort exclusions must be a list")
            for exclusion in cohort["exclusions"]:
                if (
                    not isinstance(exclusion, dict)
                    or not isinstance(exclusion.get("item"), str)
                    or not exclusion["item"]
                    or not isinstance(exclusion.get("reason"), str)
                    or not exclusion["reason"]
                    or not isinstance(exclusion.get("reference_sha256"), str)
                    or len(exclusion["reference_sha256"]) != 64
                ):
                    raise ValueError("cohort exclusion is invalid")

        # Gold labels resolve from the label manifest only.
        labels = label_manifest.get("labels") or {}
        unknown_labels = set(labels) - opportunity_ids
        if unknown_labels:
            raise ValueError("label manifest references unknown opportunities")
        missing_labels = opportunity_ids - set(labels)
        if missing_labels:
            raise ValueError("label manifest does not cover every opportunity")
        resolved_opportunities = []
        for opportunity in opportunities:
            resolved = dict(opportunity)
            resolved["label"] = labels[opportunity["opportunity_id"]]
            resolved["label_custody"] = label_manifest.get("label_custody")
            resolved_opportunities.append(resolved)

        # Build frozen RunSpecs server-side from the resolved snapshots.
        public = resolved_release["target_release"].public_manifest()
        run_specs: dict[str, dict[str, Any]] = {}
        for application_id, snapshot in sorted(resolved_snapshots.items()):
            run_specs[application_id] = self._build_run_spec(
                application_id, snapshot, resolved_release, public
            )

        environment = {
            "python": platform.python_version(),
            "evaluator_build": EVALUATOR_BUILD,
            "dependency_identity": self._dependency_identity(),
            "schema_version": PLAN_SCHEMA,
        }
        return {
            "plan_id": plan_id,
            "scope": scope,
            "seed": seed,
            "budget": budget,
            "stop_rule": stop_rule,
            "split": split,
            "clusters": clusters,
            "tracks": tracks,
            "views": views,
            "opportunities": resolved_opportunities,
            "label_manifest": {
                "manifest_id": label_manifest["manifest_id"],
                "manifest_digest": label_reference["manifest_digest"],
                "label_custody": label_manifest.get("label_custody"),
            },
            "environment": environment,
            "release": {
                "release_id": resolved_release["release_id"],
                "release_digest": resolved_release["release_digest"],
                "checker_build": resolved_release["checker_build"],
                "manifest_id": resolved_release["manifest_id"],
                "manifest_digest": resolved_release["manifest_digest"],
                "protected_baseline_digest": resolved_release[
                    "protected_baseline_digest"
                ],
                "limits": resolved_release["limits"],
                "applicable_check_ids": resolved_release["applicable_check_ids"],
                "applicable_check_count": resolved_release[
                    "applicable_check_count"
                ],
            },
            "checker_artifact": resolved_release["checker_artifact"],
            "run_specs": run_specs,
            "evidence_references": {
                application_id: {
                    "snapshot_id": snapshot["evidence_snapshot_id"],
                    "snapshot_digest": snapshot["evidence_snapshot_digest"],
                    "cycle": snapshot["cycle"],
                }
                for application_id, snapshot in resolved_snapshots.items()
            },
            "mandatory_check_families": mandatory_check_families,
            "cohort": cohort,
        }

    @staticmethod
    def _build_run_spec(
        application_id: str,
        snapshot: dict[str, Any],
        resolved_release: dict[str, Any],
        public: dict[str, Any],
    ) -> dict[str, Any]:
        snapshot_payload = snapshot["evidence_snapshot"]
        snapshot_bytes = canonical_bytes(snapshot_payload)
        snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()
        if snapshot_digest != snapshot["evidence_snapshot_digest"]:
            raise ValueError("evidence snapshot digest does not verify")
        return {
            "run_id": f"run_{application_id}",
            "application_id": application_id,
            "cycle": int(snapshot.get("cycle") or 1),
            "lifecycle_revision": int(snapshot.get("lifecycle_revision") or 0),
            "evidence_snapshot_id": snapshot["evidence_snapshot_id"],
            "evidence_snapshot_digest": snapshot_digest,
            "evidence_snapshot": copy.deepcopy(snapshot_payload),
            "evidence_revision": int(snapshot.get("evidence_revision") or 0),
            "evidence_readiness_policy": "c-demo-readiness/1",
            "baseline_release": copy.deepcopy(public),
            "release_id": resolved_release["release_id"],
            "release_digest": resolved_release["release_digest"],
            "checker_build": resolved_release["checker_build"],
            "fence": 1,
            "limits": copy.deepcopy(resolved_release["limits"]),
            "applicable_check_ids": resolved_release["applicable_check_ids"],
            "applicable_check_count": resolved_release["applicable_check_count"],
        }

    @staticmethod
    def _dependency_identity() -> str:
        """Server-derived dependency identity: the digest of the declared
        dependency manifest (pyproject.toml) plus the evaluator build.  The
        caller cannot claim an environment."""
        repo_root = Path(__file__).resolve().parents[2]
        pyproject = repo_root / "pyproject.toml"
        try:
            dependency_bytes = pyproject.read_bytes()
        except OSError:
            dependency_bytes = b""
        return hashlib.sha256(dependency_bytes).hexdigest()

    def start_job(self, plan_id: str, worker_id: str) -> dict[str, Any]:
        with self._lock:
            self._store.reload()
            plan = self._require_plan(plan_id)
            job_id = self._stable_id(
                "s12job",
                f"{plan_id}:{worker_id}:{int(self._clock())}:{secrets.token_hex(8)}",
            )
            job = {
                "schema_version": JOB_SCHEMA,
                "job_id": job_id,
                "plan_id": plan_id,
                "plan_digest": plan["plan_digest"],
                "worker_id": worker_id,
                "status": "queued",
                "fence": 0,
                "attempt_no": 0,
                "lease_until": None,
                "rerun_of_bundle_id": None,
                "created_at": int(self._clock()),
            }
            self._store.insert_job(job_id, job)
            return job

    def rerun_job(self, job_id: str, worker_id: str) -> dict[str, Any]:
        with self._lock:
            self._store.reload()
            source = self._store.jobs.get(job_id)
            if source is None:
                raise ValueError(f"job {job_id} does not exist")
            source_bundle = source.get("result", {}).get("bundle_id")
            if not source_bundle:
                raise ValueError("job has no published bundle to rerun")
            plan = self._require_plan(source["plan_id"])
            rerun_job_id = self._stable_id(
                "s12job",
                f"{source['plan_id']}:{worker_id}:{int(self._clock())}:"
                f"{secrets.token_hex(8)}",
            )
            job = {
                "schema_version": JOB_SCHEMA,
                "job_id": rerun_job_id,
                "plan_id": source["plan_id"],
                "plan_digest": plan["plan_digest"],
                "worker_id": worker_id,
                "status": "queued",
                "fence": 0,
                "attempt_no": 0,
                "lease_until": None,
                "rerun_of_bundle_id": source_bundle,
                "created_at": int(self._clock()),
            }
            self._store.insert_job(rerun_job_id, job)
            return job

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            self._store.reload()
            return self._store.cancel_job_transaction(job_id)

    def query_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            self._store.reload()
            job = self._store.jobs.get(job_id)
            if job is None:
                raise ValueError(f"job {job_id} does not exist")
            return job

    def query_bundle(self, bundle_id: str) -> dict[str, Any]:
        with self._lock:
            self._store.reload()
            bundle = self._store.bundles.get(bundle_id)
            if bundle is None:
                raise ValueError(f"bundle {bundle_id} does not exist")
            _verify_bundle_content_address(bundle_id, bundle)
            return bundle

    # -- durable worker -----------------------------------------------------

    def process_job(
        self,
        job_id: str,
        *,
        runner_result: dict[str, Any] | None = None,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        claim = self._claim_job(job_id, worker_id=worker_id)
        if "reason_code" in claim:
            return claim
        job, observed_now, snapshot = claim
        worker_id = job["worker_id"]
        plan = snapshot.plans[job["plan_id"]]
        if content_digest(
            {k: v for k, v in plan.items() if k != "plan_digest"}
        ) != plan["plan_digest"]:
            return self._settle_diagnostic(snapshot, job, observed_now, ["PLAN_DIGEST_MISMATCH"])
        runner_request = {
            "schema_version": RUNNER_REQUEST_SCHEMA,
            "checker_artifact": plan["checker_artifact"],
            "run_specs": plan["run_specs"],
            "budget": copy.deepcopy(plan["budget"]),
            "stop_rule": plan["stop_rule"],
        }
        if runner_result is None:
            if self._runner_override is not None:
                runner_result = self._runner_override(runner_request)
            else:
                runner_result = run_s12_runner(runner_request)
        validation = self._validate_runner_result(runner_result, plan)
        if validation is not None:
            return self._settle_diagnostic(
                snapshot, job, observed_now, validation
            )
        assert runner_result is not None
        predictions, errors, missing = self._materialize_predictions(
            plan, runner_result
        )
        stop_observation = runner_result["stop"]
        stop_reason = stop_observation["stop_reason"]
        stop_rule_satisfied = plan["stop_rule"] == "budget-or-plan" or (
            plan["stop_rule"] == "plan-exhausted"
            and stop_reason == "plan-exhausted"
        )
        opportunities = plan["opportunities"]
        by_id = {opportunity["opportunity_id"]: opportunity for opportunity in opportunities}
        track_statistics = {
            track: _cluster_statistics(
                [by_id[oid] for oid in membership["opportunities"]],
                plan["clusters"],
                predictions,
                plan["seed"],
                membership=track,
            )
            for track, membership in plan["tracks"].items()
        }
        view_statistics = {
            view: _cluster_statistics(
                [by_id[oid] for oid in membership["opportunities"]],
                plan["clusters"],
                predictions,
                plan["seed"],
                membership=view,
            )
            for view, membership in plan["views"].items()
        }
        # Formal eligibility and mandatory check families: derived from the
        # frozen manifest, never from caller claims.
        holdout_eligible, holdout_reasons = _holdout_eligibility(
            opportunities, plan["clusters"]
        )
        mandatory_family_statistics: dict[str, dict[str, Any]] = {}
        mandatory_families_ok = True
        family_failures: list[str] = []
        for family in plan["mandatory_check_families"]:
            family_check_ids = set(family["check_ids"])
            family_opportunities = [
                opportunity
                for opportunity in opportunities
                if opportunity["check_id"] in family_check_ids
            ]
            family_statistics = _cluster_statistics(
                family_opportunities,
                plan["clusters"],
                predictions,
                plan["seed"],
                membership=family["family_id"],
            )
            mandatory_family_statistics[family["family_id"]] = family_statistics
            if not family_statistics["estimable"]:
                mandatory_families_ok = False
                family_failures.append(
                    f"{family['family_id']}: "
                    + ",".join(family_statistics["not_estimable_reasons"])
                )
            elif family_statistics["conclusion"] != "pass":
                mandatory_families_ok = False
                family_failures.append(
                    f"{family['family_id']}: mandatory family gates not passing"
                )
        strata: dict[str, dict[str, dict[str, Any]]] = {}
        for group_by in (
            "difficulty",
            "data_source",
            "document_combination",
            "perturbation_family",
        ):
            grouped: dict[str, list[dict[str, Any]]] = {}
            for opportunity in opportunities:
                value = opportunity.get(group_by)
                if isinstance(value, str) and value:
                    grouped.setdefault(value, []).append(opportunity)
            strata[group_by] = {
                value: _cluster_statistics(
                    members,
                    plan["clusters"],
                    predictions,
                    plan["seed"],
                    membership=f"{group_by}:{value}",
                )
                for value, members in sorted(grouped.items())
            }
        status, status_reasons = _select_status(
            track_statistics,
            view_statistics,
            plan["scope"],
            holdout_eligible=holdout_eligible,
            mandatory_families_ok=mandatory_families_ok,
        )
        scope_reasons = list(holdout_reasons)
        scope_reasons.extend(family_failures)
        # Measured business state: capture again immediately before terminal
        # publication.  Any required fact that changed since freeze prevents
        # a formal result and records the exact changed fact.
        business_before = plan.get("business_before") or {}
        business_after = dict(self._business_state_provider())
        changed_facts = sorted(
            key
            for key in business_before
            if key in business_after and business_before[key] != business_after[key]
        )
        if changed_facts:
            return self._settle_diagnostic(
                snapshot,
                job,
                observed_now,
                [f"BUSINESS_AUTHORITY_CHANGED:{key}" for key in changed_facts],
            )
        settled_at = int(self._clock())
        bundle_content = {
            "schema_version": BUNDLE_SCHEMA,
            "bundle_id": None,
            "plan_id": plan["plan_id"],
            "plan_digest": plan["plan_digest"],
            "job_id": job["job_id"],
            "fence": job["fence"],
            "attempt_no": job["attempt_no"],
            "worker_id": worker_id,
            "rerun_of_bundle_id": job.get("rerun_of_bundle_id"),
            "run_started_at": observed_now,
            "run_settled_at": settled_at,
            "status": status,
            "scope": plan["scope"],
            "status_reasons": status_reasons,
            "tracks": track_statistics,
            "views": view_statistics,
            "mandatory_check_families": mandatory_family_statistics,
            "strata": strata,
            "scope_eligibility": {
                "holdout_eligible": holdout_eligible,
                "reasons": sorted(set(scope_reasons)),
            },
            "clusters": copy.deepcopy(plan["clusters"]),
            "opportunities": copy.deepcopy(plan["opportunities"]),
            "tracks_declared": copy.deepcopy(plan["tracks"]),
            "views_declared": copy.deepcopy(plan["views"]),
            "evidence_references": copy.deepcopy(plan["evidence_references"]),
            "label_manifest": copy.deepcopy(plan["label_manifest"]),
            "cohort": copy.deepcopy(plan.get("cohort")),
            "predictions": predictions,
            "prediction_alphabet": list(PREDICTION_ALPHABET),
            "gold_alphabet": list(GOLD_ALPHABET),
            "errors": errors,
            "missing_opportunities": missing,
            "release": copy.deepcopy(plan["release"]),
            "environment": copy.deepcopy(plan["environment"]),
            "stop_rule": plan["stop_rule"],
            "stop_reason": stop_reason,
            "stop_elapsed_ms": stop_observation["elapsed_ms"],
            "completed_application_ids": copy.deepcopy(
                stop_observation["completed_application_ids"]
            ),
            "stop_rule_satisfied": stop_rule_satisfied,
            "evidence_snapshot_ids": sorted(
                {opportunity["evidence_snapshot_id"] for opportunity in opportunities}
            ),
            "seed": plan["seed"],
            "budget": copy.deepcopy(plan["budget"]),
            "split": copy.deepcopy(plan["split"]),
            "business_before": copy.deepcopy(business_before),
            "business_after": copy.deepcopy(business_after),
            "business_deltas": _business_deltas(business_before, business_after),
            "command": f"s12:process:{job['job_id']}",
        }
        result_material = {
            "plan_id": plan["plan_id"],
            "plan_digest": plan["plan_digest"],
            "scope": plan["scope"],
            "seed": plan["seed"],
            "budget": copy.deepcopy(plan["budget"]),
            "stop_rule": plan["stop_rule"],
            "split": copy.deepcopy(plan["split"]),
            "release": copy.deepcopy(plan["release"]),
            "environment": copy.deepcopy(plan["environment"]),
            "evidence_references": copy.deepcopy(plan["evidence_references"]),
            "label_manifest": copy.deepcopy(plan["label_manifest"]),
            "mandatory_check_families": copy.deepcopy(
                plan["mandatory_check_families"]
            ),
            "cohort": copy.deepcopy(plan.get("cohort")),
            "clusters": copy.deepcopy(plan["clusters"]),
            "opportunities": copy.deepcopy(plan["opportunities"]),
            "tracks": copy.deepcopy(plan["tracks"]),
            "views": copy.deepcopy(plan["views"]),
            "predictions": predictions,
            "errors": errors,
            "missing_opportunities": missing,
            "tracks_statistics": track_statistics,
            "views_statistics": view_statistics,
            "mandatory_family_statistics": mandatory_family_statistics,
            "strata": strata,
            "scope_eligibility": {
                "holdout_eligible": holdout_eligible,
                "reasons": sorted(set(scope_reasons)),
            },
            "status": status,
            "status_reasons": status_reasons,
            "stop_reason": stop_reason,
            "completed_application_ids": stop_observation[
                "completed_application_ids"
            ],
            "business_before": copy.deepcopy(business_before),
            "business_after": copy.deepcopy(business_after),
            "business_deltas": _business_deltas(business_before, business_after),
        }
        bundle_content["result_digest"] = content_digest(result_material)
        digest = content_digest(
            {k: v for k, v in bundle_content.items() if k != "bundle_id"}
        )
        bundle_id = f"s12_bundle_sha256_{digest}"
        bundle_content["bundle_id"] = bundle_id
        attempt_record = {
            "schema_version": ATTEMPT_SCHEMA,
            "job_id": job["job_id"],
            "fence": job["fence"],
            "attempt_no": job["attempt_no"],
            "worker_id": worker_id,
            "status": "complete",
            "started_at": observed_now,
            "result": {"bundle_id": bundle_id, "status": status},
        }
        attempt_record["attempt_id"] = self._stable_id(
            "s12attempt", f"{job['job_id']}:{job['fence']}:{job['attempt_no']}"
        )
        prediction_records = {
            f"{job['job_id']}:{opportunity_id}": {
                "schema_version": "s12-prediction/1",
                "job_id": job["job_id"],
                "opportunity_id": opportunity_id,
                "prediction": prediction,
                "plan_id": plan["plan_id"],
            }
            for opportunity_id, prediction in predictions.items()
        }
        published = self._store.publish_bundle_transaction(
            job,
            int(self._clock()),
            bundle=bundle_content,
            predictions=prediction_records,
            attempt=attempt_record,
        )
        if not published:
            return self._settle_stale_attempt(job, observed_now)
        self._store.reload()
        return {
            "status": status,
            "job_id": job["job_id"],
            "bundle_id": bundle_id,
            "attempt_no": job["attempt_no"],
        }

    def _claim_job(
        self, job_id: str, *, worker_id: str | None = None
    ) -> dict[str, Any] | tuple[dict[str, Any], int, _EvalStore]:
        with self._lock:
            for _ in range(2):
                try:
                    result = self._store.claim_job_transaction(
                        job_id,
                        worker_id=worker_id,
                        now=int(self._clock()),
                    )
                except sqlite3.OperationalError:
                    continue
                if not isinstance(result, tuple):
                    return result
                job, observed_now = result
                self._store.reload()
                return copy.deepcopy(job), observed_now, copy.deepcopy(self._store)
            return {
                "status": "blocked",
                "reason_code": "JOB_CLAIM_CONTENTION",
            }

    @staticmethod
    def _owned_job(
        store: _EvalStore, job: dict[str, Any], now: int
    ) -> dict[str, Any] | None:
        current = store.jobs.get(job["job_id"])
        if current is None:
            return None
        if (
            current.get("status") != "leased"
            or current.get("worker_id") != job.get("worker_id")
            or current.get("fence") != job.get("fence")
            or current.get("attempt_no") != job.get("attempt_no")
            or int(current.get("lease_until") or 0) <= now
        ):
            return None
        return current

    def _settle_stale_attempt(
        self, job: dict[str, Any], observed_now: int
    ) -> dict[str, Any]:
        with self._lock:
            self._store.reload()
            attempt = {
                "schema_version": ATTEMPT_SCHEMA,
                "job_id": job["job_id"],
                "fence": job["fence"],
                "attempt_no": job["attempt_no"],
                "worker_id": job["worker_id"],
                "status": "discarded",
                "started_at": observed_now,
                "result": {"reason_code": "STALE_WORKER"},
            }
            attempt["attempt_id"] = self._stable_id(
                "s12attempt",
                f"{job['job_id']}:{job['fence']}:{job['attempt_no']}",
            )
            self._store.settle_stale_attempt_transaction(job, attempt)
        return {
            "status": "stale",
            "job_id": job["job_id"],
            "reason_code": "STALE_WORKER",
        }

    def _settle_diagnostic(
        self,
        snapshot: _EvalStore,
        job: dict[str, Any],
        observed_now: int,
        reasons: list[str],
    ) -> dict[str, Any]:
        with self._lock:
            self._store.reload()
            attempt = {
                "schema_version": ATTEMPT_SCHEMA,
                "job_id": job["job_id"],
                "fence": job["fence"],
                "attempt_no": job["attempt_no"],
                "worker_id": job["worker_id"],
                "status": "failed",
                "started_at": observed_now,
                "result": {"reason_codes": reasons},
            }
            attempt["attempt_id"] = self._stable_id(
                "s12attempt",
                f"{job['job_id']}:{job['fence']}:{job['attempt_no']}",
            )
            settled = self._store.settle_diagnostic_transaction(
                job,
                int(self._clock()),
                reasons=reasons,
                attempt=attempt,
            )
            if not settled:
                return self._settle_stale_attempt(job, observed_now)
        return {
            "status": "INVALID",
            "job_id": job["job_id"],
            "bundle_id": None,
            "reason_codes": reasons,
        }

    def _require_plan(self, plan_id: str) -> dict[str, Any]:
        plan = self._store.plans.get(plan_id)
        if plan is None:
            raise ValueError(f"plan {plan_id} does not exist")
        return plan

    @staticmethod
    def _validate_runner_result(
        runner_result: dict[str, Any] | None, plan: dict[str, Any]
    ) -> list[str] | None:
        """INVALID conditions: malformed, unknown, duplicate, identity or
        digest-mismatched runner output.  Returns the reason list, or None
        when the result is structurally valid."""
        if runner_result is None:
            return ["RUNNER_EXECUTION_FAILED"]
        if not isinstance(runner_result, dict):
            return ["RUNNER_OUTPUT_MALFORMED"]
        if runner_result.get("schema_version") != RUNNER_RESULT_SCHEMA:
            return ["RUNNER_SCHEMA_MISMATCH"]
        declared_digest = runner_result.get("digest")
        if (
            not isinstance(declared_digest, str)
            or len(declared_digest) != 64
            or content_digest(
                {key: value for key, value in runner_result.items() if key != "digest"}
            )
            != declared_digest
        ):
            return ["RUNNER_DIGEST_MISMATCH"]
        stop = runner_result.get("stop")
        if (
            not isinstance(stop, dict)
            or stop.get("stop_reason") not in {"plan-exhausted", "budget-or-plan"}
            or isinstance(stop.get("elapsed_ms"), bool)
            or not isinstance(stop.get("elapsed_ms"), int)
            or stop["elapsed_ms"] < 0
            or not isinstance(stop.get("completed_application_ids"), list)
        ):
            return ["RUNNER_STOP_OBSERVATION_INVALID"]
        if not isinstance(runner_result.get("applications"), list):
            return ["RUNNER_OUTPUT_MALFORMED"]
        known_apps = set(plan["run_specs"])
        seen: set[str] = set()
        for application in runner_result["applications"]:
            if not isinstance(application, dict):
                return ["RUNNER_OUTPUT_MALFORMED"]
            application_id = application.get("application_id")
            if not isinstance(application_id, str) or not application_id:
                return ["RUNNER_OUTPUT_MALFORMED"]
            if application_id in seen:
                return ["RUNNER_DUPLICATE_APPLICATION"]
            seen.add(application_id)
            if application_id not in known_apps:
                return ["RUNNER_UNKNOWN_APPLICATION"]
            if "error" in application:
                if not isinstance(application["error"], str) or not application["error"]:
                    return ["RUNNER_OUTPUT_MALFORMED"]
                continue
            run_spec = plan["run_specs"][application_id]
            if application.get("run_id") != run_spec["run_id"]:
                return ["RUNNER_IDENTITY_MISMATCH"]
            checks = application.get("checks")
            if not isinstance(checks, list):
                return ["RUNNER_OUTPUT_MALFORMED"]
            rule_ids: set[str] = set()
            for check in checks:
                if not isinstance(check, dict):
                    return ["RUNNER_OUTPUT_MALFORMED"]
                rule_id = check.get("rule_id")
                verdict = check.get("verdict")
                if (
                    not isinstance(rule_id, str)
                    or not rule_id
                    or rule_id in rule_ids
                    or not isinstance(verdict, str)
                    or verdict not in {"consistent", "inconsistent", "uncertain", "skipped"}
                ):
                    return ["RUNNER_CHECK_INVALID"]
                rule_ids.add(rule_id)
        completed_ids = stop["completed_application_ids"]
        if set(completed_ids) != seen:
            return ["RUNNER_STOP_OBSERVATION_INVALID"]
        return None

    @staticmethod
    def _materialize_predictions(
        plan: dict[str, Any], runner_result: dict[str, Any]
    ) -> tuple[dict[str, str], list[dict[str, str]], list[str]]:
        """Per-opportunity predictions over the frozen alphabet.  Omitted
        runner output becomes explicit ``missing``; per-known-opportunity
        checker failure becomes ``error``."""
        by_app: dict[str, dict[str, Any]] = {
            application["application_id"]: application
            for application in runner_result["applications"]
        }
        predictions: dict[str, str] = {}
        errors: list[dict[str, str]] = []
        missing: list[str] = []
        for opportunity in plan["opportunities"]:
            opportunity_id = opportunity["opportunity_id"]
            application = by_app.get(opportunity["application_id"])
            if application is None:
                predictions[opportunity_id] = "missing"
                missing.append(opportunity_id)
                continue
            if "error" in application:
                predictions[opportunity_id] = "error"
                errors.append(
                    {
                        "opportunity_id": opportunity_id,
                        "reason_code": application["error"],
                    }
                )
                continue
            verdict = next(
                (
                    check["verdict"]
                    for check in application["checks"]
                    if check["rule_id"] == opportunity["check_id"]
                ),
                None,
            )
            if verdict is None:
                predictions[opportunity_id] = "missing"
                missing.append(opportunity_id)
                continue
            predictions[opportunity_id] = verdict
        return predictions, errors, missing
