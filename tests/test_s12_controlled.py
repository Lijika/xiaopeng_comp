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
    ControlledScenarioTestDriver,
    S01CommandPrincipal,
)
from task4_consistency.controlled.s01_checker import TargetRelease
from task4_consistency.controlled.s01_store import SQLiteTargetStore
from task4_consistency.controlled.s12 import (
    EvaluationService,
    LabelManifestStore,
    _clopper_pearson_upper,
    _cluster_statistics,
    _select_status,
    content_digest,
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


def _make_business_harness(
    tmp_path: Path, rules_path: Path
) -> tuple[
    list[ControlledScenarioService],
    list[tuple[str, str]],
    dict[str, tuple[str, str]],
    Path,
]:
    """One S01 business authority admitting several fixed scenarios (each
    scenario is constructor-bound) and running each admission's checker so a
    frozen ``immutable_ready_snapshot`` exists per application."""
    business_path = tmp_path / "business.sqlite3"
    scenarios = (
        "app_r53_bad_engine.json",
        "app_s04_bad_vin.json",
        "app_bad_brand.json",
        "app_bad_model.json",
    )
    services: list[ControlledScenarioService] = []
    admitted: list[tuple[str, str]] = []
    for index, scenario in enumerate(scenarios):
        service = ControlledScenarioService(
            fixture_root=ROOT / "fixtures" / "applications",
            rules_path=rules_path,
            state_path=business_path,
            scenario_id=scenario,
        )
        result = service.submit_demo(
            principal=TEST_INTEGRATOR,
            scenario_id=scenario,
            idempotency_key=f"s12-r1-{index}",
        )
        assert result.disposition is AdmissionDisposition.ACCEPTED, scenario
        ControlledScenarioTestDriver(service).process_next_job(now=0)
        services.append(service)
        admitted.append((scenario, result.application_id))
    store = SQLiteTargetStore(business_path)
    store.reload()
    snapshot_by_application: dict[str, tuple[str, str]] = {}
    for event in store.evidence_events:
        if event.get("kind") == "immutable_ready_snapshot":
            snapshot_by_application[event["application_id"]] = (
                event["snapshot_id"],
                event["content_sha256"],
            )
    assert len(snapshot_by_application) == len(scenarios)
    return services, admitted, snapshot_by_application, business_path


def _make_governed_release(
    tmp_path: Path,
) -> tuple[Any, str, str, dict[str, Any]]:
    """One bootstrapped S08 governed release authority plus the bootstrap
    release identity resolved from the Registry."""
    from tests.test_s08_controlled import make_policy_service

    service, _rules_path, _kb_path = make_policy_service(tmp_path)
    governance_path = tmp_path / "governance.sqlite3"
    store = SQLiteTargetStore(governance_path)
    store.reload()
    manifest = next(
        item for item in store.policy_manifests if item.get("schema_version")
    )
    checker_id = next(
        component["id"]
        for component in manifest["components"]
        if component["type"] == "checker"
    )
    artifact_row = next(
        item
        for item in store.policy_artifacts
        if item.get("artifact_id") == checker_id
    )
    import json as _json

    release = TargetRelease.from_artifact(
        _json.loads(artifact_row["canonical_json"])
    )
    public = release.public_manifest()
    return service, public["release_id"], public["digest"], manifest


def _write_label_manifest(
    work: Path, labels: dict[str, str]
) -> tuple[Path, str, str]:
    body = {
        "schema_version": "s12-label-manifest/1",
        "label_custody": "independent",
        "labels": labels,
    }
    digest = content_digest(body)
    manifest_id = f"manifest_sha256_{digest}"
    root = work / "labels"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{manifest_id}.json").write_text(
        json.dumps({"manifest_id": manifest_id, **body}),
        encoding="utf-8",
    )
    return root, manifest_id, digest


def _s12_authority_service(
    tmp_path: Path,
    *,
    business_services: list[ControlledScenarioService],
    governance_service: Any,
    label_root: Path,
    worker_subject: str = "s12-test-worker",
    clock: Any = None,
) -> EvaluationService:
    def measure() -> dict[str, Any]:
        facts: dict[str, Any] = {}
        for service in business_services:
            facts.update(service.evaluation_business_measurement())
        facts.update(governance_service.evaluation_governance_measurement())
        return facts

    return EvaluationService(
        state_path=tmp_path / "evaluation.sqlite3",
        clock=clock or (lambda: 1700000000),
        snapshot_provider=lambda application_id, snapshot_id: business_services[
            0
        ].evaluation_evidence_snapshot(
            application_id=application_id, snapshot_id=snapshot_id
        ),
        release_provider=lambda release_id, release_digest: governance_service.resolve_evaluation_release(
            release_id=release_id, release_digest=release_digest
        ),
        label_manifest_provider=LabelManifestStore(label_root).resolve,
        business_state_provider=measure,
        worker_subject=worker_subject,
    )


