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
import io
import json
import threading
from contextlib import contextmanager
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
    LabelManifestNotFound,
    LabelManifestStore,
    LabelManifestUnavailable,
    S12Unavailable,
    _clopper_pearson_upper,
    _cluster_statistics,
    _opportunity_point_metrics,
    _select_status,
    _server_mandatory_families,
    content_digest,
)
from task4_consistency.controlled.s12_runner import run_s12_runner
from task4_consistency.controlled import s12_runner as s12_runner_module
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


def _business_authority_bindings(
    business_services: list[ControlledScenarioService], governance_service: Any
) -> tuple[Any, Any]:
    def measure() -> dict[str, Any]:
        facts: dict[str, Any] = {}
        for service in business_services:
            facts.update(service.evaluation_business_measurement())
        facts.update(governance_service.evaluation_governance_measurement())
        return facts

    @contextmanager
    def publication_guard(revisions: dict[str, int]):
        with business_services[0].evaluation_publication_fence(
            revisions["s01_authority_revision"]
        ):
            with governance_service.evaluation_publication_fence(
                revisions["s08_authority_revision"]
            ):
                yield

    return measure, publication_guard


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
    measure, publication_guard = _business_authority_bindings(
        business_services, governance_service
    )

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
        business_publication_guard=publication_guard,
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
    evidence_references: list[dict[str, Any]] = []
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
                "difficulty": "standard",
                "data_source": "demo",
                "document_combination": "single",
                "perturbation_family": "none",
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
        evidence_references.append(
            {
                "application_id": application_id,
                "cycle": 1,
                "snapshot_id": snapshot_id,
                "snapshot_digest": snapshot_digest,
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
        "evidence_references": evidence_references,
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

    job = service.start_job("plan-c-1")
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
    returned_runs = [item["run_id"] for item in runner_output["applications"]]
    assert set(returned_runs) == set(plan["run_specs"])
    by_run = {item["run_id"]: item for item in runner_output["applications"]}
    ground_truth: dict[str, str] = {}
    for opportunity in plan["opportunities"]:
        application = by_run[opportunity["run_id"]]
        ground_truth[opportunity["opportunity_id"]] = next(
            check["verdict"]
            for check in application["checks"]
            if check["rule_id"] == opportunity["check_id"]
        )

    # Omit the last application from the runner result via an authenticated
    # budget stop: the parent must materialize an explicit ``missing``
    # prediction and keep the opportunity in its denominator.
    omitted_run = returned_runs[-1]
    omitted_opportunity = next(
        opportunity["opportunity_id"]
        for opportunity in plan["opportunities"]
        if opportunity["run_id"] == omitted_run
    )
    runner_output = _stopped_runner_result(
        runner_output,
        completed_ids=[run for run in returned_runs if run != omitted_run],
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
    rerun_job = service2.rerun_job(job["job_id"])
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
    trimmed = copy.deepcopy(command)
    trimmed["plan_id"] = "plan-c-cancel"
    trimmed["budget"] = {"max_opportunities": 4, "max_runtime_ms": 5000}
    label_root, manifest_id, manifest_digest = _write_label_manifest(
        tmp_path,
        {
            opportunity["opportunity_id"]: "consistent"
            for opportunity in trimmed["opportunities"]
        },
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
    job = service.start_job("plan-c-cancel")
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
        worker_subject="s12-worker-a",
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
        business_publication_guard=context["publication_guard"],
    )
    service_b = EvaluationService(
        state_path=eval_path,
        clock=clock,
        worker_subject="s12-worker-b",
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
        business_publication_guard=context["publication_guard"],
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
    job = service_a.start_job(plan["plan_id"])

    worker_a_results: dict[str, Any] = {}

    def run_a() -> None:
        worker_a_results["outcome"] = service_a.process_job(
            job["job_id"])

    thread_a = threading.Thread(target=run_a)
    thread_a.start()
    assert claimed.wait(timeout=30)
    # Worker B cannot claim while the lease is live.
    busy = service_b.process_job(
        job["job_id"], runner_result=runner_output
    )
    assert busy["status"] == "busy"
    assert busy["reason_code"] == "JOB_LEASE_ACTIVE"
    # The lease expires: worker B reclaims with a higher fence/attempt.
    clock_state["now"] += 31
    winner = service_b.process_job(
        job["job_id"], runner_result=runner_output
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
        business_publication_guard=context["publication_guard"],
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
    measure, publication_guard = _business_authority_bindings(
        business_services, governance_service
    )

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
        business_publication_guard=publication_guard,
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
        "publication_guard": publication_guard,
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
        opportunity["run_id"] for opportunity in plan["opportunities"]
    }
    assert len(plan["run_specs"]) == len(_context["admitted"])
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
    tampered_snapshot["evidence_references"][0]["snapshot_digest"] = "0" * 64
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
    with pytest.raises(LabelManifestNotFound):
        service.freeze_plan(unknown_manifest)
    fabricated = copy.deepcopy(command)
    fabricated["environment"] = {"python": "9.9.9", "evaluator_build": "fake"}
    with pytest.raises(ValueError):
        service.freeze_plan(fabricated)
    fabricated = copy.deepcopy(command)
    fabricated["run_specs"] = {"app": {"run_id": "fake"}}
    with pytest.raises(ValueError):
        service.freeze_plan(fabricated)


def test_label_storage_root_unavailable_and_healthy_unknown_reference(
    tmp_path: Path,
) -> None:
    manifest_id = "manifest_sha256_" + "0" * 64
    missing_root = LabelManifestStore(tmp_path / "missing-label-root")
    with pytest.raises(LabelManifestUnavailable):
        missing_root.resolve(manifest_id, "0" * 64)

    healthy_root = tmp_path / "healthy-empty-label-root"
    healthy_root.mkdir()
    with pytest.raises(LabelManifestNotFound):
        LabelManifestStore(healthy_root).resolve(manifest_id, "0" * 64)


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
    job = service.start_job(plan["plan_id"])
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
    job = service.start_job(plan["plan_id"])
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
        scope="R-E2E",
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
    job = service.start_job(plan["plan_id"])
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
    job = service.start_job(plan["plan_id"])
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
    for index, opportunity in enumerate(incomplete["opportunities"]):
        opportunity["track"] = "R"
        opportunity["target_scope"] = (
            "R-E2E" if index < 2 else "R-T4-conditional"
        )
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
    complete["scope_declared"] = "R-E2E"
    complete["mandatory_check_families"] = _server_mandatory_families(
        {
            opportunity["check_id"]
            for opportunity in complete["opportunities"][:2]
        }
    )
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
    accepted["opportunities"][0]["variant_id"] = "variant-a"
    accepted["opportunities"][1]["variant_id"] = "variant-b"
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
# R2 Slice 1 — exact scope and run-reference identity
# ---------------------------------------------------------------------------


def _r2_baseline(tmp_path: Path) -> dict[str, Any]:
    """One real S01/S08/label authority context for R2 reference commands."""
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    business_services, admitted, snapshots, business_path = _make_business_harness(
        tmp_path, rules_path
    )
    governance_service, release_id, release_digest, _manifest = _make_governed_release(
        tmp_path
    )
    label_root, manifest_id, manifest_digest = _write_label_manifest(
        tmp_path,
        labels={
            f"opp-{index}": "consistent" for index in range(len(admitted))
        },
    )
    return {
        "admitted": admitted,
        "snapshots": snapshots,
        "business_services": business_services,
        "governance_service": governance_service,
        "release_id": release_id,
        "release_digest": release_digest,
        "manifest_id": manifest_id,
        "manifest_digest": manifest_digest,
        "label_root": label_root,
        "business_path": business_path,
    }


def _r2_reference_command(
    context: dict[str, Any],
    *,
    plan_id: str = "plan-r2-1",
    scope_declared: str = "C",
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The R2 reference command: evidence references are a LIST of immutable
    run references (application, cycle, snapshot); one opportunity per run."""
    check_by_scenario = {
        "app_r53_bad_engine.json": "R_ENGINE_CROSS",
        "app_s04_bad_vin.json": "R_VIN_CROSS",
        "app_bad_brand.json": "R_BRAND_CROSS",
        "app_bad_model.json": "R_MODEL_CROSS",
    }
    admitted = context["admitted"]
    snapshot_by_application = context["snapshots"]
    opportunities: list[dict[str, Any]] = []
    clusters: list[dict[str, Any]] = []
    evidence_references: list[dict[str, Any]] = []
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
        evidence_references.append(
            {
                "application_id": application_id,
                "cycle": 1,
                "snapshot_id": snapshot_id,
                "snapshot_digest": snapshot_digest,
            }
        )
    default_labels = {
        opportunity["opportunity_id"]: "consistent"
        for opportunity in opportunities
    }
    return {
        "schema_version": "s12-plan-command/1",
        "plan_id": plan_id,
        "scope_declared": scope_declared,
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
        "evidence_references": evidence_references,
        "release_reference": {
            "release_id": context["release_id"],
            "release_digest": context["release_digest"],
        },
        "label_manifest": {
            "manifest_id": context["manifest_id"],
            "manifest_digest": context["manifest_digest"],
        },
        "mandatory_check_families": [
            {
                "family_id": "cross-document",
                "check_ids": ["R_ENGINE_CROSS", "R_VIN_CROSS"],
            },
            {
                "family_id": "brand-model",
                "check_ids": ["R_BRAND_CROSS", "R_MODEL_CROSS"],
            },
        ],
    }


def _r2_service(
    tmp_path: Path, context: dict[str, Any], *, snapshot_provider: Any = None
) -> EvaluationService:
    measure, publication_guard = _business_authority_bindings(
        context["business_services"], context["governance_service"]
    )

    return EvaluationService(
        state_path=tmp_path / "evaluation.sqlite3",
        clock=lambda: 1700000000,
        snapshot_provider=snapshot_provider
        or (lambda application_id, snapshot_id: context["business_services"][
            0
        ].evaluation_evidence_snapshot(
            application_id=application_id, snapshot_id=snapshot_id
        )),
        release_provider=lambda release_id, release_digest: context[
            "governance_service"
        ].resolve_evaluation_release(
            release_id=release_id, release_digest=release_digest
        ),
        label_manifest_provider=LabelManifestStore(context["label_root"]).resolve,
        business_state_provider=measure,
        business_publication_guard=publication_guard,
    )


def test_freeze_rejects_unknown_or_mixed_formal_scope(tmp_path: Path) -> None:
    """Freeze accepts only the finite formal scopes C, R-E2E and
    R-T4-conditional; mixed or unknown scope values are command errors."""
    context = _r2_baseline(tmp_path)
    service = _r2_service(tmp_path, context)
    for bad_scope in ("R+C", "unknown-scope", ""):
        command = _r2_reference_command(context, scope_declared=bad_scope)
        with pytest.raises(ValueError, match="formal scope"):
            service.freeze_plan(command)
    command = _r2_reference_command(context, scope_declared="C")
    assert service.freeze_plan(command)["scope"] == "C"


def test_formal_status_uses_only_the_selected_scope_and_required_views() -> None:
    """Status evaluates only the selected formal scope: C uses the C track;
    an R scope uses the R track plus exactly its required view."""
    opportunities, clusters, predictions = _synthetic_track(
        consistent_clusters=60, inconsistent_clusters=100
    )
    passing_c = _cluster_statistics(
        opportunities, clusters, predictions, seed=15, membership="C"
    )
    failing_r = _cluster_statistics(
        opportunities, clusters, {"opp-0": "wrong"}, seed=16, membership="R"
    )
    status, reasons = _select_status(
        {"R": failing_r, "C": passing_c},
        {"R-E2E": _empty_stats("R-E2E"), "R-T4-conditional": _empty_stats("R-T4-conditional")},
        scope="C",
    )
    assert status == "PASS(scope=C)", reasons

    passing_r = _cluster_statistics(
        opportunities, clusters, predictions, seed=17, membership="R"
    )
    status, _reasons = _select_status(
        {"R": failing_r, "C": passing_c},
        {"R-E2E": passing_r, "R-T4-conditional": failing_r},
        scope="R-E2E",
    )
    assert status == "PASS(scope=R-E2E)"

    status, _reasons = _select_status(
        {"R": failing_r, "C": passing_c},
        {"R-E2E": failing_r, "R-T4-conditional": passing_r},
        scope="R-T4-conditional",
    )
    assert status == "PASS(scope=R-T4-conditional)"

    with pytest.raises(ValueError, match="formal scope"):
        _select_status(
            {"R": passing_r, "C": passing_c},
            {"R-E2E": _empty_stats("R-E2E"), "R-T4-conditional": _empty_stats("R-T4-conditional")},
            scope="R",
        )


def test_duplicate_track_and_view_membership_ids_are_rejected(tmp_path: Path) -> None:
    """Duplicate membership IDs are rejected before set conversion."""
    context = _r2_baseline(tmp_path)
    service = _r2_service(tmp_path, context)
    command = _r2_reference_command(context)
    duplicated = copy.deepcopy(command)
    duplicated["tracks"]["C"]["opportunities"].append("opp-0")
    with pytest.raises(ValueError, match="duplicate"):
        service.freeze_plan(duplicated)
    duplicated_view = copy.deepcopy(command)
    duplicated_view["views"]["R-E2E"]["opportunities"] = ["opp-0", "opp-0"]
    with pytest.raises(ValueError, match="duplicate"):
        service.freeze_plan(duplicated_view)


def test_opportunity_application_and_variant_belong_to_base_cluster(tmp_path: Path) -> None:
    """Every opportunity application (and optional variant) is owned by the
    opportunity's named base cluster; cross-cluster ownership is rejected."""
    context = _r2_baseline(tmp_path)
    service = _r2_service(tmp_path, context)
    command = _r2_reference_command(context)
    cross_cluster = copy.deepcopy(command)
    cross_cluster["opportunities"][0]["cluster"] = "cl-1"
    with pytest.raises(ValueError, match="base cluster"):
        service.freeze_plan(cross_cluster)

    with_variant = copy.deepcopy(command)
    with_variant["clusters"][0]["variants"] = ["variant-0"]
    with_variant["opportunities"][0]["variant_id"] = "variant-1"
    with pytest.raises(ValueError, match="base cluster"):
        service.freeze_plan(with_variant)


def test_declared_variants_must_exactly_match_opportunity_universe(
    tmp_path: Path,
) -> None:
    context = _r2_baseline(tmp_path)
    service = _r2_service(tmp_path, context)
    command = _r2_reference_command(context)
    command["clusters"][0]["variants"] = ["variant-uncovered"]

    with pytest.raises(ValueError, match="variant universe"):
        service.freeze_plan(command)


def test_clustered_application_run_reference_and_opportunity_universes_match_exactly(
    tmp_path: Path,
) -> None:
    """The clustered application universe, the declared run-reference
    universe and the opportunity universe match exactly."""
    context = _r2_baseline(tmp_path)
    service = _r2_service(tmp_path, context)
    command = _r2_reference_command(context)
    # Remove one opportunity (and its track membership) while keeping its
    # evidence reference: the declared run reference has no opportunity.
    extra_reference = copy.deepcopy(command)
    dropped = extra_reference["opportunities"].pop(0)
    extra_reference["tracks"]["C"]["opportunities"].remove(dropped["opportunity_id"])
    replacement = copy.deepcopy(extra_reference["opportunities"][0])
    replacement["opportunity_id"] = "opp-mandatory-replacement"
    replacement["check_id"] = dropped["check_id"]
    extra_reference["opportunities"].append(replacement)
    extra_reference["tracks"]["C"]["opportunities"].append(
        replacement["opportunity_id"]
    )
    extra_reference["mandatory_check_families"] = _server_mandatory_families(
        {
            opportunity["check_id"]
            for opportunity in extra_reference["opportunities"]
        }
    )
    remaining_labels = {
        opportunity["opportunity_id"]: "consistent"
        for opportunity in extra_reference["opportunities"]
    }
    label_root, manifest_id, manifest_digest = _write_label_manifest(
        tmp_path, remaining_labels
    )
    extra_reference["label_manifest"] = {
        "manifest_id": manifest_id,
        "manifest_digest": manifest_digest,
    }
    with pytest.raises(ValueError, match="no opportunity"):
        service.freeze_plan(extra_reference)

    unowned = copy.deepcopy(command)
    unowned["opportunities"][0]["application_id"] = "not-in-any-cluster"
    with pytest.raises(ValueError, match="base cluster"):
        service.freeze_plan(unowned)


def test_same_application_two_cycles_and_snapshots_freeze_as_distinct_runs(
    tmp_path: Path,
) -> None:
    """Two immutable snapshots of one application across cycles freeze as two
    distinct runs keyed by immutable run-reference identity."""
    context = _r2_baseline(tmp_path)
    digest_a = content_digest({"application_id": "app-dual", "cycle": 1})
    digest_b = content_digest({"application_id": "app-dual", "cycle": 2})
    snapshots_by_id = {
        f"snapshot_sha256_{digest_a}": {
            "application_id": "app-dual",
            "cycle": 1,
            "evidence_snapshot_id": f"snapshot_sha256_{digest_a}",
            "evidence_snapshot_digest": digest_a,
            "evidence_snapshot": {"application_id": "app-dual", "cycle": 1},
            "evidence_revision": 1,
        },
        f"snapshot_sha256_{digest_b}": {
            "application_id": "app-dual",
            "cycle": 2,
            "evidence_snapshot_id": f"snapshot_sha256_{digest_b}",
            "evidence_snapshot_digest": digest_b,
            "evidence_snapshot": {"application_id": "app-dual", "cycle": 2},
            "evidence_revision": 2,
        },
    }
    provider = lambda application_id, snapshot_id: snapshots_by_id[snapshot_id]
    service = _r2_service(tmp_path, context, snapshot_provider=provider)
    label_root, manifest_id, manifest_digest = _write_label_manifest(
        tmp_path,
        {
            "opp-a": "consistent",
            "opp-b": "consistent",
            "opp-mandatory-1": "consistent",
            "opp-mandatory-2": "consistent",
            "opp-mandatory-3": "consistent",
        },
    )
    command = {
        "schema_version": "s12-plan-command/1",
        "plan_id": "plan-dual",
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
        "clusters": [
            {
                "cluster_id": "cl-0",
                "stratum": "c",
                "applications": ["app-dual"],
                "usage": "development",
            }
        ],
        "tracks": {
            "R": {"opportunities": []},
            "C": {"opportunities": ["opp-a", "opp-b"]},
        },
        "views": {
            "R-E2E": {"opportunities": []},
            "R-T4-conditional": {"opportunities": []},
        },
        "opportunities": [
            {
                "opportunity_id": "opp-a",
                "track": "C",
                "cluster": "cl-0",
                "application_id": "app-dual",
                "cycle": 1,
                "check_id": "R_ENGINE_CROSS",
                "target_scope": "C",
                "evidence_snapshot_id": f"snapshot_sha256_{digest_a}",
            },
            {
                "opportunity_id": "opp-b",
                "track": "C",
                "cluster": "cl-0",
                "application_id": "app-dual",
                "cycle": 2,
                "check_id": "R_ENGINE_CROSS",
                "target_scope": "C",
                "evidence_snapshot_id": f"snapshot_sha256_{digest_b}",
            },
        ],
        "evidence_references": [
            {
                "application_id": "app-dual",
                "cycle": 1,
                "snapshot_id": f"snapshot_sha256_{digest_a}",
                "snapshot_digest": digest_a,
            },
            {
                "application_id": "app-dual",
                "cycle": 2,
                "snapshot_id": f"snapshot_sha256_{digest_b}",
                "snapshot_digest": digest_b,
            },
        ],
        "release_reference": {
            "release_id": context["release_id"],
            "release_digest": context["release_digest"],
        },
        "label_manifest": {
            "manifest_id": manifest_id,
            "manifest_digest": manifest_digest,
        },
        "mandatory_check_families": [
            {
                "family_id": "cross-document",
                "check_ids": ["R_ENGINE_CROSS", "R_VIN_CROSS"],
            },
            {
                "family_id": "brand-model",
                "check_ids": ["R_BRAND_CROSS", "R_MODEL_CROSS"],
            },
        ],
    }
    for index, check_id in enumerate(
        ("R_VIN_CROSS", "R_BRAND_CROSS", "R_MODEL_CROSS"),
        start=1,
    ):
        command["opportunities"].append(
            {
                "opportunity_id": f"opp-mandatory-{index}",
                "track": "C",
                "cluster": "cl-0",
                "application_id": "app-dual",
                "cycle": 1,
                "check_id": check_id,
                "target_scope": "C",
                "evidence_snapshot_id": f"snapshot_sha256_{digest_a}",
            }
        )
        command["tracks"]["C"]["opportunities"].append(
            f"opp-mandatory-{index}"
        )
    plan = service.freeze_plan(command)
    run_specs = plan["run_specs"]
    assert len(run_specs) == 5
    assert {spec["cycle"] for spec in run_specs.values()} == {1, 2}
    assert {spec["evidence_snapshot_id"] for spec in run_specs.values()} == {
        f"snapshot_sha256_{digest_a}",
        f"snapshot_sha256_{digest_b}",
    }
    assert all(spec["application_id"] == "app-dual" for spec in run_specs.values())
    target_runs = {
        opportunity["run_id"]
        for opportunity in plan["opportunities"]
        if opportunity["opportunity_id"] in {"opp-a", "opp-b"}
    }
    assert len(target_runs) == 2
    assert target_runs <= set(run_specs)


# ---------------------------------------------------------------------------
# R2 Slice 2 — complete required metrics (ADR-0007 §4 auxiliary rates)
# ---------------------------------------------------------------------------


def _auxiliary_metric_fixture() -> tuple[
    list[dict[str, Any]], dict[str, str], dict[str, float]
]:
    """Ten frozen opportunities covering every prediction outcome with gold
    consistent/inconsistent/indeterminate/not_applicable, and the exact
    expected values for every required auxiliary metric."""
    golds = (
        ["consistent"] * 4
        + ["inconsistent"] * 4
        + ["indeterminate"]
        + ["not_applicable"]
    )
    predictions = {
        "opp-0": "consistent",
        "opp-1": "consistent",
        "opp-2": "inconsistent",
        "opp-3": "skipped",
        "opp-4": "inconsistent",
        "opp-5": "inconsistent",
        "opp-6": "consistent",
        "opp-7": "uncertain",
        "opp-8": "missing",
        "opp-9": "error",
    }
    opportunities = [
        {
            "opportunity_id": f"opp-{index}",
            "track": "C",
            "cluster": f"cl-{index}",
            "label": gold,
        }
        for index, gold in enumerate(golds)
    ]
    expected = {
        "labelability": 8 / 9,  # not_applicable is outside the denominator
        "uncertain_on_inconsistent": 0.25,  # 1 uncertain among 4 gold-inconsistent
        "skipped_rate": 1 / 9,
        "missing_rate": 1 / 9,
        "error_rate": 0.0,
        "conditional_fpr": 1 / 3,  # 1 fp among 3 decisive gold-consistent
    }
    return opportunities, predictions, expected


def test_statistics_report_labelability_and_all_auxiliary_rates() -> None:
    """Point estimates carry every required auxiliary rate: labelability,
    uncertain-on-inconsistent, skipped, missing, error, and conditional
    FPR, alongside the existing primary metrics."""
    opportunities, predictions, expected = _auxiliary_metric_fixture()
    metrics = _opportunity_point_metrics(opportunities, predictions)
    point = metrics["point"]
    for name, value in expected.items():
        assert name in point, name
        assert point[name] == value, (name, point[name])
    for name in (
        "coverage",
        "false_positive_rate",
        "false_negative_rate",
        "miss_rate",
    ):
        assert name in point, name


def test_auxiliary_metric_denominators_follow_fixed_applicability_rules() -> None:
    """Each auxiliary metric uses its fixed applicable-opportunity
    denominator; empty or degenerate denominators never silently change."""
    opportunities, predictions, _expected = _auxiliary_metric_fixture()
    metrics = _opportunity_point_metrics(opportunities, predictions)
    denominators = metrics["denominators"]
    assert denominators["E_all"] == 9
    assert denominators["applicable_opportunities"] == 9
    assert denominators["n_consistent"] == 4
    assert denominators["n_inconsistent"] == 4
    assert denominators["n_consistent_decisive"] == 3
    # Every auxiliary metric carries its explicit denominator.
    for name in (
        "labelability",
        "uncertain_on_inconsistent",
        "skipped_rate",
        "missing_rate",
        "error_rate",
        "conditional_fpr",
    ):
        assert name in metrics["denominators"], name
    assert metrics["denominators"]["labelability"] == 9
    assert metrics["denominators"]["uncertain_on_inconsistent"] == 4
    assert metrics["denominators"]["skipped_rate"] == 9
    assert metrics["denominators"]["missing_rate"] == 9
    assert metrics["denominators"]["error_rate"] == 9
    assert metrics["denominators"]["conditional_fpr"] == 3
    # A degenerate class denominator yields the contract-defined 0.0 rate
    # without silently shrinking the denominator.
    only_consistent = [opportunities[0]]
    degenerate = _opportunity_point_metrics(
        only_consistent, {"opp-0": "consistent"}
    )
    assert degenerate["point"]["uncertain_on_inconsistent"] == 0.0
    assert degenerate["point"]["conditional_fpr"] == 0.0
    assert degenerate["denominators"]["uncertain_on_inconsistent"] == 0


def test_not_applicable_is_excluded_from_auxiliary_denominators() -> None:
    opportunities = [
        {"opportunity_id": "applicable", "label": "consistent"},
        {"opportunity_id": "outside", "label": "not_applicable"},
    ]
    metrics = _opportunity_point_metrics(
        opportunities,
        {"applicable": "consistent", "outside": "error"},
    )

    assert metrics["denominators"]["applicable_opportunities"] == 1
    assert metrics["denominators"]["labelability"] == 1
    assert metrics["denominators"]["error_rate"] == 1
    assert metrics["point"]["labelability"] == 1.0
    assert metrics["point"]["error_rate"] == 0.0


def test_required_auxiliary_metrics_are_global_and_in_every_required_stratum(
    tmp_path: Path,
) -> None:
    """The auxiliary rates appear in the global track blocks, the view
    blocks, and every published required stratum."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"])
    outcome = service.process_job(job["job_id"])
    bundle = service.query_bundle(outcome["bundle_id"])
    auxiliary = {
        "labelability",
        "uncertain_on_inconsistent",
        "skipped_rate",
        "missing_rate",
        "error_rate",
        "conditional_fpr",
    }
    for block in (
        bundle["tracks"]["C"],
        bundle["tracks"]["R"],
        bundle["views"]["R-E2E"],
        bundle["views"]["R-T4-conditional"],
    ):
        assert auxiliary <= set(block["point"]), block["membership"]
    for group_by, groups in bundle["strata"].items():
        for _value, statistics in groups.items():
            assert auxiliary <= set(statistics["point"]), (group_by, statistics)


# ---------------------------------------------------------------------------
# R2 Slice 3 — authenticated runner identity and stop
# ---------------------------------------------------------------------------


def _invalidate_and_run(
    service: EvaluationService,
    plan: dict[str, Any],
    tampered: dict[str, Any],
    expected_reason: str,
) -> dict[str, Any]:
    """Start a fresh job, process the tampered result, and assert the closed
    INVALID diagnostic with the exact reason and no bundle."""
    job = service.start_job(plan["plan_id"])
    outcome = service.process_job(job["job_id"], runner_result=tampered)
    assert outcome["status"] == "INVALID", outcome
    assert expected_reason in (outcome.get("reason_codes") or []), outcome
    assert outcome["bundle_id"] is None
    return outcome


def _recompute_digest(tampered: dict[str, Any]) -> dict[str, Any]:
    tampered["digest"] = content_digest(
        {key: value for key, value in tampered.items() if key != "digest"}
    )
    return tampered


def test_runner_error_requires_frozen_run_reference_application_and_run_id(
    tmp_path: Path,
) -> None:
    """Every runner record -- success or error -- must bind the frozen
    run-reference id, the application id and the run id before any
    outcome-specific validation."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    runner_output = _runner_output_for_plan(plan)

    missing_run_id = copy.deepcopy(runner_output)
    del missing_run_id["applications"][0]["run_id"]
    _invalidate_and_run(
        service, plan, _recompute_digest(missing_run_id), "RUNNER_OUTPUT_MALFORMED"
    )

    forged_run = copy.deepcopy(runner_output)
    forged_run["applications"][0]["run_id"] = "forged-run"
    forged_run["applications"][0]["error"] = "CHECKER_EXECUTION_FAILED"
    _invalidate_and_run(
        service, plan, _recompute_digest(forged_run), "RUNNER_UNKNOWN_APPLICATION"
    )

    cross_application_error = copy.deepcopy(runner_output)
    cross_application_error["applications"][0]["run_id"] = runner_output[
        "applications"
    ][1]["run_id"]
    cross_application_error["applications"][0][
        "error"
    ] = "CHECKER_EXECUTION_FAILED"
    _invalidate_and_run(
        service,
        plan,
        _recompute_digest(cross_application_error),
        "RUNNER_IDENTITY_MISMATCH",
    )


def test_runner_result_rejects_duplicate_or_cross_application_run_identity(
    tmp_path: Path,
) -> None:
    """Duplicate run identities (in records or in the completion list) and
    cross-application run identities are INVALID."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    runner_output = _runner_output_for_plan(plan)

    duplicated_record = copy.deepcopy(runner_output)
    duplicated_record["applications"].append(
        copy.deepcopy(duplicated_record["applications"][0])
    )
    _invalidate_and_run(
        service,
        plan,
        _recompute_digest(duplicated_record),
        "RUNNER_DUPLICATE_APPLICATION",
    )

    duplicated_completion = copy.deepcopy(runner_output)
    duplicated_completion["stop"]["completed_run_ids"] = [
        duplicated_completion["stop"]["completed_run_ids"][0],
        *duplicated_completion["stop"]["completed_run_ids"],
    ]
    _invalidate_and_run(
        service,
        plan,
        _recompute_digest(duplicated_completion),
        "RUNNER_STOP_OBSERVATION_INVALID",
    )

    cross_application = copy.deepcopy(runner_output)
    other_application = next(
        application["application_id"]
        for application in runner_output["applications"]
        if application["run_id"] != runner_output["applications"][0]["run_id"]
    )
    cross_application["applications"][0]["application_id"] = other_application
    _invalidate_and_run(
        service,
        plan,
        _recompute_digest(cross_application),
        "RUNNER_IDENTITY_MISMATCH",
    )


def test_plan_exhausted_requires_every_frozen_run_reference_exactly_once(
    tmp_path: Path,
) -> None:
    """A plan-exhausted stop must account for the complete frozen run
    universe exactly once: a subset or duplicated completion list is
    INVALID."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    runner_output = _runner_output_for_plan(plan)
    assert runner_output["stop"]["stop_reason"] == "plan-exhausted"

    subset = copy.deepcopy(runner_output)
    subset["applications"] = subset["applications"][:2]
    subset["stop"]["completed_run_ids"] = [
        application["run_id"] for application in subset["applications"]
    ]
    _invalidate_and_run(
        service,
        plan,
        _recompute_digest(subset),
        "RUNNER_STOP_OBSERVATION_INVALID",
    )

    duplicated = copy.deepcopy(runner_output)
    duplicated["stop"]["completed_run_ids"] = [
        duplicated["stop"]["completed_run_ids"][0],
        *duplicated["stop"]["completed_run_ids"],
    ]
    _invalidate_and_run(
        service,
        plan,
        _recompute_digest(duplicated),
        "RUNNER_STOP_OBSERVATION_INVALID",
    )

    complete = copy.deepcopy(runner_output)
    job = service.start_job(plan["plan_id"])
    outcome = service.process_job(job["job_id"], runner_result=complete)
    assert outcome["status"] != "INVALID"
    assert outcome["bundle_id"] is not None


def test_budget_stop_rejects_elapsed_time_outside_frozen_global_budget(
    tmp_path: Path,
) -> None:
    """The observed elapsed time of a budget stop must respect the frozen
    global runtime window; an over-budget elapsed vector is INVALID."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    max_runtime_ms = plan["budget"]["max_runtime_ms"]
    runner_output = _runner_output_for_plan(plan)

    over_budget = copy.deepcopy(runner_output)
    over_budget["stop"]["stop_reason"] = "budget-or-plan"
    over_budget["stop"]["elapsed_ms"] = max_runtime_ms * 3
    _invalidate_and_run(
        service,
        plan,
        _recompute_digest(over_budget),
        "RUNNER_STOP_OBSERVATION_INVALID",
    )

    within_budget = copy.deepcopy(runner_output)
    within_budget["stop"]["stop_reason"] = "budget-or-plan"
    within_budget["stop"]["elapsed_ms"] = max_runtime_ms
    _recompute_digest(within_budget)
    job = service.start_job(plan["plan_id"])
    outcome = service.process_job(job["job_id"], runner_result=within_budget)
    assert outcome["status"] != "INVALID"


def test_incomplete_budget_stop_is_always_insufficient() -> None:
    """A stop observation that does not satisfy the frozen stop rule gates
    formal status selection: the run is INSUFFICIENT even when the selected
    scope's statistics would otherwise pass."""
    opportunities, clusters, predictions = _synthetic_track(
        consistent_clusters=60, inconsistent_clusters=100
    )
    passing = _cluster_statistics(
        opportunities, clusters, predictions, seed=30, membership="C"
    )
    assert passing["conclusion"] == "pass"
    status, reasons = _select_status(
        {"R": _empty_stats("R"), "C": passing},
        {"R-E2E": _empty_stats("R-E2E"), "R-T4-conditional": _empty_stats("R-T4-conditional")},
        scope="C",
        stop_rule_satisfied=False,
    )
    assert status == "INSUFFICIENT"
    assert any("stop" in reason for reason in reasons)


# ---------------------------------------------------------------------------
# R2 Slice 4 — complete authority vectors reject any business change
# ---------------------------------------------------------------------------


def test_publication_rejects_any_s01_or_s08_vector_change(tmp_path: Path) -> None:
    """A change to any covered S01 or S08 row or payload between freeze and
    terminal publication prevents the formal bundle: the complete measured
    vectors must be unchanged."""
    service, command, context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"])
    store = SQLiteTargetStore(context["business_path"])
    store.reload()
    application = next(iter(store.applications.values()))
    application["lifecycle_revision"] = (
        int(application["lifecycle_revision"] or 0) - 1
    )
    store.persist()
    outcome = service.process_job(job["job_id"])
    assert outcome["status"] == "INVALID"
    assert outcome["bundle_id"] is None
    assert any(
        reason.startswith("BUSINESS_AUTHORITY_CHANGED:")
        for reason in (outcome.get("reason_codes") or [])
    )


def test_healthy_unknown_snapshot_reference_is_an_invalid_command(
    tmp_path: Path,
) -> None:
    """A healthy authority with an unknown snapshot reference is a caller
    command error (ValueError), not an authority outage."""
    context = _r2_baseline(tmp_path)
    service = _r2_service(tmp_path, context)
    command = _r2_reference_command(context)
    command["evidence_references"][0]["snapshot_id"] = (
        "snapshot_sha256_" + "b" * 64
    )
    command["evidence_references"][0]["snapshot_digest"] = "b" * 64
    with pytest.raises(ValueError):
        service.freeze_plan(command)


# ---------------------------------------------------------------------------
# R2 Slice 5 — scientific identity and self-contained replay
# ---------------------------------------------------------------------------


def _scientific_harness(
    tmp_path: Path, *, clock: Any = None
) -> tuple[EvaluationService, dict[str, Any], dict[str, Any]]:
    service, command, context = _slice1_harness(tmp_path)
    if clock is not None:
        service = EvaluationService(
            state_path=tmp_path / "evaluation.sqlite3",
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
            label_manifest_provider=LabelManifestStore(
                context["label_root"]
            ).resolve,
            business_state_provider=context["measure"],
            business_publication_guard=context["publication_guard"],
        )
    return service, command, context


def test_result_digest_ignores_plan_id_freeze_time_job_attempt_worker_and_lineage(
    tmp_path: Path,
) -> None:
    """result_digest identifies the scientific inputs and observed results:
    operational plan id, freeze time, job/attempt identity, worker and
    lineage never change it."""
    _service_a, command, context = _slice1_harness(tmp_path)
    service_a = EvaluationService(
        state_path=tmp_path / "evaluation.sqlite3",
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
        business_publication_guard=context["publication_guard"],
    )
    plan_a = service_a.freeze_plan(command)
    later_command = copy.deepcopy(command)
    later_command["plan_id"] = "plan-c-1-later-clock"
    service_b = EvaluationService(
        state_path=tmp_path / "evaluation-later.sqlite3",
        clock=lambda: 1700000001,
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
        business_publication_guard=context["publication_guard"],
    )
    plan_b = service_b.freeze_plan(later_command)
    assert plan_a["plan_digest"] != plan_b["plan_digest"]
    runner_observation = _runner_output_for_plan(plan_a)
    job_a = service_a.start_job(plan_a["plan_id"])
    outcome_a = service_a.process_job(
        job_a["job_id"], runner_result=copy.deepcopy(runner_observation)
    )
    job_b = service_b.start_job(plan_b["plan_id"])
    outcome_b = service_b.process_job(
        job_b["job_id"], runner_result=copy.deepcopy(runner_observation)
    )
    bundle_a = service_a.query_bundle(outcome_a["bundle_id"])
    bundle_b = service_b.query_bundle(outcome_b["bundle_id"])
    assert bundle_a["result_digest"] == bundle_b["result_digest"]
    assert bundle_a["bundle_id"] != bundle_b["bundle_id"]


def test_result_digest_changes_when_scientific_input_or_observation_changes(
    tmp_path: Path,
) -> None:
    """A scientific input (budget) or an observed result (prediction)
    change alters the result digest."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    runner_output = _runner_output_for_plan(plan)
    job = service.start_job(plan["plan_id"])
    outcome = service.process_job(job["job_id"], runner_result=runner_output)
    baseline = service.query_bundle(outcome["bundle_id"])["result_digest"]

    changed_budget_command = copy.deepcopy(command)
    changed_budget_command["budget"]["max_runtime_ms"] = 6000
    changed_budget_command["plan_id"] = "plan-c-1-changed-budget"
    changed_plan = service.freeze_plan(changed_budget_command)
    changed_job = service.start_job(
        changed_plan["plan_id"]
    )
    changed_outcome = service.process_job(
        changed_job["job_id"],
        runner_result=_runner_output_for_plan(changed_plan),
    )
    changed = service.query_bundle(changed_outcome["bundle_id"])["result_digest"]
    assert changed != baseline

    tampered_observation = copy.deepcopy(runner_output)
    target_opportunity = plan["opportunities"][0]
    target_application = next(
        application
        for application in tampered_observation["applications"]
        if application["run_id"] == target_opportunity["run_id"]
    )
    target_check = next(
        check
        for check in target_application["checks"]
        if check["rule_id"] == target_opportunity["check_id"]
    )
    target_check["verdict"] = "uncertain"
    tampered_observation = _recompute_digest(tampered_observation)
    job2 = service.start_job(plan["plan_id"])
    outcome2 = service.process_job(job2["job_id"], runner_result=tampered_observation)
    changed_observation = service.query_bundle(outcome2["bundle_id"])[
        "result_digest"
    ]
    assert changed_observation != baseline


def test_bundle_embeds_complete_content_addressed_replay_package(
    tmp_path: Path,
) -> None:
    """The published bundle embeds the complete immutable replay package:
    the frozen plan (with checker artifact, RunSpecs and evidence payloads,
    release material, label-manifest content, membership, budgets and
    statistical configuration) plus the observed runner material, all under
    one verified content address."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"])
    outcome = service.process_job(job["job_id"])
    bundle = service.query_bundle(outcome["bundle_id"])
    replay = bundle["replay_package"]
    assert replay["schema_version"] == "s12-replay-package/1"
    embedded_plan = replay["plan"]
    round_trip_plan = json.loads(
        json.dumps(plan, ensure_ascii=False, sort_keys=True)
    )
    assert embedded_plan["plan_id"] == plan["plan_id"]
    assert embedded_plan["plan_digest"] == plan["plan_digest"]
    assert embedded_plan["checker_artifact"] == round_trip_plan["checker_artifact"]
    assert embedded_plan["run_specs"] == round_trip_plan["run_specs"]
    assert embedded_plan["label_manifest"].get("labels") == {
        opportunity["opportunity_id"]: opportunity["label"]
        for opportunity in plan["opportunities"]
    }
    assert replay["predictions"] == bundle["predictions"]
    assert {application["run_id"] for application in replay["applications"]} == set(
        embedded_plan["run_specs"]
    )
    assert replay["runner_result_digest"] == content_digest(
        {
            "schema_version": "s12-runner-result/1",
            "applications": replay["applications"],
            "stop": replay["stop"],
        }
    )
    assert bundle["replay_package_digest"] == content_digest(replay)
    assert replay["plan"]["release"]["release_id"]
    assert replay["plan"]["budget"] == round_trip_plan["budget"]


def test_bundle_query_rejects_missing_or_corrupt_nested_replay_material(
    tmp_path: Path,
) -> None:
    """Query verifies the outer bundle plus the nested replay material: a
    stored bundle whose nested material is missing or digest-mismatched
    fails closed."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"])
    outcome = service.process_job(job["job_id"])
    bundle_id = outcome["bundle_id"]
    assert service.query_bundle(bundle_id)["bundle_id"] == bundle_id

    import sqlite3 as _sqlite3

    from task4_consistency.controlled.s12 import _integrity_digest

    store_path = tmp_path / "evaluation.sqlite3"
    connection = _sqlite3.connect(store_path)
    try:
        row = connection.execute(
            "SELECT payload FROM s12_bundles WHERE item_id = ?", (bundle_id,)
        ).fetchone()
        assert row is not None
        bundle = json.loads(row[0])
        del bundle["replay_package"]["plan"]["checker_artifact"]
        payload_text = json.dumps(
            bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = _integrity_digest("s12_bundles", bundle_id, payload_text)
        connection.execute(
            "UPDATE s12_bundles SET payload = ?, integrity_sha256 = ? "
            "WHERE item_id = ?",
            (payload_text, digest, bundle_id),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises((ValueError, RuntimeError)):
        service.query_bundle(bundle_id)


def _delete_plan_row(state_path: Path, plan_id: str) -> None:
    import sqlite3 as _sqlite3

    connection = _sqlite3.connect(state_path)
    try:
        connection.execute(
            "DELETE FROM s12_plans WHERE item_id = ?", (plan_id,)
        )
        connection.commit()
    finally:
        connection.close()


def test_rerun_uses_source_bundle_after_plan_row_is_removed_or_changed(
    tmp_path: Path,
) -> None:
    """A rerun materializes exclusively from the verified source bundle: it
    survives deletion of the current plan row."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    runner_output = _runner_output_for_plan(plan)
    job = service.start_job(plan["plan_id"])
    outcome = service.process_job(job["job_id"], runner_result=runner_output)
    bundle_id = outcome["bundle_id"]
    _delete_plan_row(tmp_path / "evaluation.sqlite3", plan["plan_id"])
    rerun_job = service.rerun_job(job["job_id"])
    assert rerun_job["rerun_of_bundle_id"] == bundle_id
    rerun_outcome = service.process_job(
        rerun_job["job_id"], runner_result=runner_output
    )
    rerun_bundle = service.query_bundle(rerun_outcome["bundle_id"])
    assert rerun_bundle["rerun_of_bundle_id"] == bundle_id
    assert rerun_bundle["result_digest"] == service.query_bundle(bundle_id)[
        "result_digest"
    ]


def test_rerun_never_resolves_current_authority_providers(tmp_path: Path) -> None:
    """Rerun performs no current snapshot/release/label/business provider
    resolution: the source bundle's frozen package is the only authority."""
    service, command, context = _slice1_harness(tmp_path)
    counters = {"snapshot": 0, "release": 0, "label": 0, "business": 0}

    def counted_snapshot(application_id: str, snapshot_id: str) -> Any:
        counters["snapshot"] += 1
        return context["business_services"][0].evaluation_evidence_snapshot(
            application_id=application_id, snapshot_id=snapshot_id
        )

    def counted_release(release_id: str, release_digest: str) -> Any:
        counters["release"] += 1
        return context["governance_service"].resolve_evaluation_release(
            release_id=release_id, release_digest=release_digest
        )

    def counted_label(manifest_id: str, manifest_digest: str) -> Any:
        counters["label"] += 1
        return LabelManifestStore(context["label_root"]).resolve(
            manifest_id, manifest_digest
        )

    def counted_business() -> Any:
        counters["business"] += 1
        return context["measure"]()

    service = EvaluationService(
        state_path=tmp_path / "evaluation.sqlite3",
        clock=lambda: 1700000000,
        snapshot_provider=counted_snapshot,
        release_provider=counted_release,
        label_manifest_provider=counted_label,
        business_state_provider=counted_business,
        business_publication_guard=context["publication_guard"],
    )
    plan = service.freeze_plan(command)
    runner_output = _runner_output_for_plan(plan)
    job = service.start_job(plan["plan_id"])
    outcome = service.process_job(job["job_id"], runner_result=runner_output)
    freeze_calls = dict(counters)
    assert freeze_calls["snapshot"] > 0
    rerun_job = service.rerun_job(job["job_id"])
    rerun_outcome = service.process_job(
        rerun_job["job_id"], runner_result=runner_output
    )
    assert rerun_outcome["bundle_id"]
    assert counters["snapshot"] == freeze_calls["snapshot"]
    assert counters["release"] == freeze_calls["release"]
    assert counters["label"] == freeze_calls["label"]
    assert counters["business"] == freeze_calls["business"]


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
        if application["run_id"] in completed_ids
    ]
    stop = {
        "stop_reason": "budget-or-plan",
        "elapsed_ms": 1,
        "completed_run_ids": list(completed_ids),
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


@pytest.mark.parametrize("stop_rule", ["plan-exhausted", "budget-or-plan"])
def test_runner_tail_crossing_deadline_cannot_satisfy_formal_stop_rule(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stop_rule: str
) -> None:
    service, command, _context = _slice1_harness(tmp_path)
    command["stop_rule"] = stop_rule
    command["budget"]["max_runtime_ms"] = 5
    plan = service.freeze_plan(command)
    run_id, run_spec = next(iter(plan["run_specs"].items()))
    payload = {
        "schema_version": "s12-runner-request/1",
        "checker_artifact": plan["checker_artifact"],
        "run_specs": {run_id: copy.deepcopy(run_spec)},
        "budget": copy.deepcopy(plan["budget"]),
        "stop_rule": stop_rule,
    }
    stdin = io.TextIOWrapper(
        io.BytesIO(json.dumps(payload).encode("utf-8")), encoding="utf-8"
    )
    stdout = io.StringIO()
    clock = iter((100.0, 100.0, 100.006))
    run_ids = [run_id]
    monkeypatch.setattr(s12_runner_module, "_apply_process_boundaries", lambda: None)
    monkeypatch.setattr(s12_runner_module.sys, "stdin", stdin)
    monkeypatch.setattr(s12_runner_module.sys, "stdout", stdout)
    monkeypatch.setattr(s12_runner_module.time, "monotonic", lambda: next(clock))

    assert s12_runner_module.main() == 0
    result = json.loads(stdout.getvalue())

    assert result["stop"] == {
        "stop_reason": "budget-or-plan",
        "elapsed_ms": 5,
        "completed_run_ids": run_ids,
    }
    assert [application["run_id"] for application in result["applications"]] == (
        run_ids
    )

    job = service.start_job(plan["plan_id"])
    outcome = service.process_job(job["job_id"], runner_result=result)
    bundle = service.query_bundle(outcome["bundle_id"])

    assert bundle["stop_rule_satisfied"] is False


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
        tampered_job = service.start_job(plan["plan_id"])
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
    # A record whose run id is frozen but whose application id is not the
    # frozen application of that run is a cross-application identity.
    _run_case(unknown_app, "RUNNER_IDENTITY_MISMATCH")

    unknown_run = copy.deepcopy(runner_output)
    unknown_run["applications"][0]["run_id"] = "forged-run"
    unknown_run["stop"]["completed_run_ids"] = [
        "forged-run",
        *unknown_run["stop"]["completed_run_ids"][1:],
    ]
    unknown_run["digest"] = content_digest(
        {key: value for key, value in unknown_run.items() if key != "digest"}
    )
    _run_case(unknown_run, "RUNNER_UNKNOWN_APPLICATION")

    duplicated = copy.deepcopy(runner_output)
    duplicated["applications"].append(copy.deepcopy(duplicated["applications"][0]))
    duplicated["stop"]["completed_run_ids"].append(
        duplicated["applications"][0]["run_id"]
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
    job = service.start_job(plan["plan_id"])
    runner_output = _runner_output_for_plan(plan)
    failed_application = runner_output["applications"][0]["application_id"]
    failed_run = runner_output["applications"][0]["run_id"]
    error_result = copy.deepcopy(runner_output)
    error_result["applications"] = [
        {
            "application_id": failed_application,
            "run_id": failed_run,
            "error": "CHECKER_EXECUTION_FAILED",
        },
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
    job = service.start_job(plan["plan_id"])
    runner_output = _runner_output_for_plan(plan)
    assert runner_output["stop"]["stop_reason"] == "budget-or-plan"
    assert len(runner_output["stop"]["completed_run_ids"]) < len(
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
    job = service.start_job(plan["plan_id"])
    runner_output = _runner_output_for_plan(plan)
    completed_ids = runner_output["stop"]["completed_run_ids"]
    stopped = _stopped_runner_result(runner_output, completed_ids=completed_ids[:2])
    outcome = service.process_job(job["job_id"], runner_result=stopped)
    assert outcome["status"] == "INSUFFICIENT"
    bundle = service.query_bundle(outcome["bundle_id"])
    unfinished = [
        opportunity["opportunity_id"]
        for opportunity in plan["opportunities"]
        if opportunity["run_id"] not in completed_ids[:2]
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
    job = service.start_job(plan["plan_id"])
    runner_output = _runner_output_for_plan(plan)
    assert runner_output["stop"]["stop_reason"] == "plan-exhausted"
    outcome = service.process_job(job["job_id"], runner_result=runner_output)
    bundle = service.query_bundle(outcome["bundle_id"])
    assert bundle["stop_reason"] == "plan-exhausted"
    assert bundle["stop_rule_satisfied"] is True
    assert len(bundle["completed_run_ids"]) == len(plan["run_specs"])


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
    job = service.start_job(plan["plan_id"])
    _tamper_stored_job_payload(tmp_path / "evaluation.sqlite3", job["job_id"])
    with pytest.raises(S12IntegrityError):
        service.process_job(job["job_id"])


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
        worker_subject="s12-worker-a",
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
        business_publication_guard=context["publication_guard"],
    )
    service_b = EvaluationService(
        state_path=eval_path,
        clock=clock,
        worker_subject="s12-worker-b",
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
        business_publication_guard=context["publication_guard"],
    )
    job = service_a.start_job(plan["plan_id"])
    worker_a_results: dict[str, Any] = {}

    def run_a() -> None:
        worker_a_results["outcome"] = service_a.process_job(
            job["job_id"])

    thread_a = threading.Thread(target=run_a)
    thread_a.start()
    assert claimed.wait(timeout=30)
    clock_state["now"] += 31
    winner = service_b.process_job(
        job["job_id"], runner_result=runner_output
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
        business_publication_guard=context["publication_guard"],
    )
    assert set(final._store.bundles) == {winner["bundle_id"]}


def test_cancel_claim_diagnostic_and_publish_are_row_scoped_transactions(
    tmp_path: Path,
) -> None:
    """Every mutable job transition persists row-scoped: a fresh authority
    reopens the exact terminal row after each transition."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"])
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
        business_state_provider=_context["measure"],
        business_publication_guard=_context["publication_guard"],
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
        business_publication_guard=context["publication_guard"],
    )
    results: dict[str, Any] = {}

    def start_a() -> None:
        results["a"] = service_a.start_job(plan["plan_id"])

    def start_b() -> None:
        results["b"] = service_b.start_job(plan["plan_id"])

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
        business_publication_guard=context["publication_guard"],
    )
    job = service_a.start_job(plan["plan_id"])
    service_b.cancel_job(job["job_id"])
    outcome = service_a.process_job(
        job["job_id"])
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
    job = service.start_job(plan["plan_id"])
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
    job = service.start_job(plan["plan_id"])
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
    runner_observation = _runner_output_for_plan(plan)
    job = service.start_job(plan["plan_id"])
    outcome = service.process_job(
        job["job_id"], runner_result=copy.deepcopy(runner_observation)
    )
    bundle = service.query_bundle(outcome["bundle_id"])
    rerun_job = service.rerun_job(job["job_id"])
    rerun_outcome = service.process_job(
        rerun_job["job_id"], runner_result=copy.deepcopy(runner_observation)
    )
    rerun_bundle = service.query_bundle(rerun_outcome["bundle_id"])
    assert rerun_bundle["result_digest"] == bundle["result_digest"]


def test_bundle_id_changes_with_job_or_rerun_lineage(tmp_path: Path) -> None:
    """bundle_id covers the complete operational bundle: a rerun with the
    same scientific result gets a distinct bundle id."""
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"])
    outcome = service.process_job(job["job_id"])
    bundle_id = outcome["bundle_id"]
    rerun_job = service.rerun_job(job["job_id"])
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
    job = service.start_job(plan["plan_id"])
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
        business_publication_guard=context["publication_guard"],
    )
    assert replay.query_bundle(bundle_id)["bundle_id"] == bundle_id
    rerun_job = replay.rerun_job(job["job_id"])
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
    job = service.start_job(plan["plan_id"])
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


# ---------------------------------------------------------------------------
# Ticket #28 R2 Slice 7 — exact fixed-base rollback proof (ST-03 / SP-16)
# ---------------------------------------------------------------------------


def _run_rollback_probe(*arguments: str) -> tuple[int, dict[str, Any]]:
    import json as _json
    import subprocess as _subprocess

    completed = _subprocess.run(
        [
            str(ROOT / ".venv" / "bin" / "python"),
            "scripts/s12_rollback_probe.py",
            *arguments,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=240,
    )
    report = _json.loads(completed.stdout) if completed.stdout else {}
    return completed.returncode, report


def test_rollback_probe_extracts_and_executes_the_exact_fixed_base_archive(
    tmp_path: Path,
) -> None:
    """The probe extracts the exact fixed base itself and executes the
    prior-artifact reader with that extraction as the only import root:
    the reported module path lives inside the verified extraction."""
    exit_code, report = _run_rollback_probe(
        "--fixed-base", "8a8d7f1bfe37fe97e713dfa92350a56fef31266d"
    )
    assert exit_code == 0, report
    assert report["probe"] == "PASS"
    prior = report["prior_artifact"]
    assert prior["archived_base"] == "8a8d7f1bfe37fe97e713dfa92350a56fef31266d"
    assert prior["prior_code_serves_business_state"] is True
    assert prior["source_digest"]
    assert prior["tree_digest"] == prior["module_tree_digest"]
    assert "s01.py" in prior["module_path"]


def test_rollback_probe_rejects_prior_source_root_mismatch(tmp_path: Path) -> None:
    """A supplied prior root that does not match the fixed-base archive is
    rejected: the probe terminates nonzero with the comparison recorded."""
    exit_code, report = _run_rollback_probe(
        "--fixed-base",
        "8a8d7f1bfe37fe97e713dfa92350a56fef31266d",
        "--prior-source-root",
        str(ROOT),
    )
    assert exit_code != 0, report
    assert report["probe"] == "FAIL"
    assert report["prior_artifact"]["tree_digest_matches"] is False
    assert report["prior_artifact"]["prior_code_serves_business_state"] is False


# ---------------------------------------------------------------------------
# Ticket #28 R3 Codex repair
# ---------------------------------------------------------------------------


def test_formal_r_view_ignores_aggregate_sibling_and_out_of_scope_gates() -> None:
    """A formal R view owns its conclusion; aggregate R and the sibling view
    remain report-only for that scope."""

    def statistics(membership: str, conclusion: str) -> dict[str, Any]:
        return {
            "membership": membership,
            "opportunity_count": 1,
            "denominators": {"E": 1},
            "estimable": True,
            "not_estimable_reasons": [],
            "conclusion": conclusion,
        }

    status, reasons = _select_status(
        {
            "R": statistics("R", "fail"),
            "C": statistics("C", "fail"),
        },
        {
            "R-E2E": statistics("R-E2E", "pass"),
            "R-T4-conditional": statistics("R-T4-conditional", "fail"),
        },
        scope="R-E2E",
    )

    assert status == "PASS(scope=R-E2E)", reasons


def test_process_uses_scope_local_holdout_and_mandatory_family_gates(
    tmp_path: Path,
) -> None:
    """A C formal run reports and gates only C opportunities even when the
    frozen plan also carries a development-only R view."""
    context = _r2_baseline(tmp_path)
    service = _r2_service(tmp_path, context)
    command = _r2_reference_command(context, scope_declared="C")
    out_of_scope = command["opportunities"][0]
    out_of_scope["track"] = "R"
    out_of_scope["target_scope"] = "R-E2E"
    out_of_scope_id = out_of_scope["opportunity_id"]
    command["tracks"]["R"]["opportunities"] = [out_of_scope_id]
    command["tracks"]["C"]["opportunities"].remove(out_of_scope_id)
    command["views"]["R-E2E"]["opportunities"] = [out_of_scope_id]
    command["mandatory_check_families"][0]["check_ids"].remove(
        out_of_scope["check_id"]
    )

    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"])
    outcome = service.process_job(job["job_id"])
    bundle = service.query_bundle(outcome["bundle_id"])

    reasons = bundle["scope_eligibility"]["reasons"]
    assert all(out_of_scope_id not in reason for reason in reasons), reasons
    assert bundle["mandatory_check_families"]["cross-document"][
        "opportunity_count"
    ] == 1


def test_opportunity_scope_must_match_track_and_view(tmp_path: Path) -> None:
    context = _r2_baseline(tmp_path)
    service = _r2_service(tmp_path, context)

    c_mismatch = _r2_reference_command(context, plan_id="scope-c-mismatch")
    c_mismatch["opportunities"][0]["target_scope"] = "R-E2E"
    with pytest.raises(ValueError, match="target scope"):
        service.freeze_plan(c_mismatch)

    r_mismatch = _r2_reference_command(
        context,
        plan_id="scope-r-mismatch",
        scope_declared="R-E2E",
    )
    opportunity_ids = [
        opportunity["opportunity_id"]
        for opportunity in r_mismatch["opportunities"]
    ]
    for opportunity in r_mismatch["opportunities"]:
        opportunity["track"] = "R"
        opportunity["target_scope"] = "R-T4-conditional"
    r_mismatch["tracks"] = {
        "R": {"opportunities": opportunity_ids},
        "C": {"opportunities": []},
    }
    r_mismatch["views"] = {
        "R-E2E": {"opportunities": opportunity_ids},
        "R-T4-conditional": {"opportunities": []},
    }
    with pytest.raises(ValueError, match="target scope"):
        service.freeze_plan(r_mismatch)


def test_mandatory_family_coverage_is_server_owned_and_exact(
    tmp_path: Path,
) -> None:
    service, command, _context = _slice1_harness(tmp_path)

    omitted = copy.deepcopy(command)
    omitted["mandatory_check_families"].pop()
    with pytest.raises(ValueError, match="server-owned registry"):
        service.freeze_plan(omitted)

    invented = copy.deepcopy(command)
    invented["mandatory_check_families"][0]["family_id"] = "caller-family"
    with pytest.raises(ValueError, match="server-owned registry"):
        service.freeze_plan(invented)

    overlapping = copy.deepcopy(command)
    overlapping["mandatory_check_families"][1]["check_ids"].append(
        "R_ENGINE_CROSS"
    )
    with pytest.raises(ValueError, match="server-owned registry"):
        service.freeze_plan(overlapping)

    uncovered = copy.deepcopy(command)
    uncovered["mandatory_check_families"][0]["check_ids"].remove(
        "R_ENGINE_CROSS"
    )
    with pytest.raises(ValueError, match="server-owned registry"):
        service.freeze_plan(uncovered)


def test_mandatory_checks_must_come_from_governed_release(
    tmp_path: Path,
) -> None:
    context = _r2_baseline(tmp_path)
    service = _r2_service(tmp_path, context)
    command = _r2_reference_command(context, plan_id="unknown-governed-check")
    replaced_check = command["opportunities"][0]["check_id"]
    command["opportunities"][0]["check_id"] = "UNREGISTERED_CHECK"
    for family in command["mandatory_check_families"]:
        if replaced_check in family["check_ids"]:
            family["check_ids"].remove(replaced_check)
    command["mandatory_check_families"].append(
        {
            "family_id": "UNREGISTERED_CHECK",
            "check_ids": ["UNREGISTERED_CHECK"],
        }
    )

    with pytest.raises(ValueError, match="governed release"):
        service.freeze_plan(command)


def test_governed_mandatory_check_cannot_be_omitted_with_its_local_universe(
    tmp_path: Path,
) -> None:
    context = _r2_baseline(tmp_path)
    service = _r2_service(tmp_path, context)
    command = _r2_reference_command(context, plan_id="omitted-governed-check")
    omitted = next(
        opportunity
        for opportunity in command["opportunities"]
        if opportunity["check_id"] == "R_MODEL_CROSS"
    )
    command["opportunities"].remove(omitted)
    command["tracks"]["C"]["opportunities"].remove(omitted["opportunity_id"])
    command["clusters"] = [
        cluster
        for cluster in command["clusters"]
        if cluster["cluster_id"] != omitted["cluster"]
    ]
    command["evidence_references"] = [
        reference
        for reference in command["evidence_references"]
        if reference["application_id"] != omitted["application_id"]
    ]
    for family in command["mandatory_check_families"]:
        if omitted["check_id"] in family["check_ids"]:
            family["check_ids"].remove(omitted["check_id"])
    _root, manifest_id, manifest_digest = _write_label_manifest(
        tmp_path,
        {
            opportunity["opportunity_id"]: "consistent"
            for opportunity in command["opportunities"]
        },
    )
    command["label_manifest"] = {
        "manifest_id": manifest_id,
        "manifest_digest": manifest_digest,
    }

    with pytest.raises(ValueError, match="governed mandatory checks"):
        service.freeze_plan(command)


def test_process_partial_budget_stop_is_insufficient(tmp_path: Path) -> None:
    service, command, _context = _slice1_harness(tmp_path)
    command["stop_rule"] = "budget-or-plan"
    plan = service.freeze_plan(command)
    complete = _runner_output_for_plan(plan)
    completed_ids = complete["stop"]["completed_run_ids"][:1]
    partial = _stopped_runner_result(complete, completed_ids=completed_ids)

    job = service.start_job(plan["plan_id"])
    outcome = service.process_job(job["job_id"], runner_result=partial)
    bundle = service.query_bundle(outcome["bundle_id"])

    assert outcome["status"] == "INSUFFICIENT"
    assert bundle["stop_rule_satisfied"] is False
    assert any("stop" in reason for reason in bundle["status_reasons"])


def test_runner_elapsed_must_obey_exact_frozen_deadline(tmp_path: Path) -> None:
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    over_deadline = _runner_output_for_plan(plan)
    over_deadline["stop"]["stop_reason"] = "budget-or-plan"
    over_deadline["stop"]["elapsed_ms"] = (
        plan["budget"]["max_runtime_ms"] + 1
    )

    _invalidate_and_run(
        service,
        plan,
        _recompute_digest(over_deadline),
        "RUNNER_STOP_OBSERVATION_INVALID",
    )


def test_result_digest_covers_complete_verified_runner_observation(
    tmp_path: Path,
) -> None:
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    first_result = _runner_output_for_plan(plan)
    first_job = service.start_job(plan["plan_id"])
    first_outcome = service.process_job(
        first_job["job_id"], runner_result=first_result
    )
    first_digest = service.query_bundle(first_outcome["bundle_id"])[
        "result_digest"
    ]

    changed_observation = copy.deepcopy(first_result)
    changed_observation["stop"]["elapsed_ms"] += 1
    _recompute_digest(changed_observation)
    second_job = service.start_job(plan["plan_id"])
    second_outcome = service.process_job(
        second_job["job_id"], runner_result=changed_observation
    )
    second_bundle = service.query_bundle(second_outcome["bundle_id"])

    assert second_bundle["runner_result_digest"] == changed_observation["digest"]
    assert second_bundle["result_digest"] != first_digest


def test_runner_observation_requires_closed_check_schema(tmp_path: Path) -> None:
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    baseline = _runner_output_for_plan(plan)

    malformed_checks: list[dict[str, Any]] = []
    missing_severity = copy.deepcopy(baseline)
    del missing_severity["applications"][0]["checks"][0]["severity"]
    malformed_checks.append(missing_severity)
    wrong_severity = copy.deepcopy(baseline)
    wrong_severity["applications"][0]["checks"][0]["severity"] = {}
    malformed_checks.append(wrong_severity)
    wrong_reasons = copy.deepcopy(baseline)
    wrong_reasons["applications"][0]["checks"][0]["reason_codes"] = "bad"
    malformed_checks.append(wrong_reasons)
    extra_check_field = copy.deepcopy(baseline)
    extra_check_field["applications"][0]["checks"][0]["invented"] = True
    malformed_checks.append(extra_check_field)

    for malformed in malformed_checks:
        _invalidate_and_run(
            service,
            plan,
            _recompute_digest(malformed),
            "RUNNER_CHECK_INVALID",
        )

    mixed_outcome = copy.deepcopy(baseline)
    mixed_outcome["applications"][0]["error"] = "CHECKER_EXECUTION_FAILED"
    _invalidate_and_run(
        service,
        plan,
        _recompute_digest(mixed_outcome),
        "RUNNER_OUTPUT_MALFORMED",
    )

    extra_application_field = copy.deepcopy(baseline)
    extra_application_field["applications"][0]["invented"] = True
    _invalidate_and_run(
        service,
        plan,
        _recompute_digest(extra_application_field),
        "RUNNER_OUTPUT_MALFORMED",
    )


def _insert_readdressed_bundle(
    state_path: Path, bundle: dict[str, Any]
) -> str:
    import sqlite3 as _sqlite3

    from task4_consistency.controlled.s12 import _integrity_digest

    replay = bundle["replay_package"]
    plan = replay["plan"]
    plan["plan_digest"] = content_digest(
        {key: value for key, value in plan.items() if key != "plan_digest"}
    )
    replay["plan"] = plan
    bundle["plan_digest"] = plan["plan_digest"]
    bundle["replay_package_digest"] = content_digest(replay)
    bundle_id = "s12_bundle_sha256_" + content_digest(
        {key: value for key, value in bundle.items() if key != "bundle_id"}
    )
    bundle["bundle_id"] = bundle_id
    payload = json.dumps(
        bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    connection = _sqlite3.connect(state_path)
    try:
        connection.execute(
            "INSERT INTO s12_bundles(item_id, payload, integrity_sha256) "
            "VALUES (?, ?, ?)",
            (
                bundle_id,
                payload,
                _integrity_digest("s12_bundles", bundle_id, payload),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return bundle_id


def test_query_independently_verifies_every_nested_replay_address(
    tmp_path: Path,
) -> None:
    from task4_consistency.controlled.s12 import S12IntegrityError

    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"])
    outcome = service.process_job(job["job_id"])
    original = service.query_bundle(outcome["bundle_id"])
    state_path = tmp_path / "evaluation.sqlite3"

    def mutate_label(bundle: dict[str, Any]) -> None:
        bundle["replay_package"]["plan"]["label_manifest"]["labels"][
            "opp-0"
        ] = "inconsistent"

    def mutate_evidence(bundle: dict[str, Any]) -> None:
        run_spec = next(
            iter(bundle["replay_package"]["plan"]["run_specs"].values())
        )
        run_spec["evidence_snapshot"]["forged"] = True

    def mutate_run_spec(bundle: dict[str, Any]) -> None:
        run_spec = next(
            iter(bundle["replay_package"]["plan"]["run_specs"].values())
        )
        run_spec["application_id"] = "forged-application"

    def mutate_checker(bundle: dict[str, Any]) -> None:
        bundle["replay_package"]["plan"]["checker_artifact"][
            "checker_build"
        ] = "forged-checker"

    def mutate_result(bundle: dict[str, Any]) -> None:
        bundle["replay_package"]["result_material"]["status"] = "PASS(scope=C)"

    def mutate_derived_statistics(bundle: dict[str, Any]) -> None:
        replay = bundle["replay_package"]
        for statistics in (
            bundle["tracks"]["C"],
            replay["tracks_statistics"]["C"],
            replay["result_material"]["tracks_statistics"]["C"],
        ):
            statistics["point"]["coverage"] = 0.123456
        bundle["result_digest"] = content_digest(replay["result_material"])

    for mutator in (
        mutate_label,
        mutate_evidence,
        mutate_run_spec,
        mutate_checker,
        mutate_result,
        mutate_derived_statistics,
    ):
        tampered = copy.deepcopy(original)
        mutator(tampered)
        tampered_id = _insert_readdressed_bundle(state_path, tampered)
        with pytest.raises(S12IntegrityError):
            service.query_bundle(tampered_id)


def test_replay_verifies_governed_release_and_business_vector_digests(
    tmp_path: Path,
) -> None:
    from task4_consistency.controlled.s12 import S12IntegrityError

    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"])
    outcome = service.process_job(job["job_id"])
    original = service.query_bundle(outcome["bundle_id"])
    state_path = tmp_path / "evaluation.sqlite3"

    release_tamper = copy.deepcopy(original)
    release_replay = release_tamper["replay_package"]
    for release in (
        release_replay["plan"]["release"],
        release_replay["result_material"]["release"],
        release_tamper["release"],
    ):
        release["manifest_id"] = "manifest_sha256_" + "a" * 64
        release["manifest_digest"] = "a" * 64
        release["protected_baseline_digest"] = "a" * 64
    release_tamper["result_digest"] = content_digest(
        release_replay["result_material"]
    )
    release_tamper_id = _insert_readdressed_bundle(
        state_path, release_tamper
    )

    vector_tamper = copy.deepcopy(original)
    vector_replay = vector_tamper["replay_package"]
    measurements = (
        vector_replay["plan"]["business_before"],
        vector_replay["business_before"],
        vector_replay["business_after"],
        vector_replay["result_material"]["business_before"],
        vector_replay["result_material"]["business_after"],
        vector_tamper["business_before"],
        vector_tamper["business_after"],
    )
    for measurement in measurements:
        measurement["applications_vector"][0][
            "application_id"
        ] = "self-readdressed"
    vector_tamper["result_digest"] = content_digest(
        vector_replay["result_material"]
    )
    vector_tamper_id = _insert_readdressed_bundle(state_path, vector_tamper)

    with pytest.raises(S12IntegrityError):
        service.query_bundle(release_tamper_id)
    with pytest.raises(S12IntegrityError):
        service.query_bundle(vector_tamper_id)


def test_replay_top_level_material_must_match(tmp_path: Path) -> None:
    from task4_consistency.controlled.s12 import S12IntegrityError

    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"])
    outcome = service.process_job(job["job_id"])
    tampered = copy.deepcopy(service.query_bundle(outcome["bundle_id"]))
    tampered["replay_package"]["predictions"]["opp-0"] = "uncertain"
    tampered_id = _insert_readdressed_bundle(
        tmp_path / "evaluation.sqlite3", tampered
    )

    with pytest.raises(S12IntegrityError):
        service.query_bundle(tampered_id)


def test_business_vector_keyset_must_match_exactly(tmp_path: Path) -> None:
    service, command, _context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    service._business_state_provider = lambda: {}
    job = service.start_job(plan["plan_id"])

    outcome = service.process_job(job["job_id"])

    assert outcome["status"] == "INVALID"
    assert outcome["bundle_id"] is None
    assert outcome["reason_codes"] == ["BUSINESS_AUTHORITY_UNAVAILABLE"]


def test_business_measurement_requires_complete_authority_schema(
    tmp_path: Path,
) -> None:
    service, command, _context = _slice1_harness(tmp_path)
    service._business_state_provider = lambda: {}

    with pytest.raises(S12Unavailable, match="unavailable or corrupt"):
        service.freeze_plan(command)


def test_publication_rechecks_business_vector_inside_fenced_decision(
    tmp_path: Path,
) -> None:
    service, command, context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"])
    publication_guard = service._business_publication_guard

    @contextmanager
    def mutate_then_guard(revisions: dict[str, int]):
        store = SQLiteTargetStore(context["business_path"])
        application = next(iter(store.applications.values()))
        application["route"] = "late-authority-change"
        store.persist()
        with publication_guard(revisions):
            yield

    service._business_publication_guard = mutate_then_guard
    outcome = service.process_job(job["job_id"])

    assert outcome["status"] == "INVALID"
    assert outcome["bundle_id"] is None
    assert outcome["reason_codes"] == [
        "BUSINESS_AUTHORITY_CHANGED:authority_revision"
    ]


def test_publication_holds_authority_revision_fence_through_commit(
    tmp_path: Path,
) -> None:
    service, command, context = _slice1_harness(tmp_path)
    plan = service.freeze_plan(command)
    job = service.start_job(plan["plan_id"])
    original_write = service._store._write_row
    writer_started = threading.Event()
    writer_finished = threading.Event()
    writer_errors: list[BaseException] = []

    def mutate_business_authority() -> None:
        try:
            writer_started.set()
            store = SQLiteTargetStore(context["business_path"])
            application = next(iter(store.applications.values()))
            application["route"] = "late-authority-write"
            store.persist()
        except BaseException as error:  # surfaced on the test thread below
            writer_errors.append(error)
        finally:
            writer_finished.set()

    writer = threading.Thread(target=mutate_business_authority)

    def observe_bundle_write(
        connection: sqlite3.Connection,
        table: str,
        item_id: str,
        value: dict[str, Any],
    ) -> None:
        if table == "s12_bundles":
            writer.start()
            assert writer_started.wait(timeout=5)
            assert not writer_finished.wait(timeout=0.2)
        original_write(connection, table, item_id, value)

    service._store._write_row = observe_bundle_write
    try:
        outcome = service.process_job(job["job_id"])
    finally:
        writer.join(timeout=10)

    assert not writer.is_alive()
    assert writer_errors == []
    assert outcome["bundle_id"] is not None
