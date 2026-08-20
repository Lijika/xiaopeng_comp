"""Ticket #28 S12 — isolated and honest formal evaluation (controlled).

S12 runs a frozen evaluation plan through a restricted subprocess runner over
the existing pure ``TargetChecker``, materializes explicit missing/error
predictions, aggregates R/C and view statistics with fixed-seed cluster
bootstrap, and seals an immutable content-addressed bundle.  It never
touches the S01 business database: business lifecycle/evidence/current-run/
policy/governance revisions stay at delta zero across freeze, execution,
query, restart, and rerun.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from task4_consistency.controlled.s01 import (
    AdmissionDisposition,
    ControlledScenarioService,
    S01CommandPrincipal,
)
from task4_consistency.controlled.s01_checker import TargetRelease
from task4_consistency.controlled.s12 import EvaluationService, content_digest
from task4_consistency.controlled.s12 import (
    _clopper_pearson_upper,
    _cluster_statistics,
    _select_status,
)
from task4_consistency.controlled.s12_runner import run_s12_runner
from task4_consistency.kb.store import get_kb
from task4_consistency.rules.loader import load_rules

ROOT = Path(__file__).resolve().parents[1]

TEST_INTEGRATOR = S01CommandPrincipal(
    subject="registered-test-integrator",
    role="integrator",
    scope="C-DEMO",
    source_id="s12-test-client",
)

_ZERO_BUSINESS_DELTAS = {
    "lifecycle_revision": 0,
    "evidence_rows": 0,
    "evidence_digest": None,
    "current_run_pointer": 0,
    "policy_revision": 0,
    "governance_revision": 0,
}


def _make_business_baseline(
    tmp_path: Path, rules_path: Path
) -> tuple[Path, str]:
    """A real S01 business database with admitted state, plus the SHA-256 of
    its bytes before any S12 operation."""
    business_path = tmp_path / "business.sqlite3"
    business = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=rules_path,
        state_path=business_path,
    )
    admitted = business.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=f"s12-business-{business_path.name}",
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    assert business.fact_counts()["applications"] == 1
    return business_path, hashlib.sha256(business_path.read_bytes()).hexdigest()


def _eligible_field(raw: str, *, observation_id: str, source_region: str) -> dict[str, object]:
    return {
        "raw": raw,
        "confidence": 0.99,
        "observation_id": observation_id,
        "source_object_ref": "c-demo-object:sha256:test-source",
        "source_sha256": "test-source",
        "provenance_manifest_digest": "f" * 64,
        "source_page": 1,
        "source_region": source_region,
        "evidence_eligible": True,
        "eligibility_reason": "SYNTHETIC_SOURCE_VERIFIED",
    }


def _plate_documents(
    plate_no: str, *, second_role: bool, second_plate_no: str | None = None
) -> list[dict[str, object]]:
    """R_PLATE_CROSS evidence: 机动车登记证书 always present; the 交强险保单
    is optional so a missing document deterministically yields ``uncertain``.
    ``second_plate_no`` lets the second document disagree with the first for
    a deterministic ``inconsistent`` verdict."""
    documents = [
        {
            "document_id": "doc-reg",
            "document_role": "机动车登记证书",
            "fields": {
                "plate_no": _eligible_field(
                    plate_no,
                    observation_id="obs-plate-reg",
                    source_region="/documents/reg/fields/plate_no",
                )
            },
        }
    ]
    if second_role:
        documents.append(
            {
                "document_id": "doc-ins",
                "document_role": "交强险保单",
                "fields": {
                    "plate_no": _eligible_field(
                    plate_no if second_plate_no is None else second_plate_no,
                        observation_id="obs-plate-ins",
                        source_region="/documents/ins/fields/plate_no",
                    )
                },
            }
        )
    return documents


def _complete_run_spec(
    release: TargetRelease,
    evidence: list[dict[str, object]],
    *,
    application_id: str,
) -> dict[str, Any]:
    snapshot = {
        "schema_version": "s01-evidence-snapshot/1",
        "evidence": evidence,
    }
    snapshot_bytes = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()
    manifest = release.public_manifest()
    return {
        "run_id": f"run_{application_id}",
        "application_id": application_id,
        "cycle": 1,
        "lifecycle_revision": 4,
        "evidence_snapshot_id": f"snapshot_sha256_{snapshot_digest}",
        "evidence_snapshot_digest": snapshot_digest,
        "evidence_snapshot": snapshot,
        "evidence_revision": 1,
        "evidence_readiness_policy": "c-demo-readiness/1",
        "baseline_release": copy.deepcopy(manifest),
        "release_id": manifest["release_id"],
        "release_digest": manifest["digest"],
        "checker_build": manifest["checker_build"],
        "fence": 1,
        "limits": copy.deepcopy(manifest["limits"]),
        "applicable_check_ids": manifest["applicable_check_ids"],
        "applicable_check_count": manifest["applicable_check_count"],
    }


def _small_c_plan_command(
    release: TargetRelease,
    run_specs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    manifest = release.public_manifest()
    opportunities = [
        {
            "opportunity_id": "opp-0",
            "track": "C",
            "cluster": "cl-0",
            "application_id": "app-0",
            "cycle": 1,
            "check_id": "R_PLATE_CROSS",
            "target_scope": "C",
            "evidence_snapshot_id": run_specs["app-0"]["evidence_snapshot_id"],
            "label": "consistent",
        },
        {
            "opportunity_id": "opp-1",
            "track": "C",
            "cluster": "cl-1",
            "application_id": "app-1",
            "cycle": 1,
            "check_id": "R_PLATE_CROSS",
            "target_scope": "C",
            "evidence_snapshot_id": run_specs["app-1"]["evidence_snapshot_id"],
            "label": "inconsistent",
        },
        {
            "opportunity_id": "opp-2",
            "track": "C",
            "cluster": "cl-2",
            "application_id": "app-2",
            "cycle": 1,
            "check_id": "R_PLATE_CROSS",
            "target_scope": "C",
            "evidence_snapshot_id": run_specs["app-2"]["evidence_snapshot_id"],
            "label": "consistent",
        },
        {
            "opportunity_id": "opp-3",
            "track": "C",
            "cluster": "cl-3",
            "application_id": "app-3",
            "cycle": 1,
            "check_id": "R_PLATE_CROSS",
            "target_scope": "C",
            "evidence_snapshot_id": run_specs["app-3"]["evidence_snapshot_id"],
            "label": "inconsistent",
        },
    ]
    return {
        "schema_version": "s12-plan-command/1",
        "plan_id": "plan-c-1",
        "scope": "C",
        "seed": 20260820,
        "budget": {"max_opportunities": 10, "max_runtime_ms": 5000},
        "stop_rule": "plan-exhausted",
        "split": {
            "scheme": "cluster_usage_partition",
            "usage_partitions": [
                "development",
                "calibration",
                "acceptance_holdout",
            ],
        },
        "environment": {
            "python": "3.12",
            "schema_version": "s12-evaluation-plan/1",
        },
        "release": {
            "release_id": manifest["release_id"],
            "release_digest": manifest["digest"],
            "checker_build": manifest["checker_build"],
            "limits": copy.deepcopy(manifest["limits"]),
        },
        "checker_artifact": release.to_artifact(),
        "run_specs": copy.deepcopy(run_specs),
        "clusters": [
            {
                "cluster_id": "cl-0",
                "stratum": "c",
                "applications": ["app-0"],
                "usage": "development",
            },
            {
                "cluster_id": "cl-1",
                "stratum": "c",
                "applications": ["app-1"],
                "usage": "development",
            },
            {
                "cluster_id": "cl-2",
                "stratum": "c",
                "applications": ["app-2"],
                "usage": "development",
            },
            {
                "cluster_id": "cl-3",
                "stratum": "c",
                "applications": ["app-3"],
                "usage": "development",
            },
        ],
        "tracks": {
            "R": {"opportunities": []},
            "C": {"opportunities": ["opp-0", "opp-1", "opp-2", "opp-3"]},
        },
        "views": {
            "R-E2E": {"opportunities": []},
            "R-T4-conditional": {"opportunities": []},
        },
        "opportunities": opportunities,
    }


def test_frozen_run_is_isolated_insufficient_replayable_and_rerunnable(
    tmp_path: Path,
) -> None:
    """The first S12 vertical slice: real subprocess + TargetChecker.run on a
    frozen C plan, one omitted runner result materialized as ``missing`` with
    a retained denominator, INSUFFICIENT status, canonical SHA-256 bundle
    bytes, zero business deltas, restart replay, and linked non-overwriting
    rerun."""
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    release = TargetRelease.compile(
        load_rules(rules_path),
        hashlib.sha256(rules_path.read_bytes()).hexdigest(),
        knowledge=get_kb().to_dict(),
    )

    # A real S01 business database with admitted state: S12 must never change it.
    business_path, business_before = _make_business_baseline(tmp_path, rules_path)

    # Frozen evidence: app-0 consistent, app-1 inconsistent, app-2 uncertain
    # (missing 交强险保单 document), app-3 kept out of the runner result.
    run_specs = {
        "app-0": _complete_run_spec(
            release, _plate_documents("苏A92054", second_role=True), application_id="app-0"
        ),
        "app-1": _complete_run_spec(
            release,
            _plate_documents("苏A92054", second_role=True, second_plate_no="苏A92055"),
            application_id="app-1",
        ),
        "app-2": _complete_run_spec(
            release, _plate_documents("苏A92054", second_role=False), application_id="app-2"
        ),
        "app-3": _complete_run_spec(
            release, _plate_documents("苏A92054", second_role=True), application_id="app-3"
        ),
    }

    eval_path = tmp_path / "evaluation.sqlite3"
    service = EvaluationService(state_path=eval_path, clock=lambda: 1700000000)

    # Freeze: one plan owns cohorts, splits, opportunities, gold labels,
    # evidence snapshot, release/checker/build identities, seed, budget,
    # stop rule, and the runner projection.
    plan = service.freeze_plan(_small_c_plan_command(release, run_specs))
    assert plan["plan_id"] == "plan-c-1"
    assert plan["plan_digest"] == content_digest({k: v for k, v in plan.items() if k != "plan_digest"})

    job = service.start_job("plan-c-1", worker_id="s12-test-worker")
    assert job["status"] == "queued"
    assert job["fence"] == 0
    assert job["attempt_no"] == 0

    # Real restricted subprocess invoking the existing pure checker.  The
    # runner projection carries no gold labels: only the checker artifact and
    # the frozen RunSpecs.
    projection = {
        "schema_version": "s12-runner-request/1",
        "checker_artifact": release.to_artifact(),
        "run_specs": copy.deepcopy(run_specs),
        "budget": {"max_opportunities": 10, "max_runtime_ms": 5000},
    }
    runner_output = run_s12_runner(projection)
    assert runner_output is not None
    returned = [item["application_id"] for item in runner_output["applications"]]
    assert set(returned) == {"app-0", "app-1", "app-2", "app-3"}
    by_app = {item["application_id"]: item for item in runner_output["applications"]}
    assert {
        check["rule_id"]: check["verdict"]
        for check in by_app["app-0"]["checks"]
        if check["rule_id"] == "R_PLATE_CROSS"
    }["R_PLATE_CROSS"] == "consistent"
    assert {
        check["rule_id"]: check["verdict"]
        for check in by_app["app-1"]["checks"]
        if check["rule_id"] == "R_PLATE_CROSS"
    }["R_PLATE_CROSS"] == "inconsistent"
    assert {
        check["rule_id"]: check["verdict"]
        for check in by_app["app-2"]["checks"]
        if check["rule_id"] == "R_PLATE_CROSS"
    }["R_PLATE_CROSS"] == "uncertain"

    # Omit app-3 from the runner result: the parent must materialize an
    # explicit ``missing`` prediction and keep the opportunity in its
    # denominator.
    runner_output["applications"] = [
        item
        for item in runner_output["applications"]
        if item["application_id"] != "app-3"
    ]

    outcome = service.process_job(job["job_id"], runner_result=runner_output)
    assert outcome["status"] == "INSUFFICIENT"
    bundle_id = outcome["bundle_id"]
    assert bundle_id.startswith("s12_bundle_sha256_")

    bundle = service.query_bundle(bundle_id)
    assert bundle["status"] == "INSUFFICIENT"
    assert bundle["plan_id"] == "plan-c-1"
    assert bundle["job_id"] == job["job_id"]
    assert bundle["rerun_of_bundle_id"] is None
    assert bundle["predictions"] == {
        "opp-0": "consistent",
        "opp-1": "inconsistent",
        "opp-2": "uncertain",
        "opp-3": "missing",
    }
    c_stats = bundle["tracks"]["C"]
    assert c_stats["denominators"] == {
        "E": 4,
        "n_consistent": 2,
        "n_inconsistent": 2,
    }
    # Every frozen C/I opportunity remains in the denominator: the omitted
    # runner output did not shrink E, N_C or N_I.
    assert c_stats["point"]["coverage"] == 0.5
    assert c_stats["point"]["miss_rate"] == 0.5

    # Canonical SHA-256 bundle bytes: bundle_id is the digest of the canonical
    # complete bundle content.
    canonical_without_id = {
        key: value for key, value in bundle.items() if key != "bundle_id"
    }
    assert bundle["bundle_id"] == "s12_bundle_sha256_" + content_digest(
        canonical_without_id
    )

    # Zero business deltas: structurally recorded in the bundle and provably
    # zero at the business database byte level.
    assert bundle["business_deltas"] == _ZERO_BUSINESS_DELTAS
    business_after = hashlib.sha256(business_path.read_bytes()).hexdigest()
    assert business_after == business_before

    # Restart evaluation storage: the same bytes and digest must replay.
    service2 = EvaluationService(state_path=eval_path, clock=lambda: 1700000000)
    assert service2.query_bundle(bundle_id) == bundle

    # Linked non-overwriting rerun: a new job and a new bundle that reference
    # the source bundle; the source bundle stays byte-identical.
    rerun_job = service2.rerun_job(job["job_id"], worker_id="s12-test-worker")
    assert rerun_job["rerun_of_bundle_id"] == bundle_id
    rerun_outcome = service2.process_job(rerun_job["job_id"], runner_result=runner_output)
    assert rerun_outcome["bundle_id"] != bundle_id
    rerun_bundle = service2.query_bundle(rerun_outcome["bundle_id"])
    assert rerun_bundle["rerun_of_bundle_id"] == bundle_id
    assert service2.query_bundle(bundle_id) == bundle
    assert hashlib.sha256(business_path.read_bytes()).hexdigest() == business_before


# ---------------------------------------------------------------------------
# Statistics: degeneracy fallback, cluster minima, estimability, statuses
# ---------------------------------------------------------------------------


def _synthetic_track(
    *,
    consistent_clusters: int,
    inconsistent_clusters: int,
    predictions: str = "correct",
    gold_eligible: bool = True,
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, str]]:
    """Synthetic per-cluster opportunities.  ``predictions``: "correct" maps
    every consistent gold to consistent and every inconsistent gold to
    inconsistent; "wrong" flips them (gold consistent predicted
    inconsistent and vice versa)."""
    opportunities: list[dict[str, object]] = []
    clusters: list[dict[str, object]] = []
    predictions: dict[str, str]
    labels: dict[str, str] = {}
    for index in range(consistent_clusters):
        cluster_id = f"cl-c-{index}"
        opportunity_id = f"opp-c-{index}"
        opportunities.append(
            {
                "opportunity_id": opportunity_id,
                "track": "C",
                "cluster": cluster_id,
                "label": "consistent",
            }
        )
        clusters.append({"cluster_id": cluster_id, "stratum": "c"})
        labels[opportunity_id] = "consistent"
    for index in range(inconsistent_clusters):
        cluster_id = f"cl-i-{index}"
        opportunity_id = f"opp-i-{index}"
        opportunities.append(
            {
                "opportunity_id": opportunity_id,
                "track": "C",
                "cluster": cluster_id,
                "label": "inconsistent",
            }
        )
        clusters.append({"cluster_id": cluster_id, "stratum": "c"})
        labels[opportunity_id] = "inconsistent"
    if not gold_eligible:
        for opportunity in opportunities:
            opportunity["label"] = "indeterminate"
    if predictions == "wrong":
        flipped = {
            opportunity_id: (
                "inconsistent" if label == "consistent" else "consistent"
            )
            for opportunity_id, label in labels.items()
        }
        predictions = flipped
    else:
        predictions = dict(labels)
    return opportunities, clusters, predictions


def test_zero_error_degeneracy_uses_conservative_clopper_pearson_bound() -> None:
    """Zero-error degeneracy falls back to the conservative one-sided 95%
    Clopper-Pearson upper bound over independent clusters, and the more
    conservative (larger) bound wins over the bootstrap bound."""
    opportunities, clusters, predictions = _synthetic_track(
        consistent_clusters=60, inconsistent_clusters=100
    )
    stats = _cluster_statistics(
        opportunities, clusters, predictions, seed=11, membership="C"
    )
    assert stats["estimable"] is True
    assert stats["conclusion"] == "pass"
    bounds = stats["bounds_95_one_sided"]
    # FPR/FNR/miss point estimates are zero: the bootstrap bound is zero and
    # the Clopper-Pearson bound over the exposed clusters is conservative.
    assert bounds["false_positive_rate_upper"] == round(
        max(
            0.0,
            _clopper_pearson_upper(0, 60),
        ),
        6,
    )
    assert bounds["false_negative_rate_upper"] == round(
        max(0.0, _clopper_pearson_upper(0, 100)), 6
    )
    assert bounds["miss_rate_upper"] == round(
        max(0.0, _clopper_pearson_upper(0, 100)), 6
    )
    # Two-sided interval covers the point estimate.
    interval = stats["interval_95_two_sided"]
    assert interval["coverage"][0] <= 1.0 <= interval["coverage"][1]


def test_below_minimum_clusters_is_not_estimable() -> None:
    """Independent cluster minima: 59 consistent / 99 inconsistent.  A zero-
    error estimate below the minimum for its class is ``not estimable``."""
    opportunities, clusters, predictions = _synthetic_track(
        consistent_clusters=58, inconsistent_clusters=100
    )
    stats = _cluster_statistics(
        opportunities, clusters, predictions, seed=12, membership="C"
    )
    assert stats["estimable"] is False
    assert any(
        "false_positive_rate: independent consistent clusters 58 < 59"
        in reason
        for reason in stats["not_estimable_reasons"]
    )
    assert stats["conclusion"] == "insufficient"


def test_insufficient_valid_resamples_is_not_estimable() -> None:
    """When resampling keeps dropping a required class, the metric is
    ``not estimable`` (valid replicates below 95%)."""
    opportunities, clusters, predictions = _synthetic_track(
        consistent_clusters=1, inconsistent_clusters=1
    )
    stats = _cluster_statistics(
        opportunities, clusters, predictions, seed=13, membership="C"
    )
    assert stats["estimable"] is False
    assert any(
        "valid_resamples" in reason
        for reason in stats["not_estimable_reasons"]
    )


def test_clopper_pearson_boundaries_match_pinned_minima() -> None:
    """The documented 59/99 cluster minima are the exact boundary where the
    one-sided 95% Clopper-Pearson upper bound crosses the FPR/FNR gates."""
    assert _clopper_pearson_upper(0, 59) <= 0.05
    assert _clopper_pearson_upper(0, 58) > 0.05
    assert _clopper_pearson_upper(0, 99) <= 0.03
    assert _clopper_pearson_upper(0, 98) > 0.03
    # Conservative monotonicity: more error clusters raise the bound.
    assert _clopper_pearson_upper(1, 100) > _clopper_pearson_upper(0, 100)


def test_select_status_smoke_only_without_eligible_gold() -> None:
    """No reliable gold (zero eligible C/I opportunities) is SMOKE_ONLY, and
    an empty plane is also SMOKE_ONLY."""
    opportunities, clusters, predictions = _synthetic_track(
        consistent_clusters=4, inconsistent_clusters=4, gold_eligible=False
    )
    stats = _cluster_statistics(
        opportunities, clusters, predictions, seed=14, membership="C"
    )
    status, reasons = _select_status(
        {"R": _empty_stats("R"), "C": stats},
        {"R-E2E": _empty_stats("R-E2E"), "R-T4-conditional": _empty_stats("R-T4-conditional")},
        scope="C",
    )
    assert status == "SMOKE_ONLY"
    assert any("eligible C/I gold" in reason for reason in reasons)


def _empty_stats(membership: str) -> dict[str, object]:
    return _cluster_statistics([], [], {}, seed=1, membership=membership)


def test_select_status_pass_is_always_scoped_and_invalid_insufficient_fail_map() -> None:
    """Formal PASS is prohibited unscoped: the exact status is
    PASS(scope=...).  Missing classes map to INSUFFICIENT; failing gates to
    FAIL."""
    opportunities, clusters, predictions = _synthetic_track(
        consistent_clusters=60, inconsistent_clusters=100
    )
    passing = _cluster_statistics(
        opportunities, clusters, predictions, seed=15, membership="C"
    )
    status, _reasons = _select_status(
        {"R": _empty_stats("R"), "C": passing},
        {"R-E2E": _empty_stats("R-E2E"), "R-T4-conditional": _empty_stats("R-T4-conditional")},
        scope="C",
    )
    assert status == "PASS(scope=C)"

    wrong_opportunities, wrong_clusters, wrong_predictions = _synthetic_track(
        consistent_clusters=60, inconsistent_clusters=100, predictions="wrong"
    )
    failing = _cluster_statistics(
        wrong_opportunities, wrong_clusters, wrong_predictions, seed=16, membership="C"
    )
    status, _reasons = _select_status(
        {"R": _empty_stats("R"), "C": failing},
        {"R-E2E": _empty_stats("R-E2E"), "R-T4-conditional": _empty_stats("R-T4-conditional")},
        scope="C",
    )
    assert status == "FAIL"

    small_opportunities, small_clusters, small_predictions = _synthetic_track(
        consistent_clusters=2, inconsistent_clusters=2
    )
    insufficient = _cluster_statistics(
        small_opportunities, small_clusters, small_predictions, seed=17, membership="C"
    )
    status, _reasons = _select_status(
        {"R": _empty_stats("R"), "C": insufficient},
        {"R-E2E": _empty_stats("R-E2E"), "R-T4-conditional": _empty_stats("R-T4-conditional")},
        scope="C",
    )
    assert status == "INSUFFICIENT"


# ---------------------------------------------------------------------------
# Durable cancellation and lease/fence discipline
# ---------------------------------------------------------------------------


def _runner_projection(release: TargetRelease, run_specs: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "s12-runner-request/1",
        "checker_artifact": release.to_artifact(),
        "run_specs": copy.deepcopy(run_specs),
        "budget": {"max_opportunities": 10, "max_runtime_ms": 5000},
    }


def test_cancel_between_start_and_process_is_durable_and_zero_delta(
    tmp_path: Path,
) -> None:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    release = TargetRelease.compile(
        load_rules(rules_path),
        hashlib.sha256(rules_path.read_bytes()).hexdigest(),
        knowledge=get_kb().to_dict(),
    )
    business_path, business_before = _make_business_baseline(tmp_path, rules_path)
    run_specs = {
        f"app-{index}": _complete_run_spec(
            release, _plate_documents("苏A92054", second_role=True), application_id=f"app-{index}"
        )
        for index in range(4)
    }
    plan_command = _small_c_plan_command(release, run_specs)
    # Keep the plan within budget for a single-application plan.
    plan_command["plan_id"] = "plan-c-cancel"
    plan_command["budget"] = {"max_opportunities": 4, "max_runtime_ms": 5000}
    plan_command["opportunities"] = [
        opportunity
        for opportunity in plan_command["opportunities"]
        if opportunity["application_id"] == "app-0"
    ]
    plan_command["tracks"]["C"] = {"opportunities": ["opp-0"]}
    plan_command["clusters"] = [
        cluster
        for cluster in plan_command["clusters"]
        if cluster["cluster_id"] == "cl-0"
    ]
    plan_command["run_specs"] = {
        application_id: run_spec
        for application_id, run_spec in run_specs.items()
        if application_id == "app-0"
    }

    eval_path = tmp_path / "evaluation-cancel.sqlite3"
    service = EvaluationService(state_path=eval_path, clock=lambda: 1700000000)
    service.freeze_plan(plan_command)
    job = service.start_job("plan-c-cancel", worker_id="s12-test-worker")
    cancelled = service.cancel_job(job["job_id"])
    assert cancelled["status"] == "cancelled"

    outcome = service.process_job(job["job_id"], runner_result={"applications": []})
    assert outcome["status"] == "failed"
    assert outcome["reason_code"] == "JOB_CANCELLED"
    with pytest.raises(ValueError):
        service.query_bundle("s12_bundle_sha256_" + "0" * 64)
    assert hashlib.sha256(business_path.read_bytes()).hexdigest() == business_before


def test_lease_takeover_fences_stale_worker_and_publishes_one_bundle(
    tmp_path: Path,
) -> None:
    """A reclaimed lease (expired and re-claimed by a second worker) fences
    the stale worker: the second worker publishes exactly one bundle, the
    stale worker settles only its own discarded attempt, and the job keeps
    the higher fence/attempt."""
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    release = TargetRelease.compile(
        load_rules(rules_path),
        hashlib.sha256(rules_path.read_bytes()).hexdigest(),
        knowledge=get_kb().to_dict(),
    )
    business_path, business_before = _make_business_baseline(tmp_path, rules_path)
    run_specs = {
        "app-0": _complete_run_spec(
            release, _plate_documents("苏A92054", second_role=True), application_id="app-0"
        ),
        "app-1": _complete_run_spec(
            release,
            _plate_documents("苏A92054", second_role=True, second_plate_no="苏A92055"),
            application_id="app-1",
        ),
        "app-2": _complete_run_spec(
            release, _plate_documents("苏A92054", second_role=False), application_id="app-2"
        ),
        "app-3": _complete_run_spec(
            release, _plate_documents("苏A92054", second_role=True), application_id="app-3"
        ),
    }
    eval_path = tmp_path / "evaluation-fence.sqlite3"
    clock_state = {"now": 1700000000}

    def clock() -> int:
        return clock_state["now"]

    runner_output = run_s12_runner(
        _runner_projection(release, run_specs)
    )
    assert runner_output is not None
    release_stale = threading.Event()
    release_stale.clear()
    claimed = threading.Event()
    claimed.clear()

    def slow_runner(payload: dict[str, Any]) -> dict[str, Any] | None:
        del payload
        claimed.set()
        release_stale.wait(timeout=30)
        return runner_output

    service_a = EvaluationService(
        state_path=eval_path, clock=clock, runner_override=slow_runner
    )
    service_b = EvaluationService(state_path=eval_path, clock=clock)
    plan = service_a.freeze_plan(_small_c_plan_command(release, run_specs))
    job = service_a.start_job(plan["plan_id"], worker_id="s12-worker-a")

    worker_a_results: dict[str, Any] = {}

    def run_a() -> None:
        worker_a_results["outcome"] = service_a.process_job(
            job["job_id"], worker_id="s12-worker-a"
        )

    thread_a = threading.Thread(target=run_a)
    thread_a.start()
    assert claimed.wait(timeout=30)
    # Worker B cannot claim while the lease is live.
    busy = service_b.process_job(
        job["job_id"], runner_result=runner_output, worker_id="s12-worker-b"
    )
    assert busy["status"] == "busy"
    assert busy["reason_code"] == "JOB_LEASE_ACTIVE"
    # The lease expires: worker B reclaims with a higher fence/attempt.
    clock_state["now"] += 31
    winner = service_b.process_job(
        job["job_id"], runner_result=runner_output, worker_id="s12-worker-b"
    )
    assert winner["status"] == "INSUFFICIENT"
    assert winner["bundle_id"] is not None
    release_stale.set()
    thread_a.join(timeout=30)
    assert not thread_a.is_alive()
    assert worker_a_results["outcome"]["status"] == "stale"
    assert worker_a_results["outcome"]["reason_code"] == "STALE_WORKER"

    service_c = EvaluationService(state_path=eval_path, clock=clock)
    final_job = service_c.query_job(job["job_id"])
    assert final_job["status"] == "complete"
    assert final_job["fence"] == 2
    assert final_job["attempt_no"] == 2
    assert final_job["result"]["bundle_id"] == winner["bundle_id"]
    # Exactly one bundle was published; the stale worker published nothing.
    assert set(service_c._store.bundles) == {winner["bundle_id"]}
    attempt_statuses = sorted(
        (attempt["worker_id"], attempt["status"])
        for attempt in service_c._store.attempts.values()
    )
    assert attempt_statuses == [
        ("s12-worker-a", "discarded"),
        ("s12-worker-b", "complete"),
    ]
    assert hashlib.sha256(business_path.read_bytes()).hexdigest() == business_before