def _reference_plan_command(
    *,
    admitted: list[tuple[str, str]],
    snapshot_by_application: dict[str, tuple[str, str]],
    release_id: str,
    release_digest: str,
    manifest_id: str,
    manifest_digest: str,
    plan_id: str = "plan-c-1",
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    check_by_scenario = {
        "app_r53_bad_engine.json": "R_ENGINE_CROSS",
        "app_s04_bad_vin.json": "R_VIN_CROSS",
        "app_bad_brand.json": "R_BRAND_CROSS",
        "app_bad_model.json": "R_MODEL_CROSS",
    }
    opportunities: list[dict[str, Any]] = []
    clusters: list[dict[str, Any]] = []
    for index, (scenario, application_id) in enumerate(admitted):
        opportunity_id = f"opp-{index}"
        snapshot_id, snapshot_digest = snapshot_by_application[application_id]
        opportunities.append(
            {
                "opportunity_id": opportunity_id,
                "track": "C",
                "cluster": f"cl-{index}",
                "application_id": application_id,
                "cycle": 1,
                "check_id": check_by_scenario[scenario],
                "target_scope": "C",
                "evidence_snapshot_id": snapshot_id,
            }
        )
        clusters.append(
            {
                "cluster_id": f"cl-{index}",
                "stratum": "c",
                "applications": [application_id],
                "usage": "development",
            }
        )
    default_labels = {
        opportunity["opportunity_id"]: "consistent"
        for opportunity in opportunities
    }
    return {
        "schema_version": "s12-plan-command/1",
        "plan_id": plan_id,
        "scope_declared": "C",
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
        "clusters": clusters,
        "tracks": {
            "R": {"opportunities": []},
            "C": {
                "opportunities": [
                    opportunity["opportunity_id"] for opportunity in opportunities
                ]
            },
        },
        "views": {
            "R-E2E": {"opportunities": []},
            "R-T4-conditional": {"opportunities": []},
        },
        "opportunities": opportunities,
        "evidence_references": {
            application_id: {
                "snapshot_id": snapshot_by_application[application_id][0],
                "snapshot_digest": snapshot_by_application[application_id][1],
                "cycle": 1,
            }
            for _scenario, application_id in admitted
        },
        "release_reference": {"release_id": release_id, "release_digest": release_digest},
        "label_manifest": {"manifest_id": manifest_id, "manifest_digest": manifest_digest},
        "mandatory_check_families": [
            {"family_id": "cross-document", "check_ids": ["R_ENGINE_CROSS", "R_VIN_CROSS"]},
            {"family_id": "brand-model", "check_ids": ["R_BRAND_CROSS", "R_MODEL_CROSS"]},
        ],
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
    """The S12 operational slice: authoritative freeze over real S01/S08/label
    authorities, real subprocess + TargetChecker.run on the frozen plan, one
    omitted runner result materialized as ``missing`` with a retained
    denominator, INSUFFICIENT status, canonical SHA-256 bundle bytes, zero
    measured business deltas, restart replay, and linked non-overwriting
    rerun."""
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    business_services, admitted, snapshots, business_path = _make_business_harness(
        tmp_path, rules_path
    )
    governance_service, release_id, release_digest, _manifest = _make_governed_release(
        tmp_path
    )
    labels = {f"opp-{index}": "consistent" for index in range(len(admitted))}
    label_root, manifest_id, manifest_digest = _write_label_manifest(tmp_path, labels)
    service = _s12_authority_service(
        tmp_path,
        business_services=business_services,
        governance_service=governance_service,
        label_root=label_root,
    )
    plan = service.freeze_plan(
        _reference_plan_command(
            admitted=admitted,
            snapshot_by_application=snapshots,
            release_id=release_id,
            release_digest=release_digest,
            manifest_id=manifest_id,
            manifest_digest=manifest_digest,
        )
    )
    assert plan["plan_id"] == "plan-c-1"
    assert plan["plan_digest"] == content_digest(
        {k: v for k, v in plan.items() if k != "plan_digest"}
    )
    business_before = hashlib.sha256(business_path.read_bytes()).hexdigest()

    job = service.start_job("plan-c-1", worker_id="s12-test-worker")
    assert job["status"] == "queued"
    assert job["fence"] == 0
    assert job["attempt_no"] == 0

    # Real restricted subprocess invoking the existing pure checker over the
    # frozen plan material.  The runner projection carries no gold labels:
    # only the checker artifact, the frozen RunSpecs and the frozen budget.
    projection = {
        "schema_version": "s12-runner-request/1",
        "checker_artifact": plan["checker_artifact"],
        "run_specs": copy.deepcopy(plan["run_specs"]),
        "budget": copy.deepcopy(plan["budget"]),
        "stop_rule": plan["stop_rule"],
    }
    runner_output = run_s12_runner(projection)
    assert runner_output is not None
    returned = [item["application_id"] for item in runner_output["applications"]]
    assert set(returned) == set(plan["run_specs"])
    by_app = {item["application_id"]: item for item in runner_output["applications"]}
    ground_truth: dict[str, str] = {}
    for opportunity in plan["opportunities"]:
        application = by_app[opportunity["application_id"]]
        ground_truth[opportunity["opportunity_id"]] = next(
            check["verdict"]
            for check in application["checks"]
            if check["rule_id"] == opportunity["check_id"]
        )

    # Omit the last application from the runner result via an authenticated
    # budget stop: the parent must materialize an explicit ``missing``
    # prediction and keep the opportunity in its denominator.
    omitted_application = returned[-1]
    omitted_opportunity = next(
        opportunity["opportunity_id"]
        for opportunity in plan["opportunities"]
        if opportunity["application_id"] == omitted_application
    )
    runner_output = _stopped_runner_result(
        runner_output, completed_ids=[app for app in returned if app != omitted_application]
    )

    outcome = service.process_job(job["job_id"], runner_result=runner_output)
    assert outcome["status"] == "INSUFFICIENT"
    bundle_id = outcome["bundle_id"]
    assert bundle_id.startswith("s12_bundle_sha256_")

    bundle = service.query_bundle(bundle_id)
    assert bundle["status"] == "INSUFFICIENT"
    assert bundle["plan_id"] == "plan-c-1"
    assert bundle["job_id"] == job["job_id"]
    assert bundle["rerun_of_bundle_id"] is None
    expected_predictions = dict(ground_truth)
    expected_predictions[omitted_opportunity] = "missing"
    assert bundle["predictions"] == expected_predictions
    c_stats = bundle["tracks"]["C"]
    assert c_stats["denominators"]["E"] == len(plan["opportunities"])
    # Every frozen C/I opportunity remains in the denominator: the omitted
    # runner output did not shrink E.
    assert c_stats["denominators"]["E"] == len(expected_predictions)

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
    service2 = _s12_authority_service(
        tmp_path,
        business_services=business_services,
        governance_service=governance_service,
        label_root=label_root,
    )
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
    service, command, context = _slice1_harness(tmp_path)
    business_path = context["business_path"]
    business_before = hashlib.sha256(business_path.read_bytes()).hexdigest()
    first_opportunity = command["opportunities"][0]
    first_application = first_opportunity["application_id"]
    trimmed = copy.deepcopy(command)
    trimmed["plan_id"] = "plan-c-cancel"
    trimmed["opportunities"] = [first_opportunity]
    trimmed["tracks"]["C"] = {"opportunities": [first_opportunity["opportunity_id"]]}
    trimmed["clusters"] = [
        cluster
        for cluster in trimmed["clusters"]
        if cluster["cluster_id"] == first_opportunity["cluster"]
    ]
    trimmed["evidence_references"] = {
        application_id: reference
        for application_id, reference in trimmed["evidence_references"].items()
        if application_id == first_application
    }
    trimmed["budget"] = {"max_opportunities": 4, "max_runtime_ms": 5000}
    trimmed["mandatory_check_families"] = [
        {"family_id": "cross-document", "check_ids": [first_opportunity["check_id"]]}
    ]
    label_root, manifest_id, manifest_digest = _write_label_manifest(
        tmp_path,
        {first_opportunity["opportunity_id"]: "consistent"},
    )
    trimmed["label_manifest"] = {
        "manifest_id": manifest_id,
        "manifest_digest": manifest_digest,
    }
    service = _s12_authority_service(
        tmp_path,
        business_services=context["business_services"],
        governance_service=context["governance_service"],
        label_root=label_root,
    )
    service.freeze_plan(trimmed)
    job = service.start_job("plan-c-cancel", worker_id=service._worker_subject)
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
    _service, command, context = _slice1_harness(tmp_path)
    business_path = context["business_path"]
    business_before = hashlib.sha256(business_path.read_bytes()).hexdigest()
    eval_path = tmp_path / "evaluation-fence.sqlite3"
    clock_state = {"now": 1700000000}

    def clock() -> int:
        return clock_state["now"]

    release_stale = threading.Event()
    release_stale.clear()
    claimed = threading.Event()
    claimed.clear()

    prepared: dict[str, Any] = {}

    def slow_runner(payload: dict[str, Any]) -> dict[str, Any] | None:
        del payload
        claimed.set()
        release_stale.wait(timeout=30)
        return prepared["runner_output"]

    service_a = EvaluationService(
        state_path=eval_path,
        clock=clock,
        runner_override=slow_runner,
        snapshot_provider=lambda application_id, snapshot_id: context[
            "business_services"
        ][0].evaluation_evidence_snapshot(
            application_id=application_id, snapshot_id=snapshot_id
        ),
        release_provider=lambda release_id, release_digest: context[
            "governance_service"
        ].resolve_evaluation_release(
            release_id=release_id, release_digest=release_digest
        ),
        label_manifest_provider=LabelManifestStore(context["label_root"]).resolve,
        business_state_provider=context["measure"],
    )
    service_b = EvaluationService(
        state_path=eval_path,
        clock=clock,
        snapshot_provider=lambda application_id, snapshot_id: context[
            "business_services"
        ][0].evaluation_evidence_snapshot(
            application_id=application_id, snapshot_id=snapshot_id
        ),
        release_provider=lambda release_id, release_digest: context[
            "governance_service"
        ].resolve_evaluation_release(
            release_id=release_id, release_digest=release_digest
        ),
        label_manifest_provider=LabelManifestStore(context["label_root"]).resolve,
        business_state_provider=context["measure"],
    )
    plan = service_a.freeze_plan(command)
    runner_output = run_s12_runner(
        {
            "schema_version": "s12-runner-request/1",
            "checker_artifact": plan["checker_artifact"],
            "run_specs": copy.deepcopy(plan["run_specs"]),
            "budget": copy.deepcopy(plan["budget"]),
            "stop_rule": plan["stop_rule"],
        }
    )
    assert runner_output is not None
    prepared["runner_output"] = runner_output
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

    service_c = EvaluationService(
        state_path=eval_path,
        clock=clock,
        snapshot_provider=lambda application_id, snapshot_id: context[
            "business_services"
        ][0].evaluation_evidence_snapshot(
            application_id=application_id, snapshot_id=snapshot_id
        ),
        release_provider=lambda release_id, release_digest: context[
            "governance_service"
        ].resolve_evaluation_release(
            release_id=release_id, release_digest=release_digest
        ),
        label_manifest_provider=LabelManifestStore(context["label_root"]).resolve,
        business_state_provider=context["measure"],
    )
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


# ---------------------------------------------------------------------------
# Slice 1 — authoritative freeze and measured business state
# ---------------------------------------------------------------------------


def _slice1_harness(
    tmp_path: Path,
) -> tuple[EvaluationService, dict[str, Any], dict[str, Any]]:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    business_services, admitted, snapshots, business_path = _make_business_harness(
        tmp_path, rules_path
    )
    governance_service, release_id, release_digest, _manifest = _make_governed_release(
        tmp_path
    )
    labels = {f"opp-{index}": "consistent" for index in range(len(admitted))}
    label_root, manifest_id, manifest_digest = _write_label_manifest(
        tmp_path, labels
    )
    def measure() -> dict[str, Any]:
        facts: dict[str, Any] = {}
        for business_service in business_services:
            facts.update(business_service.evaluation_business_measurement())
        facts.update(governance_service.evaluation_governance_measurement())
        return facts

    service = EvaluationService(
        state_path=tmp_path / "evaluation.sqlite3",
        clock=lambda: 1700000000,
        snapshot_provider=lambda application_id, snapshot_id: business_services[
            0
        ].evaluation_evidence_snapshot(
            application_id=application_id, snapshot_id=snapshot_id
        ),
        release_provider=lambda release_id, release_digest: governance_service.resolve_evaluation_release(
            release_id=release_id, release_digest=release_digest
        ),
        label_manifest_provider=LabelManifestStore(label_root).resolve,
        business_state_provider=measure,
    )
    command = _reference_plan_command(
        admitted=admitted,
        snapshot_by_application=snapshots,
        release_id=release_id,
        release_digest=release_digest,
        manifest_id=manifest_id,
        manifest_digest=manifest_digest,
    )
    return service, command, {
        "admitted": admitted,
        "snapshots": snapshots,
        "business_services": business_services,
        "governance_service": governance_service,
        "label_root": label_root,
        "business_path": business_path,
        "measure": measure,
    }


def test_freeze_resolves_s01_snapshot_and_s08_release_by_verified_reference(
    tmp_path: Path,
) -> None:
    """Freeze accepts stable references and the authority resolves the real
    S01 snapshot, S08 release/checker artifact, and label manifest content."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    assert plan["plan_id"] == "plan-c-1"
    assert plan["plan_digest"]
    assert plan["release"]["release_id"]
    assert plan["release"]["release_digest"]
    assert plan["release"]["checker_build"]
    assert plan["release"]["manifest_id"]
    assert plan["release"]["manifest_digest"]
    assert plan["release"]["protected_baseline_digest"]
    # Every frozen RunSpec was built server-side from the resolved snapshots
    # and pins the resolved release identity.
    assert set(plan["run_specs"]) == {
        application_id for _scenario, application_id in _context["admitted"]
    }
    for run_spec in plan["run_specs"].values():
        assert run_spec["release_digest"] == plan["release"]["release_digest"]
        assert run_spec["checker_build"] == plan["release"]["checker_build"]
        assert run_spec["evidence_snapshot_digest"]
        assert run_spec["evidence_snapshot"]["schema_version"] == "s01-evidence-snapshot/1"
    # Every opportunity carries gold resolved from the label manifest.
    for opportunity in plan["opportunities"]:
        assert opportunity["label"] in {
            "consistent",
            "inconsistent",
            "indeterminate",
            "not_applicable",
        }
        assert opportunity["label_custody"] == "independent"
    # Environment is derived server-side.
    assert plan["environment"]["python"]
    assert plan["environment"]["evaluator_build"] == "s12-evaluator/1"
    assert plan["environment"]["dependency_identity"]


def test_freeze_rejects_release_pin_checker_artifact_or_snapshot_mismatch(
    tmp_path: Path,
) -> None:
    """A mismatched release digest or evidence snapshot digest fails freeze:
    caller-supplied identities cannot impersonate the authorities."""
    service, command, _context = _slice1_harness(tmp_path)
    tampered_release = copy.deepcopy(command)
    tampered_release["release_reference"]["release_digest"] = "0" * 64
    with pytest.raises(ValueError):
        service.freeze_plan(tampered_release)
    tampered_snapshot = copy.deepcopy(command)
    first = next(iter(tampered_snapshot["evidence_references"]))
    tampered_snapshot["evidence_references"][first]["snapshot_digest"] = "0" * 64
    with pytest.raises(ValueError):
        service.freeze_plan(tampered_snapshot)


def test_freeze_rejects_unregistered_label_manifest_and_caller_environment_claims(
    tmp_path: Path,
) -> None:
    """An unregistered label manifest and any caller-supplied environment /
    run_spec / checker_artifact claim are rejected."""
    service, command, _context = _slice1_harness(tmp_path)
    unknown_manifest = copy.deepcopy(command)
    unknown_manifest["label_manifest"]["manifest_id"] = "manifest_sha256_" + "0" * 64
    with pytest.raises(ValueError):
        service.freeze_plan(unknown_manifest)
    fabricated = copy.deepcopy(command)
    fabricated["environment"] = {"python": "9.9.9", "evaluator_build": "fake"}
    with pytest.raises(ValueError):
        service.freeze_plan(fabricated)
    fabricated = copy.deepcopy(command)
    fabricated["run_specs"] = {"app": {"run_id": "fake"}}
    with pytest.raises(ValueError):
        service.freeze_plan(fabricated)


def test_business_authority_measurements_are_captured_and_unchanged(
    tmp_path: Path,
) -> None:
    """Freeze captures the S01/S08 business measurements; the plan carries
    the before-facts and the published bundle carries before/after with zero
    deltas."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    before = plan["business_before"]
    for key in (
        "lifecycle_revision",
        "evidence_revision",
        "evidence_count",
        "evidence_digest",
        "current_run_reference",
        "governance_revision",
        "activation_count",
    ):
        assert key in before, key
    job = service.start_job(plan["plan_id"], worker_id=service._worker_subject)
    outcome = service.process_job(job["job_id"])
    assert outcome["status"] in {"INSUFFICIENT", "FAIL", "SMOKE_ONLY"}
    bundle = service.query_bundle(outcome["bundle_id"])
    assert bundle["business_deltas"] == {
        "lifecycle_revision": 0,
        "evidence_rows": 0,
        "evidence_digest": None,
        "current_run_pointer": 0,
        "policy_revision": 0,
        "governance_revision": 0,
    }
    assert bundle["business_before"] == before
    assert bundle["business_after"] == before


def test_business_authority_change_prevents_formal_publication(
    tmp_path: Path,
) -> None:
    """A business-state change between freeze and terminal publication
    prevents formal publication and records the exact changed fact."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"], worker_id=service._worker_subject)
    # Change the business authority after freeze: admit a new application.
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    extra = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=rules_path,
        state_path=_context["admitted"] and _slice1_business_path(tmp_path),
        scenario_id="app_inconsistent_vin.json",
    )
    changed = extra.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_inconsistent_vin.json",
        idempotency_key="s12-r1-changed",
    )
    assert changed.disposition is AdmissionDisposition.ACCEPTED
    outcome = service.process_job(job["job_id"])
    assert outcome["status"] == "INVALID"
    assert outcome["bundle_id"] is None
    assert any(
        "business" in reason or "BUSINESS" in reason
        for reason in (outcome.get("reason_codes") or [])
    )


def _slice1_business_path(tmp_path: Path) -> Path:
    return tmp_path / "business.sqlite3"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Slice 2 — formal eligibility, mandatory strata, exact denominators
# ---------------------------------------------------------------------------


def _synthetic_holdout_track(
    *,
    holdout_consistent: int,
    holdout_inconsistent: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, str]]:
    """Synthetic perfect track split across acceptance_holdout clusters for
    the pure eligibility/status functions."""
    opportunities: list[dict[str, object]] = []
    clusters: list[dict[str, object]] = []
    predictions: dict[str, str] = {}
    for index in range(holdout_consistent):
        opportunity_id = f"ho-c-{index}"
        opportunities.append(
            {
                "opportunity_id": opportunity_id,
                "track": "R",
                "cluster": f"ho-cl-{index}",
                "label": "consistent",
                "label_custody": "independent",
            }
        )
        clusters.append(
            {
                "cluster_id": f"ho-cl-{index}",
                "stratum": "r",
                "usage": "acceptance_holdout",
            }
        )
        predictions[opportunity_id] = "consistent"
    for index in range(holdout_inconsistent):
        opportunity_id = f"ho-i-{index}"
        opportunities.append(
            {
                "opportunity_id": opportunity_id,
                "track": "R",
                "cluster": f"ho-icl-{index}",
                "label": "inconsistent",
                "label_custody": "independent",
            }
        )
        clusters.append(
            {
                "cluster_id": f"ho-icl-{index}",
                "stratum": "r",
                "usage": "acceptance_holdout",
            }
        )
        predictions[opportunity_id] = "inconsistent"
    return opportunities, clusters, predictions


def test_development_and_calibration_clusters_cannot_produce_formal_pass() -> None:
    """A formally passing statistics block over development/calibration
    clusters is not a formal PASS: the status stays INSUFFICIENT with the
    exact non-formal reason."""
    from task4_consistency.controlled.s12 import _select_status as _select

    opportunities, clusters, predictions = _synthetic_holdout_track(
        holdout_consistent=60, holdout_inconsistent=100
    )
    passing = _cluster_statistics(
        opportunities, clusters, predictions, seed=20, membership="R"
    )
    assert passing["estimable"] is True
    assert passing["conclusion"] == "pass"
    # Development usage: the same passing statistics must not yield PASS.
    dev_clusters = copy.deepcopy(clusters)
    for cluster in dev_clusters:
        cluster["usage"] = "development"
    dev_passing = _cluster_statistics(
        opportunities, dev_clusters, predictions, seed=21, membership="R"
    )
    status, reasons = _select(
        {"R": _empty_stats("R"), "C": _empty_stats("C")},
        {"R-E2E": dev_passing, "R-T4-conditional": _empty_stats("R-T4-conditional")},
        scope="R",
        holdout_eligible=False,
    )
    assert status == "INSUFFICIENT"
    assert any("non-formal" in reason for reason in reasons)


def test_scope_is_derived_from_verified_holdout_membership(tmp_path: Path) -> None:
    """A plan whose clusters are all acceptance_holdout with independent
    label custody is holdout-eligible; any development cluster makes the
    derived scope non-eligible."""
    from task4_consistency.controlled.s12 import _holdout_eligibility

    holdout_opportunities, holdout_clusters, _predictions = _synthetic_holdout_track(
        holdout_consistent=2, holdout_inconsistent=2
    )
    eligible, reasons = _holdout_eligibility(holdout_opportunities, holdout_clusters)
    assert eligible is True
    assert reasons == []
    mixed_clusters = copy.deepcopy(holdout_clusters)
    mixed_clusters[0]["usage"] = "development"
    eligible, reasons = _holdout_eligibility(holdout_opportunities, mixed_clusters)
    assert eligible is False
    assert any("development" in reason for reason in reasons)
    # The derived scope is recorded on the published bundle.
    service, command, context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"], worker_id=service._worker_subject)
    outcome = service.process_job(job["job_id"])
    bundle = service.query_bundle(outcome["bundle_id"])
    assert bundle["scope_eligibility"]["holdout_eligible"] is False
    assert any(
        "development" in reason
        for reason in bundle["scope_eligibility"]["reasons"]
    )


def test_every_mandatory_check_family_must_be_estimable_and_pass(tmp_path: Path) -> None:
    """A declared mandatory check family with no frozen opportunities is
    rejected at freeze; a covered family appears with its own statistics."""
    service, command, _context = _slice1_harness(tmp_path)
    unknown_family = copy.deepcopy(command)
    unknown_family["mandatory_check_families"] = [
        {"family_id": "ghost", "check_ids": ["R_GHOST_CHECK"]}
    ]
    with pytest.raises(ValueError):
        service.freeze_plan(unknown_family)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"], worker_id=service._worker_subject)
    outcome = service.process_job(job["job_id"])
    bundle = service.query_bundle(outcome["bundle_id"])
    family_ids = set(bundle["mandatory_check_families"])
    assert {"cross-document", "brand-model"} <= family_ids


def test_c_track_membership_must_equal_all_declared_c_opportunities(tmp_path: Path) -> None:
    """tracks.C must equal exactly the declared C opportunities (no silent
    addition or omission)."""
    service, command, _context = _slice1_harness(tmp_path)
    trimmed = copy.deepcopy(command)
    trimmed["tracks"]["C"] = {"opportunities": ["opp-0"]}
    with pytest.raises(ValueError):
        service.freeze_plan(trimmed)
    inflated = copy.deepcopy(command)
    inflated["tracks"]["C"] = {
        "opportunities": [
            opportunity["opportunity_id"] for opportunity in command["opportunities"]
        ]
        + ["opp-ghost"]
    }
    with pytest.raises(ValueError):
        service.freeze_plan(inflated)


def test_r_views_must_exactly_follow_the_registered_view_partition(tmp_path: Path) -> None:
    """R-E2E and R-T4-conditional are disjoint and their union accounts for
    every registered R opportunity."""
    service, command, _context = _slice1_harness(tmp_path)
    opportunity_ids = [
        opportunity["opportunity_id"] for opportunity in command["opportunities"]
    ]
    incomplete = copy.deepcopy(command)
    for opportunity in incomplete["opportunities"]:
        opportunity["track"] = "R"
    incomplete["tracks"] = {"R": {"opportunities": opportunity_ids}, "C": {"opportunities": []}}
    incomplete["views"] = {
        "R-E2E": {"opportunities": opportunity_ids[:2]},
        "R-T4-conditional": {"opportunities": []},
    }
    with pytest.raises(ValueError):
        service.freeze_plan(incomplete)
    complete = copy.deepcopy(incomplete)
    complete["views"] = {
        "R-E2E": {"opportunities": opportunity_ids[:2]},
        "R-T4-conditional": {"opportunities": opportunity_ids[2:]},
    }
    plan = service.freeze_plan(complete)
    assert plan["plan_id"] == "plan-c-1"


def test_application_and_all_variants_belong_to_one_base_cluster_and_split(tmp_path: Path) -> None:
    """Every application and variant belongs to exactly one base cluster and
    split; duplicates across clusters are rejected at freeze."""
    service, command, _context = _slice1_harness(tmp_path)
    duplicated = copy.deepcopy(command)
    duplicated["clusters"][1]["applications"].append(
        command["clusters"][0]["applications"][0]
    )
    with pytest.raises(ValueError):
        service.freeze_plan(duplicated)
    with_variants = copy.deepcopy(command)
    with_variants["clusters"][0]["variants"] = ["variant-a"]
    with_variants["clusters"][1]["variants"] = ["variant-a"]
    with pytest.raises(ValueError):
        service.freeze_plan(with_variants)
    accepted = copy.deepcopy(command)
    accepted["clusters"][0]["variants"] = ["variant-a"]
    accepted["clusters"][1]["variants"] = ["variant-b"]
    plan = service.freeze_plan(accepted)
    assert plan["clusters"][0]["variants"] == ["variant-a"]


def test_opportunity_application_cycle_check_and_snapshot_match_frozen_run_spec(tmp_path: Path) -> None:
    """An opportunity whose application/cycle/snapshot does not match the
    resolved frozen run spec is rejected at freeze."""
    service, command, _context = _slice1_harness(tmp_path)
    wrong_snapshot = copy.deepcopy(command)
    wrong_snapshot["opportunities"][0]["evidence_snapshot_id"] = (
        "snapshot_sha256_" + "0" * 64
    )
    with pytest.raises(ValueError):
        service.freeze_plan(wrong_snapshot)
    wrong_cycle = copy.deepcopy(command)
    wrong_cycle["opportunities"][0]["cycle"] = 99
    with pytest.raises(ValueError):
        service.freeze_plan(wrong_cycle)
    unknown_app = copy.deepcopy(command)
    unknown_app["opportunities"][0]["application_id"] = "not-admitted"
    with pytest.raises(ValueError):
        service.freeze_plan(unknown_app)


# ---------------------------------------------------------------------------
# Slice 3 — runner digest and frozen global budget
# ---------------------------------------------------------------------------


def _stopped_runner_result(
    runner_output: dict[str, Any], *, completed_ids: list[str]
) -> dict[str, Any]:
    """A digest-valid child-style stopped result: only the completed
    applications remain and the stop observation is authenticated by the
    canonical result digest."""
    applications = [
        application
        for application in runner_output["applications"]
        if application["application_id"] in completed_ids
    ]
    stop = {
        "stop_reason": "budget-or-plan",
        "elapsed_ms": 1,
        "completed_application_ids": list(completed_ids),
    }
    material = {
        "schema_version": "s12-runner-result/1",
        "applications": applications,
        "stop": stop,
    }
    digest = content_digest(material)
    return {"digest": digest, **material}


def _runner_output_for_plan(plan: dict[str, Any]) -> dict[str, Any]:
    output = run_s12_runner(
        {
            "schema_version": "s12-runner-request/1",
            "checker_artifact": plan["checker_artifact"],
            "run_specs": copy.deepcopy(plan["run_specs"]),
            "budget": copy.deepcopy(plan["budget"]),
            "stop_rule": plan["stop_rule"],
        }
    )
    assert output is not None
    return output


def test_parent_rejects_tampered_runner_digest_checks_errors_and_identity(
    tmp_path: Path,
) -> None:
    """The parent recomputes and verifies the canonical runner result digest
    before reading any application result; tampered digests, unknown or
    duplicated application identities, and out-of-alphabet verdicts are
    INVALID with no bundle publication."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    runner_output = _runner_output_for_plan(plan)

    def _run_case(tampered: dict[str, Any], expected_reason: str) -> None:
        tampered_job = service.start_job(plan["plan_id"], worker_id=service._worker_subject)
        outcome = service.process_job(
            tampered_job["job_id"], runner_result=tampered
        )
        assert outcome["status"] == "INVALID", outcome
        assert expected_reason in (outcome.get("reason_codes") or [])
        assert outcome["bundle_id"] is None

    tampered_digest = copy.deepcopy(runner_output)
    tampered_digest["digest"] = "0" * 64
    _run_case(tampered_digest, "RUNNER_DIGEST_MISMATCH")

    tampered_alphabet = copy.deepcopy(runner_output)
    tampered_alphabet["applications"][0]["checks"][0]["verdict"] = "not-a-verdict"
    tampered_alphabet["digest"] = content_digest(
        {key: value for key, value in tampered_alphabet.items() if key != "digest"}
    )
    _run_case(tampered_alphabet, "RUNNER_CHECK_INVALID")

    unknown_app = copy.deepcopy(runner_output)
    unknown_app["applications"][0]["application_id"] = "not-admitted"
    unknown_app["digest"] = content_digest(
        {key: value for key, value in unknown_app.items() if key != "digest"}
    )
    _run_case(unknown_app, "RUNNER_UNKNOWN_APPLICATION")

    duplicated = copy.deepcopy(runner_output)
    duplicated["applications"].append(copy.deepcopy(duplicated["applications"][0]))
    duplicated["stop"]["completed_application_ids"].append(
        duplicated["applications"][0]["application_id"]
    )
    duplicated["digest"] = content_digest(
        {key: value for key, value in duplicated.items() if key != "digest"}
    )
    _run_case(duplicated, "RUNNER_DUPLICATE_APPLICATION")


def test_runner_error_record_keeps_frozen_application_and_run_identity(
    tmp_path: Path,
) -> None:
    """A per-application runner error record keeps the frozen application and
    run identity and materializes an explicit ``error`` prediction."""
    service, command, context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"], worker_id=service._worker_subject)
    runner_output = _runner_output_for_plan(plan)
    failed_application = runner_output["applications"][0]["application_id"]
    error_result = copy.deepcopy(runner_output)
    error_result["applications"] = [
        {"application_id": failed_application, "run_id": plan["run_specs"][failed_application]["run_id"], "error": "CHECKER_EXECUTION_FAILED"},
        *[application for application in error_result["applications"] if application["application_id"] != failed_application],
    ]
    error_result["digest"] = content_digest(
        {key: value for key, value in error_result.items() if key != "digest"}
    )
    outcome = service.process_job(job["job_id"], runner_result=error_result)
    assert outcome["status"] in {"INSUFFICIENT", "FAIL", "SMOKE_ONLY"}
    bundle = service.query_bundle(outcome["bundle_id"])
    failed_opportunity = next(
        opportunity["opportunity_id"]
        for opportunity in plan["opportunities"]
        if opportunity["application_id"] == failed_application
    )
    assert bundle["predictions"][failed_opportunity] == "error"
    assert {
        error["opportunity_id"]: error["reason_code"]
        for error in bundle["errors"]
    }[failed_opportunity] == "CHECKER_EXECUTION_FAILED"


def test_global_runtime_budget_is_shared_across_applications(tmp_path: Path) -> None:
    """max_runtime_ms is one frozen run-level budget: a tiny budget stops the
    run before every application completes and the recorded elapsed time
    respects the budget."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    budget_command = copy.deepcopy(command)
    budget_command["plan_id"] = "plan-c-budget"
    budget_command["budget"] = {"max_opportunities": 10, "max_runtime_ms": 1}
    plan = service.freeze_plan(budget_command)
    job = service.start_job(plan["plan_id"], worker_id=service._worker_subject)
    runner_output = _runner_output_for_plan(plan)
    assert runner_output["stop"]["stop_reason"] == "budget-or-plan"
    assert len(runner_output["stop"]["completed_application_ids"]) < len(
        plan["run_specs"]
    )
    assert runner_output["stop"]["elapsed_ms"] <= 1000


def test_budget_stop_materializes_unfinished_opportunities_and_is_insufficient(
    tmp_path: Path,
) -> None:
    """A budget stop leaves every unfinished opportunity explicitly
    ``missing`` and the run is INSUFFICIENT."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"], worker_id=service._worker_subject)
    runner_output = _runner_output_for_plan(plan)
    completed_ids = runner_output["stop"]["completed_application_ids"]
    stopped = _stopped_runner_result(runner_output, completed_ids=completed_ids[:2])
    outcome = service.process_job(job["job_id"], runner_result=stopped)
    assert outcome["status"] == "INSUFFICIENT"
    bundle = service.query_bundle(outcome["bundle_id"])
    unfinished = [
        opportunity["opportunity_id"]
        for opportunity in plan["opportunities"]
        if opportunity["application_id"] not in completed_ids[:2]
    ]
    for opportunity_id in unfinished:
        assert bundle["predictions"][opportunity_id] == "missing"
    assert set(bundle["missing_opportunities"]) == set(unfinished)
    assert bundle["stop_rule_satisfied"] is False
    assert bundle["stop_reason"] == "budget-or-plan"


