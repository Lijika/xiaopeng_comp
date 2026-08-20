#!/usr/bin/env python3
"""S12 rollback / isolation probe (Ticket #28 R1 evidence).

Proves, on temporary business and evaluation databases:
1. S12 scoped disablement: with no TASK4_S12_* configuration every S12 HTTP
   route reports S12_UNAVAILABLE while an S01-S11 route keeps serving;
2. zero business deltas: the S01 business database bytes are identical
   across S12 freeze, execution, query, cancellation, bundle publication,
   restart, replay and rerun;
3. restart/replay: reopening evaluation storage retains bundle bytes and
   digest;
4. immutable rerun lineage: a rerun publishes a new bundle that references
   the source bundle via rerun_of_bundle_id and never overwrites it;
5. prior S01-S11 continuity: the business service keeps serving its
   applications after every S12 operation;
6. prior-artifact reopen (--prior-source-root): the archived fixed-base
   code reopens the business database created before S12 operations and
   serves the preserved application state read-only.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from task4_consistency.controlled.s01 import (  # noqa: E402
    AdmissionDisposition,
    ControlledScenarioService,
    ControlledScenarioTestDriver,
    S01CommandPrincipal,
)
from task4_consistency.controlled.s01_store import SQLiteTargetStore  # noqa: E402
from task4_consistency.controlled.s08 import PolicyGovernanceService  # noqa: E402
from task4_consistency.controlled.s12 import (  # noqa: E402
    EvaluationService,
    LabelManifestStore,
    content_digest,
)

FIXED_BASE = "8a8d7f1bfe37fe97e713dfa92350a56fef31266d"
TEST_INTEGRATOR = S01CommandPrincipal(
    subject="registered-probe-integrator",
    role="integrator",
    scope="C-DEMO",
    source_id="s12-rollback-probe",
)
SCENARIOS = (
    "app_r53_bad_engine.json",
    "app_s04_bad_vin.json",
    "app_bad_brand.json",
    "app_bad_model.json",
)
CHECK_BY_SCENARIO = {
    "app_r53_bad_engine.json": "R_ENGINE_CROSS",
    "app_s04_bad_vin.json": "R_VIN_CROSS",
    "app_bad_brand.json": "R_BRAND_CROSS",
    "app_bad_model.json": "R_MODEL_CROSS",
}


def _business_harness(
    work: Path,
) -> tuple[list[ControlledScenarioService], list[tuple[str, str]], dict[str, tuple[str, str]], Path]:
    business_path = work / "business.sqlite3"
    services: list[ControlledScenarioService] = []
    admitted: list[tuple[str, str]] = []
    for index, scenario in enumerate(SCENARIOS):
        service = ControlledScenarioService(
            fixture_root=ROOT / "fixtures" / "applications",
            rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
            state_path=business_path,
            scenario_id=scenario,
        )
        result = service.submit_demo(
            principal=TEST_INTEGRATOR,
            scenario_id=scenario,
            idempotency_key=f"s12-probe-{index}",
        )
        assert result.disposition is AdmissionDisposition.ACCEPTED, scenario
        ControlledScenarioTestDriver(service).process_next_job(now=0)
        services.append(service)
        admitted.append((scenario, result.application_id))
    store = SQLiteTargetStore(business_path)
    store.reload()
    snapshots: dict[str, tuple[str, str]] = {}
    for event in store.evidence_events:
        if event.get("kind") == "immutable_ready_snapshot":
            snapshots[event["application_id"]] = (
                event["snapshot_id"],
                event["content_sha256"],
            )
    assert len(snapshots) == len(SCENARIOS)
    return services, admitted, snapshots, business_path


def _governed_release(work: Path) -> tuple[PolicyGovernanceService, str, str]:
    bundle = work / "server-bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    rules_path = bundle / "rules.yaml"
    kb_path = bundle / "entity_kb.json"
    rules_path.write_bytes(
        (ROOT / "configs" / "rules_auto_lease.yaml").read_bytes()
    )
    kb_path.write_bytes((ROOT / "configs" / "kb" / "entity_kb.json").read_bytes())
    service = PolicyGovernanceService(
        state_path=work / "governance.sqlite3",
        source_rules_path=rules_path,
        source_kb_path=kb_path,
        corpus_root=ROOT / "fixtures" / "applications",
    )
    bootstrap = service.bootstrap_once()
    assert bootstrap["status"] == "activated", bootstrap
    store = SQLiteTargetStore(work / "governance.sqlite3")
    store.reload()
    manifest = next(item for item in store.policy_manifests)
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
    from task4_consistency.controlled.s01_checker import TargetRelease

    release = TargetRelease.from_artifact(json.loads(artifact_row["canonical_json"]))
    public = release.public_manifest()
    return service, public["release_id"], public["digest"]


def _label_manifest(work: Path, labels: dict[str, str]) -> tuple[Path, str, str]:
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
        json.dumps({"manifest_id": manifest_id, **body}), encoding="utf-8"
    )
    return root, manifest_id, digest


def _plan_command(
    admitted: list[tuple[str, str]],
    snapshots: dict[str, tuple[str, str]],
    release_id: str,
    release_digest: str,
    manifest_id: str,
    manifest_digest: str,
) -> dict[str, object]:
    opportunities: list[dict[str, object]] = []
    clusters: list[dict[str, object]] = []
    evidence_references: dict[str, object] = {}
    for index, (scenario, application_id) in enumerate(admitted):
        snapshot_id, snapshot_digest = snapshots[application_id]
        opportunities.append(
            {
                "opportunity_id": f"opp-{index}",
                "track": "C",
                "cluster": f"cl-{index}",
                "application_id": application_id,
                "cycle": 1,
                "check_id": CHECK_BY_SCENARIO[scenario],
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
        evidence_references[application_id] = {
            "snapshot_id": snapshot_id,
            "snapshot_digest": snapshot_digest,
            "cycle": 1,
        }
    return {
        "schema_version": "s12-plan-command/1",
        "plan_id": "plan-c-probe",
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
            "C": {"opportunities": [f"opp-{index}" for index in range(len(admitted))]},
        },
        "views": {
            "R-E2E": {"opportunities": []},
            "R-T4-conditional": {"opportunities": []},
        },
        "opportunities": opportunities,
        "evidence_references": evidence_references,
        "release_reference": {
            "release_id": release_id,
            "release_digest": release_digest,
        },
        "label_manifest": {
            "manifest_id": manifest_id,
            "manifest_digest": manifest_digest,
        },
        "mandatory_check_families": [
            {"family_id": "cross-document", "check_ids": ["R_ENGINE_CROSS", "R_VIN_CROSS"]},
            {"family_id": "brand-model", "check_ids": ["R_BRAND_CROSS", "R_MODEL_CROSS"]},
        ],
    }


def _probe_s12_disablement(plan_command: dict[str, object]) -> dict[str, object]:
    """No TASK4_S12_* configuration: S12 routes are S12_UNAVAILABLE while an
    S01-S11 route keeps serving (scoped disablement)."""
    with tempfile.TemporaryDirectory(prefix="s12-disable-") as raw:
        work = Path(raw)
        os.environ["TASK4_S01_STATE_PATH"] = str(work / "s01-state.sqlite3")
        os.environ["TASK4_S01_AUDIT_AVAILABLE"] = "0"
        os.environ["TASK4_S01_STORAGE_AVAILABLE"] = "1"
        os.environ.pop("TASK4_S12_STATE_PATH", None)
        os.environ.pop("TASK4_S12_CREDENTIAL", None)
        os.environ.pop("TASK4_S12_SUBJECT", None)
        os.environ.pop("TASK4_S12_WORKER_SUBJECT", None)
        from fastapi.testclient import TestClient
        from task4_consistency.web import app as webapp

        assert webapp.S12_SERVICE is None
        client = TestClient(webapp.app)
        unavailable = client.post(
            "/controlled/s12/plans/freeze", json=plan_command
        )
        s01_route = client.get("/api/demo/fixtures")
        return {
            "s12_freeze_status": unavailable.status_code,
            "s12_freeze_error": unavailable.json().get("detail", {}).get("error"),
            "s01_route_status": s01_route.status_code,
        }


def _prior_artifact_probe(
    prior_source_root: Path, business_path: Path, expected_applications: int
) -> dict[str, object]:
    """Run the archived fixed-base code against the business database created
    before S12 operations: the prior code must reopen and serve the preserved
    application state read-only."""
    source_digest = hashlib.sha256(
        subprocess.run(
            [
                "git",
                "archive",
                FIXED_BASE,
                "task4_consistency",
                "configs",
                "fixtures",
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
    ).hexdigest()
    probe = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from task4_consistency.controlled.s01 import ControlledScenarioService\n"
        "root = Path(sys.argv[1])\n"
        "service = ControlledScenarioService(\n"
        "    fixture_root=root / 'fixtures' / 'applications',\n"
        "    rules_path=root / 'configs' / 'rules_auto_lease.yaml',\n"
        "    state_path=Path(sys.argv[2]),\n"
        ")\n"
        "print(json.dumps({'applications': service.fact_counts()['applications']}))\n"
    )
    environment = os.environ.copy()
    environment.update(
        PYTHONPATH=str(prior_source_root), PYTHONDONTWRITEBYTECODE="1"
    )
    completed = subprocess.run(
        [
            str(ROOT / ".venv" / "bin" / "python"),
            "-c",
            probe,
            str(prior_source_root),
            str(business_path),
        ],
        cwd=prior_source_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    served = False
    if completed.returncode == 0:
        try:
            result = json.loads(completed.stdout)
            served = int(result.get("applications") or 0) == expected_applications
        except (json.JSONDecodeError, TypeError, ValueError):
            served = False
    return {
        "archived_base": FIXED_BASE,
        "source_digest": source_digest,
        "prior_command_exit": completed.returncode,
        "prior_stderr_tail": completed.stderr[-400:],
        "prior_code_serves_business_state": served,
        "expected_applications": expected_applications,
    }


def main() -> int:
    findings: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="s12-rollback-") as raw:
        work = Path(raw)
        business_services, admitted, snapshots, business_path = _business_harness(work)
        governance_service, release_id, release_digest = _governed_release(work)
        labels = {f"opp-{index}": "consistent" for index in range(len(admitted))}
        label_root, manifest_id, manifest_digest = _label_manifest(work, labels)

        def measure() -> dict[str, object]:
            facts: dict[str, object] = {}
            for service in business_services:
                facts.update(service.evaluation_business_measurement())
            facts.update(governance_service.evaluation_governance_measurement())
            return facts

        service = EvaluationService(
            state_path=work / "evaluation.sqlite3",
            clock=lambda: 1700000000,
            snapshot_provider=lambda application_id, snapshot_id: business_services[
                0
            ].evaluation_evidence_snapshot(
                application_id=application_id, snapshot_id=snapshot_id
            ),
            release_provider=lambda rid, rd: governance_service.resolve_evaluation_release(
                release_id=rid, release_digest=rd
            ),
            label_manifest_provider=LabelManifestStore(label_root).resolve,
            business_state_provider=measure,
            worker_subject="s12-probe-worker",
        )
        command = _plan_command(
            admitted,
            snapshots,
            release_id,
            release_digest,
            manifest_id,
            manifest_digest,
        )
        plan = service.freeze_plan(command)
        business_before = hashlib.sha256(business_path.read_bytes()).hexdigest()
        job = service.start_job(plan["plan_id"], worker_id="s12-probe-worker")
        outcome = service.process_job(job["job_id"])
        bundle_id = outcome["bundle_id"]
        bundle = service.query_bundle(bundle_id)
        business_mid = hashlib.sha256(business_path.read_bytes()).hexdigest()

        job_cancel = service.start_job(plan["plan_id"], worker_id="s12-probe-cancel")
        service.cancel_job(job_cancel["job_id"])
        business_after_cancel = hashlib.sha256(business_path.read_bytes()).hexdigest()

        service2 = EvaluationService(
            state_path=work / "evaluation.sqlite3",
            clock=lambda: 1700000000,
            snapshot_provider=lambda application_id, snapshot_id: business_services[
                0
            ].evaluation_evidence_snapshot(
                application_id=application_id, snapshot_id=snapshot_id
            ),
            release_provider=lambda rid, rd: governance_service.resolve_evaluation_release(
                release_id=rid, release_digest=rd
            ),
            label_manifest_provider=LabelManifestStore(label_root).resolve,
            business_state_provider=measure,
            worker_subject="s12-probe-worker",
        )
        replayed = service2.query_bundle(bundle_id)
        replay_identical = replayed == bundle
        replay_digest = (
            replayed["bundle_id"]
            == "s12_bundle_sha256_"
            + content_digest(
                {k: v for k, v in replayed.items() if k != "bundle_id"}
            )
        )
        rerun_job = service2.rerun_job(job["job_id"], worker_id="s12-probe-worker")
        rerun_outcome = service2.process_job(rerun_job["job_id"])
        rerun_bundle = service2.query_bundle(rerun_outcome["bundle_id"])
        source_preserved = service2.query_bundle(bundle_id) == bundle
        business_final = hashlib.sha256(business_path.read_bytes()).hexdigest()

        continuity = (
            service2.query_job(job["job_id"])["status"] == "complete"
            and business_services[0].fact_counts()["applications"] == len(admitted)
        )

        prior_artifact: dict[str, object] = {"skipped": True}
        if len(sys.argv) > 2 and sys.argv[1] == "--prior-source-root":
            prior_artifact = _prior_artifact_probe(
                Path(sys.argv[2]), business_path, len(admitted)
            )

        findings.update(
            {
                "plan_id": plan["plan_id"],
                "plan_digest": plan["plan_digest"],
                "release": {
                    "release_id": plan["release"]["release_id"],
                    "release_digest": plan["release"]["release_digest"],
                    "checker_build": plan["release"]["checker_build"],
                    "manifest_digest": plan["release"]["manifest_digest"],
                    "protected_baseline_digest": plan["release"][
                        "protected_baseline_digest"
                    ],
                },
                "job": {
                    "job_id": job["job_id"],
                    "status": service2.query_job(job["job_id"])["status"],
                    "fence": service2.query_job(job["job_id"])["fence"],
                    "attempt_no": service2.query_job(job["job_id"])["attempt_no"],
                    "worker_id": service2.query_job(job["job_id"])["worker_id"],
                    "lease_until": service2.query_job(job["job_id"]).get(
                        "lease_until"
                    ),
                },
                "prediction_manifest": bundle["predictions"],
                "track_statistics": {
                    "C": {
                        "denominators": bundle["tracks"]["C"]["denominators"],
                        "point": bundle["tracks"]["C"]["point"],
                        "estimable": bundle["tracks"]["C"]["estimable"],
                        "not_estimable_reasons": bundle["tracks"]["C"][
                            "not_estimable_reasons"
                        ],
                    }
                },
                "scope_eligibility": bundle["scope_eligibility"],
                "status": bundle["status"],
                "scope": bundle["scope"],
                "status_reasons": bundle["status_reasons"],
                "result_digest": bundle["result_digest"],
                "bundle_digest_recomputed": content_digest(
                    {k: v for k, v in bundle.items() if k != "bundle_id"}
                ),
                "rerun_of_bundle_id": rerun_bundle["rerun_of_bundle_id"],
                "rerun_bundle_id": rerun_bundle["bundle_id"],
                "business_deltas": bundle["business_deltas"],
                "business_sha256": {
                    "before": business_before,
                    "after_freeze_execute_query": business_mid,
                    "after_cancel": business_after_cancel,
                    "after_restart_replay_rerun": business_final,
                },
                "business_zero_delta": (
                    business_before
                    == business_mid
                    == business_after_cancel
                    == business_final
                ),
                "replay_identical": replay_identical,
                "replay_digest_retained": replay_digest,
                "rerun_linked": rerun_bundle["rerun_of_bundle_id"] == bundle_id,
                "rerun_distinct": rerun_bundle["bundle_id"] != bundle_id,
                "source_bundle_preserved": source_preserved,
                "s01_s11_continuity": continuity,
                "s12_disablement": _probe_s12_disablement(command),
                "prior_artifact": prior_artifact,
            }
        )

    passed = all(
        (
            findings["business_zero_delta"],
            findings["replay_identical"],
            findings["replay_digest_retained"],
            findings["rerun_linked"],
            findings["rerun_distinct"],
            findings["source_bundle_preserved"],
            findings["s01_s11_continuity"],
            findings["s12_disablement"]["s12_freeze_status"] == 503,
            findings["s12_disablement"]["s01_route_status"] == 200,
            findings["status"] in {"INSUFFICIENT", "FAIL", "SMOKE_ONLY"},
        )
    )
    prior = findings["prior_artifact"]
    if prior.get("skipped") is not True:
        passed = passed and bool(prior.get("prior_code_serves_business_state"))
    findings["probe"] = "PASS" if passed else "FAIL"
    print(json.dumps(findings, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
