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
import random
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

_ZERO_BUSINESS_DELTAS = {
    "lifecycle_revision": 0,
    "evidence_rows": 0,
    "evidence_digest": None,
    "current_run_pointer": 0,
    "policy_revision": 0,
    "governance_revision": 0,
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

    def persist(self) -> None:
        self._ensure_schema()
        with contextlib.closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for table, source in (
                ("s12_plans", self.plans),
                ("s12_jobs", self.jobs),
                ("s12_attempts", self.attempts),
                ("s12_predictions", self.predictions),
                ("s12_bundles", self.bundles),
            ):
                staged: dict[str, tuple[str, str]] = {}
                for item_id, value in source.items():
                    payload = json.dumps(
                        value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    staged[item_id] = (
                        payload,
                        _integrity_digest(table, item_id, payload),
                    )
                if table == "s12_bundles":
                    existing = {
                        row[0]: row[1]
                        for row in connection.execute(
                            f"SELECT item_id, payload FROM {table}"
                        ).fetchall()
                    }
                    for item_id, (payload, _digest) in staged.items():
                        stored = existing.get(item_id)
                        if stored is None:
                            continue
                        if stored == payload:
                            # Byte-identical replay of an already published
                            # bundle: append-only, no write, no error.
                            continue
                        raise S12IntegrityError(
                            "append-only S12 bundle collision: " + item_id
                        )
                    staged = {
                        item_id: (payload, digest)
                        for item_id, (payload, digest) in staged.items()
                        if item_id not in existing
                        or existing[item_id] != payload
                    }
                for item_id, (payload, digest) in staged.items():
                    connection.execute(
                        f"""
                        INSERT INTO {table} (item_id, payload, integrity_sha256)
                        VALUES (?, ?, ?)
                        ON CONFLICT(item_id) DO UPDATE SET
                            payload = excluded.payload,
                            integrity_sha256 = excluded.integrity_sha256
                        """,
                        (item_id, payload, digest),
                    )
            connection.execute("COMMIT")

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
                "SELECT payload FROM s12_jobs WHERE item_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                return {"status": "failed", "reason_code": "JOB_NOT_FOUND"}
            job = json.loads(row[0])
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
) -> tuple[str, list[str]]:
    """Exact status vocabulary: INVALID / INSUFFICIENT / FAIL / PASS(scope=...)
    / SMOKE_ONLY.  Formal unscoped PASS is prohibited; PASS always carries the
    frozen plan scope."""
    active = [
        item
        for item in (*track_statistics.values(), *view_statistics.values())
        if item["opportunity_count"] > 0
    ]
    if not active:
        return "SMOKE_ONLY", ["no eligible C/I gold opportunities"]
    if sum(item["denominators"]["E"] for item in active) == 0:
        return "SMOKE_ONLY", ["no eligible C/I gold opportunities"]
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


# ---------------------------------------------------------------------------
# EvaluationService
# ---------------------------------------------------------------------------


class EvaluationService:
    """The concrete deep S12 evaluation authority: freeze/start/cancel/
    process/query/rerun over the evaluation-owned SQLite store."""

    def __init__(
        self,
        *,
        state_path: str | Path,
        clock: Callable[[], int] | None = None,
        runner_override: Callable[[dict[str, Any]], dict[str, Any] | None]
        | None = None,
    ) -> None:
        self._store = _EvalStore(state_path)
        self._clock = clock or (lambda: int(time.time()))
        self._runner_override = runner_override
        self._lock = threading.RLock()
        self._job_sequence = 0

    @staticmethod
    def _stable_id(prefix: str, fingerprint: str) -> str:
        return f"{prefix}_{hashlib.sha256(fingerprint.encode()).hexdigest()[:24]}"

    # -- plan freeze -------------------------------------------------------

    def freeze_plan(self, command: dict[str, Any]) -> dict[str, Any]:
        validated = self._validate_plan_command(command)
        plan = {
            "schema_version": PLAN_SCHEMA,
            "plan_id": validated["plan_id"],
            "scope": validated["scope"],
            "seed": validated["seed"],
            "budget": copy.deepcopy(validated["budget"]),
            "stop_rule": validated["stop_rule"],
            "split": copy.deepcopy(validated["split"]),
            "environment": copy.deepcopy(validated["environment"]),
            "release": copy.deepcopy(validated["release"]),
            "checker_artifact": copy.deepcopy(validated["checker_artifact"]),
            "run_specs": copy.deepcopy(validated["run_specs"]),
            "clusters": copy.deepcopy(validated["clusters"]),
            "tracks": copy.deepcopy(validated["tracks"]),
            "views": copy.deepcopy(validated["views"]),
            "opportunities": copy.deepcopy(validated["opportunities"]),
            "frozen_at": int(self._clock()),
        }
        digest = content_digest({k: v for k, v in plan.items() if k != "plan_digest"})
        plan["plan_digest"] = digest
        with self._lock:
            self._store.reload()
            if plan["plan_id"] in self._store.plans:
                existing = self._store.plans[plan["plan_id"]]
                if existing != plan:
                    raise ValueError(
                        f"plan {plan['plan_id']} already frozen with different content"
                    )
            else:
                self._store.plans[plan["plan_id"]] = plan
                self._store.persist()
        return plan

    def _validate_plan_command(self, command: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(command, dict):
            raise ValueError("plan command must be an object")
        if command.get("schema_version") != PLAN_COMMAND_SCHEMA:
            raise ValueError("plan command schema mismatch")
        plan_id = command.get("plan_id")
        scope = command.get("scope")
        if not isinstance(plan_id, str) or not plan_id:
            raise ValueError("plan_id is required")
        if not isinstance(scope, str) or not scope:
            raise ValueError("scope is required")
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
            raise ValueError("budget requires positive max_opportunities and max_runtime_ms")
        stop_rule = command.get("stop_rule")
        if not isinstance(stop_rule, str) or not stop_rule:
            raise ValueError("stop_rule is required")
        if stop_rule not in {"plan-exhausted", "budget-or-plan"}:
            raise ValueError("stop_rule must be plan-exhausted or budget-or-plan")
        split = command.get("split")
        if not isinstance(split, dict) or not isinstance(split.get("scheme"), str):
            raise ValueError("split scheme is required")
        usage_partitions = split.get("usage_partitions")
        if (
            not isinstance(usage_partitions, list)
            or set(usage_partitions) != {
                "development",
                "calibration",
                "acceptance_holdout",
            }
        ):
            raise ValueError(
                "split usage partitions must be exactly development, "
                "calibration, acceptance_holdout"
            )
        environment = command.get("environment")
        if (
            not isinstance(environment, dict)
            or not isinstance(environment.get("python"), str)
            or not environment["python"]
        ):
            raise ValueError("environment must pin a non-empty python identity")
        release = command.get("release")
        if not isinstance(release, dict):
            raise ValueError("release pin is required")
        for key in ("release_id", "release_digest", "checker_build", "limits"):
            if not isinstance(release.get(key), (str, dict, tuple, list)):
                raise ValueError(f"release pin is missing {key}")
        checker_artifact = command.get("checker_artifact")
        if not isinstance(checker_artifact, dict):
            raise ValueError("checker_artifact is required")
        run_specs = command.get("run_specs")
        if not isinstance(run_specs, dict) or not run_specs:
            raise ValueError("run_specs are required")
        for application_id, run_spec in run_specs.items():
            if (
                not isinstance(application_id, str)
                or not application_id
                or not isinstance(run_spec, dict)
                or str(run_spec.get("application_id") or "") != application_id
                or run_spec.get("release_digest") != release["release_digest"]
            ):
                raise ValueError(
                    f"run_spec for {application_id} is invalid or release-mismatched"
                )
        clusters = command.get("clusters")
        if not isinstance(clusters, list) or not clusters:
            raise ValueError("clusters are required")
        cluster_ids: set[str] = set()
        for cluster in clusters:
            if (
                not isinstance(cluster, dict)
                or not isinstance(cluster.get("cluster_id"), str)
                or cluster["cluster_id"] in cluster_ids
                or not isinstance(cluster.get("applications"), list)
                or not cluster["applications"]
                or not isinstance(cluster.get("usage"), str)
                or cluster["usage"] not in usage_partitions
            ):
                raise ValueError(
                    "clusters must have unique ids, applications and a "
                    "declared usage partition"
                )
            cluster_ids.add(cluster["cluster_id"])
        opportunities = command.get("opportunities")
        if not isinstance(opportunities, list) or not opportunities:
            raise ValueError("opportunities are required")
        opportunity_ids: set[str] = set()
        app_ids = set(run_specs)
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
                "label",
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
            if opportunity["application_id"] not in app_ids:
                raise ValueError("opportunity application has no run_spec")
            if opportunity["label"] not in GOLD_ALPHABET:
                raise ValueError("opportunity gold label is outside the gold alphabet")
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

        track_members = {track: _membership_ids(collection, track) for track, collection in tracks.items()}
        view_members = {view: _membership_ids(collection, view) for view, collection in views.items()}
        if track_members["R"] & track_members["C"]:
            raise ValueError("R and C tracks share opportunities")
        declared = {
            opportunity["opportunity_id"]
            for opportunity in opportunities
            if opportunity["track"] == "R"
        }
        if track_members["R"] != declared:
            raise ValueError("R track membership disagrees with opportunity tracks")
        if view_members["R-E2E"] & view_members["R-T4-conditional"]:
            raise ValueError("R-E2E and R-T4-conditional views share opportunities")
        for view, member_ids in view_members.items():
            if not member_ids <= track_members["R"]:
                raise ValueError(f"{view} view must be a subset of the R track")
        return {
            "plan_id": plan_id,
            "scope": scope,
            "seed": seed,
            "budget": budget,
            "stop_rule": stop_rule,
            "split": split,
            "environment": environment,
            "release": release,
            "checker_artifact": checker_artifact,
            "run_specs": run_specs,
            "clusters": clusters,
            "tracks": tracks,
            "views": views,
            "opportunities": opportunities,
        }

    # -- job lifecycle ------------------------------------------------------

    def start_job(self, plan_id: str, worker_id: str) -> dict[str, Any]:
        with self._lock:
            self._store.reload()
            plan = self._require_plan(plan_id)
            self._job_sequence = max(self._job_sequence, len(self._store.jobs)) + 1
            job_id = self._stable_id(
                "s12job", f"{plan_id}:{worker_id}:{self._job_sequence}"
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
            self._store.jobs[job_id] = job
            self._store.persist()
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
            self._job_sequence = max(self._job_sequence, len(self._store.jobs)) + 1
            rerun_job_id = self._stable_id(
                "s12job",
                f"{source['plan_id']}:{worker_id}:{self._job_sequence}",
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
            self._store.jobs[rerun_job_id] = job
            self._store.persist()
            return job

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            self._store.reload()
            job = self._store.jobs.get(job_id)
            if job is None:
                raise ValueError(f"job {job_id} does not exist")
            if job["status"] in {"complete", "cancelled", "diagnostic"}:
                return job
            job["status"] = "cancelled"
            job["lease_until"] = None
            self._store.persist()
            return job

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
        status, status_reasons = _select_status(
            track_statistics, view_statistics, plan["scope"]
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
            "predictions": predictions,
            "prediction_alphabet": list(PREDICTION_ALPHABET),
            "gold_alphabet": list(GOLD_ALPHABET),
            "errors": errors,
            "missing_opportunities": missing,
            "release": copy.deepcopy(plan["release"]),
            "environment": copy.deepcopy(plan["environment"]),
            "stop_rule_satisfied": True,
            "evidence_snapshot_ids": sorted(
                {opportunity["evidence_snapshot_id"] for opportunity in opportunities}
            ),
            "seed": plan["seed"],
            "budget": copy.deepcopy(plan["budget"]),
            "stop_rule": plan["stop_rule"],
            "split": copy.deepcopy(plan["split"]),
            "business_deltas": dict(_ZERO_BUSINESS_DELTAS),
            "command": f"s12:process:{job['job_id']}",
        }
        digest = content_digest(
            {k: v for k, v in bundle_content.items() if k != "bundle_id"}
        )
        bundle_id = f"s12_bundle_sha256_{digest}"
        bundle_content["bundle_id"] = bundle_id
        with self._lock:
            self._store.reload()
            current = self._owned_job(self._store, job, int(self._clock()))
            if current is None:
                return self._settle_stale_attempt(job, observed_now)
            current["status"] = "complete"
            current.pop("lease_until", None)
            current["result"] = {
                "bundle_id": bundle_id,
                "status": status,
                "settled_at": settled_at,
            }
            self._store.bundles[bundle_id] = bundle_content
            for opportunity_id, prediction in predictions.items():
                self._store.predictions[
                    f"{job['job_id']}:{opportunity_id}"
                ] = {
                    "schema_version": "s12-prediction/1",
                    "job_id": job["job_id"],
                    "opportunity_id": opportunity_id,
                    "prediction": prediction,
                    "plan_id": plan["plan_id"],
                }
            self._store.attempts[
                self._stable_id("s12attempt", f"{job['job_id']}:{job['fence']}:{job['attempt_no']}")
            ] = {
                "schema_version": ATTEMPT_SCHEMA,
                "job_id": job["job_id"],
                "fence": job["fence"],
                "attempt_no": job["attempt_no"],
                "worker_id": worker_id,
                "status": "complete",
                "started_at": observed_now,
                "result": {"bundle_id": bundle_id, "status": status},
            }
            self._store.persist()
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
            self._store.attempts[
                self._stable_id("s12attempt", f"{job['job_id']}:{job['fence']}:{job['attempt_no']}")
            ] = {
                "schema_version": ATTEMPT_SCHEMA,
                "job_id": job["job_id"],
                "fence": job["fence"],
                "attempt_no": job["attempt_no"],
                "worker_id": job["worker_id"],
                "status": "discarded",
                "started_at": observed_now,
                "result": {"reason_code": "STALE_WORKER"},
            }
            self._store.persist()
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
            current = self._owned_job(self._store, job, int(self._clock()))
            if current is None:
                return self._settle_stale_attempt(job, observed_now)
            current["status"] = "diagnostic"
            current.pop("lease_until", None)
            current["reason_codes"] = reasons
            current["result"] = {
                "bundle_id": None,
                "status": "INVALID",
                "reason_codes": reasons,
            }
            self._store.attempts[
                self._stable_id("s12attempt", f"{job['job_id']}:{job['fence']}:{job['attempt_no']}")
            ] = {
                "schema_version": ATTEMPT_SCHEMA,
                "job_id": job["job_id"],
                "fence": job["fence"],
                "attempt_no": job["attempt_no"],
                "worker_id": job["worker_id"],
                "status": "failed",
                "started_at": observed_now,
                "result": {"reason_codes": reasons},
            }
            self._store.persist()
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