def test_stop_rule_observation_is_recorded_from_execution(tmp_path: Path) -> None:
    """A completed plan-exhausted run records stop_reason plan-exhausted and
    stop_rule_satisfied True from the verified child observation."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"], worker_id=service._worker_subject)
    runner_output = _runner_output_for_plan(plan)
    assert runner_output["stop"]["stop_reason"] == "plan-exhausted"
    outcome = service.process_job(job["job_id"], runner_result=runner_output)
    bundle = service.query_bundle(outcome["bundle_id"])
    assert bundle["stop_reason"] == "plan-exhausted"
    assert bundle["stop_rule_satisfied"] is True
    assert len(bundle["completed_application_ids"]) == len(plan["run_specs"])


# ---------------------------------------------------------------------------
# Slice 4 — row-scoped integrity and atomic fencing/publication
# ---------------------------------------------------------------------------


def _tamper_stored_job_payload(state_path: Path, job_id: str) -> None:
    """Corrupt the stored job payload while keeping the integrity digest
    column: the next row-scoped transition must fail closed."""
    import sqlite3 as _sqlite3

    connection = _sqlite3.connect(state_path)
    connection.execute(
        "UPDATE s12_jobs SET payload = ? WHERE item_id = ?",
        ('{"schema_version": "s12-job/1", "tampered": true}', job_id),
    )
    connection.commit()
    connection.close()


def test_claim_rejects_payload_with_invalid_stored_integrity_digest(
    tmp_path: Path,
) -> None:
    """A job row whose stored payload does not match its integrity digest is
    rejected inside the claim write transaction (fail closed)."""
    from task4_consistency.controlled.s12 import S12IntegrityError

    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"], worker_id=service._worker_subject)
    _tamper_stored_job_payload(tmp_path / "evaluation.sqlite3", job["job_id"])
    with pytest.raises(S12IntegrityError):
        service.process_job(job["job_id"], worker_id=service._worker_subject)


def test_publication_cas_rejects_fence_advanced_after_ownership_read(
    tmp_path: Path,
) -> None:
    """Fence validation and publication share one row-scoped transaction: a
    worker whose lease was reclaimed by a higher fence publishes nothing."""
    service, command, context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    eval_path = tmp_path / "evaluation.sqlite3"
    clock_state = {"now": 1700000000}

    def clock() -> int:
        return clock_state["now"]

    runner_output = _runner_output_for_plan(plan)
    release_stale = threading.Event()
    claimed = threading.Event()

    def slow_runner(payload: dict[str, Any]) -> dict[str, Any] | None:
        del payload
        claimed.set()
        release_stale.wait(timeout=30)
        return runner_output

    service_a = EvaluationService(
        state_path=eval_path,
        clock=clock,
        runner_override=slow_runner,
        snapshot_provider=lambda application_id, snapshot_id: context[
            "business_services"
        ][0].evaluation_evidence_snapshot(
            application_id=application_id, snapshot_id=snapshot_id
        ),
        release_provider=lambda release_id, release_digest: context[
            "governance_service"
        ].resolve_evaluation_release(
            release_id=release_id, release_digest=release_digest
        ),
        label_manifest_provider=LabelManifestStore(context["label_root"]).resolve,
        business_state_provider=context["measure"],
    )
    service_b = EvaluationService(
        state_path=eval_path,
        clock=clock,
        snapshot_provider=lambda application_id, snapshot_id: context[
            "business_services"
        ][0].evaluation_evidence_snapshot(
            application_id=application_id, snapshot_id=snapshot_id
        ),
        release_provider=lambda release_id, release_digest: context[
            "governance_service"
        ].resolve_evaluation_release(
            release_id=release_id, release_digest=release_digest
        ),
        label_manifest_provider=LabelManifestStore(context["label_root"]).resolve,
        business_state_provider=context["measure"],
    )
    job = service_a.start_job(plan["plan_id"], worker_id="s12-worker-a")
    worker_a_results: dict[str, Any] = {}

    def run_a() -> None:
        worker_a_results["outcome"] = service_a.process_job(
            job["job_id"], worker_id="s12-worker-a"
        )

    thread_a = threading.Thread(target=run_a)
    thread_a.start()
    assert claimed.wait(timeout=30)
    clock_state["now"] += 31
    winner = service_b.process_job(
        job["job_id"], runner_result=runner_output, worker_id="s12-worker-b"
    )
    assert winner["bundle_id"] is not None
    release_stale.set()
    thread_a.join(timeout=30)
    assert worker_a_results["outcome"]["status"] == "stale"
    final = EvaluationService(
        state_path=eval_path,
        clock=clock,
        snapshot_provider=lambda application_id, snapshot_id: context[
            "business_services"
        ][0].evaluation_evidence_snapshot(
            application_id=application_id, snapshot_id=snapshot_id
        ),
        release_provider=lambda release_id, release_digest: context[
            "governance_service"
        ].resolve_evaluation_release(
            release_id=release_id, release_digest=release_digest
        ),
        label_manifest_provider=LabelManifestStore(context["label_root"]).resolve,
        business_state_provider=context["measure"],
    )
    assert set(final._store.bundles) == {winner["bundle_id"]}


def test_cancel_claim_diagnostic_and_publish_are_row_scoped_transactions(
    tmp_path: Path,
) -> None:
    """Every mutable job transition persists row-scoped: a fresh authority
    reopens the exact terminal row after each transition."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"], worker_id=service._worker_subject)
    cancelled = service.cancel_job(job["job_id"])
    assert cancelled["status"] == "cancelled"
    fresh = EvaluationService(
        state_path=tmp_path / "evaluation.sqlite3",
        clock=lambda: 1700000000,
        snapshot_provider=lambda application_id, snapshot_id: _slice1_harness(
            tmp_path
        )[2]["business_services"][0].evaluation_evidence_snapshot(
            application_id=application_id, snapshot_id=snapshot_id
        ),
        release_provider=lambda release_id, release_digest: _slice1_harness(tmp_path)[
            2
        ]["governance_service"].resolve_evaluation_release(
            release_id=release_id, release_digest=release_digest
        ),
        label_manifest_provider=LabelManifestStore(tmp_path / "labels").resolve,
        business_state_provider=lambda: {},
    )
    assert fresh.query_job(job["job_id"])["status"] == "cancelled"


def test_concurrent_start_and_rerun_create_distinct_jobs(tmp_path: Path) -> None:
    """Two authorities starting or rerunning the same plan concurrently
    produce distinct job ids."""
    service_a, command, context = _slice1_harness(tmp_path)
    plan = service_a.freeze_plan(command)
    eval_path = tmp_path / "evaluation.sqlite3"
    service_b = EvaluationService(
        state_path=eval_path,
        clock=lambda: 1700000000,
        snapshot_provider=lambda application_id, snapshot_id: context[
            "business_services"
        ][0].evaluation_evidence_snapshot(
            application_id=application_id, snapshot_id=snapshot_id
        ),
        release_provider=lambda release_id, release_digest: context[
            "governance_service"
        ].resolve_evaluation_release(
            release_id=release_id, release_digest=release_digest
        ),
        label_manifest_provider=LabelManifestStore(context["label_root"]).resolve,
        business_state_provider=context["measure"],
    )
    results: dict[str, Any] = {}

    def start_a() -> None:
        results["a"] = service_a.start_job(plan["plan_id"], worker_id="s12-worker-a")

    def start_b() -> None:
        results["b"] = service_b.start_job(plan["plan_id"], worker_id="s12-worker-a")

    thread_a = threading.Thread(target=start_a)
    thread_b = threading.Thread(target=start_b)
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=30)
    thread_b.join(timeout=30)
    assert results["a"]["job_id"] != results["b"]["job_id"]
    assert len({results["a"]["job_id"], results["b"]["job_id"]}) == 2


def test_stale_service_cache_cannot_overwrite_newer_job_state(
    tmp_path: Path,
) -> None:
    """A service whose in-memory cache predates a newer terminal state cannot
    overwrite it: transitions re-read the authoritative row."""
    service_a, command, context = _slice1_harness(tmp_path)
    plan = service_a.freeze_plan(command)
    eval_path = tmp_path / "evaluation.sqlite3"
    service_b = EvaluationService(
        state_path=eval_path,
        clock=lambda: 1700000000,
        snapshot_provider=lambda application_id, snapshot_id: context[
            "business_services"
        ][0].evaluation_evidence_snapshot(
            application_id=application_id, snapshot_id=snapshot_id
        ),
        release_provider=lambda release_id, release_digest: context[
            "governance_service"
        ].resolve_evaluation_release(
            release_id=release_id, release_digest=release_digest
        ),
        label_manifest_provider=LabelManifestStore(context["label_root"]).resolve,
        business_state_provider=context["measure"],
    )
    job = service_a.start_job(plan["plan_id"], worker_id=service_a._worker_subject)
    service_b.cancel_job(job["job_id"])
    outcome = service_a.process_job(
        job["job_id"], worker_id=service_a._worker_subject
    )
    assert outcome["status"] == "failed"
    assert outcome["reason_code"] == "JOB_CANCELLED"
    assert service_a.query_job(job["job_id"])["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Slice 5 — complete immutable replay package and independent result digest
# ---------------------------------------------------------------------------


def _tamper_stored_bundle_payload(state_path: Path, bundle_id: str) -> None:
    import sqlite3 as _sqlite3

    connection = _sqlite3.connect(state_path)
    connection.execute(
        "UPDATE s12_bundles SET payload = ? WHERE item_id = ?",
        ('{"schema_version": "s12-evaluation-bundle/1", "tampered": true}', bundle_id),
    )
    connection.commit()
    connection.close()


def test_bundle_resolves_complete_frozen_replay_package(tmp_path: Path) -> None:
    """The queried bundle resolves the complete frozen plan: clusters,
    opportunities and gold, evidence references, release identity, label
    provenance, environment, budget/stop, predictions/errors, strata,
    intervals, status, and lineage."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"], worker_id=service._worker_subject)
    outcome = service.process_job(job["job_id"])
    bundle = service.query_bundle(outcome["bundle_id"])
    def _round_trip(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))

    assert bundle["plan_id"] == plan["plan_id"]
    assert bundle["plan_digest"] == plan["plan_digest"]
    assert bundle["clusters"] == _round_trip(plan["clusters"])
    assert bundle["opportunities"] == _round_trip(plan["opportunities"])
    assert bundle["tracks_declared"] == _round_trip(plan["tracks"])
    assert bundle["views_declared"] == _round_trip(plan["views"])
    assert bundle["evidence_references"] == _round_trip(
        plan["evidence_references"]
    )
    assert bundle["label_manifest"] == _round_trip(plan["label_manifest"])
    assert bundle["release"] == _round_trip(plan["release"])
    assert bundle["environment"] == _round_trip(plan["environment"])
    assert bundle["budget"] == _round_trip(plan["budget"])
    assert bundle["stop_rule"] == plan["stop_rule"]
    assert bundle["split"] == _round_trip(plan["split"])
    assert bundle["mandatory_check_families"]
    assert bundle["strata"]
    assert bundle["scope_eligibility"]["holdout_eligible"] is False
    assert bundle["result_digest"]


def test_bundle_contains_cohort_exclusions_clusters_gold_evidence_and_versions(
    tmp_path: Path,
) -> None:
    """The bundle carries the frozen cohort with exclusions (reason + hash),
    gold labels, evidence snapshot digests and the evaluator/dependency
    versions."""
    service, command, context = _slice1_harness(tmp_path)
    command["cohort"] = {
        "exclusions": [
            {
                "item": "opportunity-ghost",
                "reason": "unverifiable label custody",
                "reference_sha256": "0" * 64,
            }
        ]
    }
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"], worker_id=service._worker_subject)
    outcome = service.process_job(job["job_id"])
    bundle = service.query_bundle(outcome["bundle_id"])
    assert bundle["cohort"]["exclusions"][0]["reason"] == "unverifiable label custody"
    assert bundle["cohort"]["exclusions"][0]["reference_sha256"] == "0" * 64
    for opportunity in bundle["opportunities"]:
        assert opportunity["label"] in {
            "consistent",
            "inconsistent",
            "indeterminate",
            "not_applicable",
        }
        assert opportunity["label_custody"] == "independent"
    assert bundle["environment"]["evaluator_build"] == "s12-evaluator/1"
    assert bundle["environment"]["dependency_identity"]
    assert bundle["evidence_snapshot_ids"]
    assert bundle["release"]["release_digest"]
    assert bundle["release"]["manifest_digest"]


def test_result_digest_is_stable_across_identical_rerun_lineage(
    tmp_path: Path,
) -> None:
    """result_digest identifies the scientific inputs and outputs and is
    stable across an identical rerun, independent of job/attempt/lineage."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"], worker_id=service._worker_subject)
    outcome = service.process_job(job["job_id"])
    bundle = service.query_bundle(outcome["bundle_id"])
    rerun_job = service.rerun_job(job["job_id"], worker_id=service._worker_subject)
    rerun_outcome = service.process_job(rerun_job["job_id"])
    rerun_bundle = service.query_bundle(rerun_outcome["bundle_id"])
    assert rerun_bundle["result_digest"] == bundle["result_digest"]


def test_bundle_id_changes_with_job_or_rerun_lineage(tmp_path: Path) -> None:
    """bundle_id covers the complete operational bundle: a rerun with the
    same scientific result gets a distinct bundle id."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"], worker_id=service._worker_subject)
    outcome = service.process_job(job["job_id"])
    bundle_id = outcome["bundle_id"]
    rerun_job = service.rerun_job(job["job_id"], worker_id=service._worker_subject)
    rerun_outcome = service.process_job(rerun_job["job_id"])
    assert rerun_outcome["bundle_id"] != bundle_id
    rerun_bundle = service.query_bundle(rerun_outcome["bundle_id"])
    assert rerun_bundle["rerun_of_bundle_id"] == bundle_id
    assert rerun_bundle["bundle_id"] != rerun_bundle["result_digest"]


def test_replay_uses_bundle_frozen_inputs_without_current_authority_resolution(
    tmp_path: Path,
) -> None:
    """Query, restart replay, and rerun resolve only the frozen evaluation
    store: broken current S01/S08/label authorities must not be consulted."""
    service, command, context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"], worker_id=service._worker_subject)
    outcome = service.process_job(job["job_id"])
    bundle_id = outcome["bundle_id"]

    def broken_provider(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("current authority must not be resolved")

    replay = EvaluationService(
        state_path=tmp_path / "evaluation.sqlite3",
        clock=lambda: 1700000000,
        snapshot_provider=broken_provider,
        release_provider=broken_provider,
        label_manifest_provider=broken_provider,
        business_state_provider=context["measure"],
    )
    assert replay.query_bundle(bundle_id)["bundle_id"] == bundle_id
    rerun_job = replay.rerun_job(job["job_id"], worker_id="s12-replay-worker")
    assert rerun_job["rerun_of_bundle_id"] == bundle_id
    runner_output = _runner_output_for_plan(plan)
    rerun_outcome = replay.process_job(
        rerun_job["job_id"], runner_result=runner_output
    )
    assert rerun_outcome["bundle_id"] is not None


def test_bundle_query_rejects_missing_or_digest_mismatched_manifest(
    tmp_path: Path,
) -> None:
    """Missing bundles are closed failures and a digest-mismatched stored
    bundle is rejected on read."""
    from task4_consistency.controlled.s12 import S12IntegrityError

    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"], worker_id=service._worker_subject)
    outcome = service.process_job(job["job_id"])
    bundle_id = outcome["bundle_id"]
    with pytest.raises(ValueError):
        service.query_bundle("s12_bundle_sha256_" + "0" * 64)
    _tamper_stored_bundle_payload(tmp_path / "evaluation.sqlite3", bundle_id)
    with pytest.raises(S12IntegrityError):
        service.query_bundle(bundle_id)


# ---------------------------------------------------------------------------
# Slice 7 — legacy consumers and prior-artifact rollback
# ---------------------------------------------------------------------------


def test_deployment_and_ci_labels_contain_no_formal_threshold_pass_claim() -> None:
    """docs/DEPLOY.md and scripts/ci_gate.sh carry no formal THRESHOLD PASS
    claim and label the fixture evaluator C-DEV-REG."""
    deploy = (ROOT / "docs" / "DEPLOY.md").read_text(encoding="utf-8")
    ci_gate = (ROOT / "scripts" / "ci_gate.sh").read_text(encoding="utf-8")
    assert "THRESHOLD PASS" not in deploy
    assert "THRESHOLD PASS" not in ci_gate
    assert "C-DEV-REG" in deploy
    assert "C-DEV-REG" in ci_gate


def test_rollback_probe_reopens_business_state_with_fixed_base_code(
    tmp_path: Path,
) -> None:
    """The prior-artifact probe mode reopens the business database created
    before S12 operations with the archived fixed-base code and serves the
    preserved application state."""
    import subprocess as _subprocess

    archive_root = tmp_path / "prior-source"
    archive_root.mkdir(parents=True, exist_ok=True)
    archive = _subprocess.run(
        [
            "git",
            "archive",
            "8a8d7f1bfe37fe97e713dfa92350a56fef31266d",
            "task4_consistency",
            "configs",
            "fixtures",
        ],
        cwd=ROOT,
        check=True,
        stdout=_subprocess.PIPE,
    )
    import tarfile as _tarfile
    import io as _io

    with _tarfile.open(fileobj=_io.BytesIO(archive.stdout), mode="r:") as bundle:
        bundle.extractall(archive_root)
    completed = _subprocess.run(
        [
            str(ROOT / ".venv" / "bin" / "python"),
            "scripts/s12_rollback_probe.py",
            "--prior-source-root",
            str(archive_root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    import json as _json

    report = _json.loads(completed.stdout)
    assert report["probe"] == "PASS"
    assert report["prior_artifact"]["prior_code_serves_business_state"] is True
    assert report["prior_artifact"]["archived_base"] == (
        "8a8d7f1bfe37fe97e713dfa92350a56fef31266d"
    )
