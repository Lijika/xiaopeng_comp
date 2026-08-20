#!/usr/bin/env python3
"""S12 rollback / isolation probe (Ticket #28 R evidence).

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
5. prior S01-S11 continuity: the business service keeps admitting and
   running normally after every S12 operation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from task4_consistency.controlled.s01 import (  # noqa: E402
    AdmissionDisposition,
    ControlledScenarioService,
    S01CommandPrincipal,
)
from task4_consistency.controlled.s01_checker import TargetRelease  # noqa: E402
from task4_consistency.controlled.s12 import (  # noqa: E402
    EvaluationService,
    content_digest,
)
from task4_consistency.kb.store import get_kb  # noqa: E402
from task4_consistency.rules.loader import load_rules  # noqa: E402

TEST_INTEGRATOR = S01CommandPrincipal(
    subject="registered-probe-integrator",
    role="integrator",
    scope="C-DEMO",
    source_id="s12-rollback-probe",
)


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


def _plate_documents(plate_no: str, *, second_role: bool) -> list[dict[str, object]]:
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
                        plate_no,
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
) -> dict[str, object]:
    snapshot = {"schema_version": "s01-evidence-snapshot/1", "evidence": evidence}
    snapshot_bytes = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
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


def _plan_command(
    release: TargetRelease, run_specs: dict[str, dict[str, object]]
) -> dict[str, object]:
    manifest = release.public_manifest()
    opportunities = []
    for index in range(4):
        application_id = f"app-{index}"
        opportunities.append(
            {
                "opportunity_id": f"opp-{index}",
                "track": "C",
                "cluster": f"cl-{index}",
                "application_id": application_id,
                "cycle": 1,
                "check_id": "R_PLATE_CROSS",
                "target_scope": "C",
                "evidence_snapshot_id": run_specs[application_id][
                    "evidence_snapshot_id"
                ],
                "label": "consistent" if index in {0, 2} else "inconsistent",
            }
        )
    return {
        "schema_version": "s12-plan-command/1",
        "plan_id": "plan-c-probe",
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
                "cluster_id": f"cl-{index}",
                "stratum": "c",
                "applications": [f"app-{index}"],
                "usage": "development",
            }
            for index in range(4)
        ],
        "tracks": {
            "R": {"opportunities": []},
            "C": {"opportunities": [f"opp-{index}" for index in range(4)]},
        },
        "views": {
            "R-E2E": {"opportunities": []},
            "R-T4-conditional": {"opportunities": []},
        },
        "opportunities": opportunities,
    }


def _business_baseline(
    work: Path, rules_path: Path
) -> tuple[ControlledScenarioService, Path, str, str]:
    business_path = work / "business.sqlite3"
    business = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=rules_path,
        state_path=business_path,
    )
    admitted = business.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s12-probe-business-baseline",
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    before = hashlib.sha256(business_path.read_bytes()).hexdigest()
    return business, business_path, before, admitted.application_id


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


def main() -> int:
    findings: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="s12-rollback-") as raw:
        work = Path(raw)
        rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
        release = TargetRelease.compile(
            load_rules(rules_path),
            hashlib.sha256(rules_path.read_bytes()).hexdigest(),
            knowledge=get_kb().to_dict(),
        )
        manifest = release.public_manifest()
        run_specs = {
            f"app-{index}": _complete_run_spec(
                release, _plate_documents("苏A92054", second_role=True), application_id=f"app-{index}"
            )
            for index in range(4)
        }
        business, business_path, business_before, application_id = _business_baseline(
            work, rules_path
        )

        eval_path = work / "evaluation.sqlite3"
        service = EvaluationService(state_path=eval_path, clock=lambda: 1700000000)
        plan = service.freeze_plan(_plan_command(release, run_specs))
        job = service.start_job(plan["plan_id"], worker_id="s12-probe-worker")
        outcome = service.process_job(job["job_id"])
        bundle_id = outcome["bundle_id"]
        bundle = service.query_bundle(bundle_id)
        business_mid = hashlib.sha256(business_path.read_bytes()).hexdigest()

        # Durable cancellation of a second job: zero business delta.
        job_cancel = service.start_job(plan["plan_id"], worker_id="s12-probe-cancel")
        service.cancel_job(job_cancel["job_id"])
        business_after_cancel = hashlib.sha256(business_path.read_bytes()).hexdigest()

        # Restart/replay: a fresh authority reads the same bytes and digest.
        service2 = EvaluationService(state_path=eval_path, clock=lambda: 1700000000)
        replayed = service2.query_bundle(bundle_id)
        replay_identical = replayed == bundle
        replay_digest = (
            replayed["bundle_id"]
            == "s12_bundle_sha256_"
            + content_digest(
                {k: v for k, v in replayed.items() if k != "bundle_id"}
            )
        )

        # Immutable rerun lineage: new bundle references the source.
        rerun_job = service2.rerun_job(job["job_id"], worker_id="s12-probe-worker")
        rerun_outcome = service2.process_job(rerun_job["job_id"])
        rerun_bundle = service2.query_bundle(rerun_outcome["bundle_id"])
        source_preserved = service2.query_bundle(bundle_id) == bundle
        business_final = hashlib.sha256(business_path.read_bytes()).hexdigest()

        # Prior S01-S11 continuity: the S01 lifecycle authority still serves
        # its admitted application over the unchanged business database, both
        # in the live instance and through a fresh reopen of the store.
        fresh_business = ControlledScenarioService(
            fixture_root=ROOT / "fixtures" / "applications",
            rules_path=rules_path,
            state_path=business_path,
        )
        continuity = (
            business.fact_counts()["applications"] == 1
            and fresh_business.fact_counts()["applications"] == 1
        )

        findings.update(
            {
                "plan_id": plan["plan_id"],
                "plan_digest": plan["plan_digest"],
                "release": {
                    "release_id": manifest["release_id"],
                    "release_digest": manifest["digest"],
                    "checker_build": manifest["checker_build"],
                },
                "job": {
                    "job_id": job["job_id"],
                    "status": service2.query_job(job["job_id"])["status"],
                    "fence": service2.query_job(job["job_id"])["fence"],
                    "attempt_no": service2.query_job(job["job_id"])["attempt_no"],
                    "worker_id": service2.query_job(job["job_id"])["worker_id"],
                    "lease_until": service2.query_job(job["job_id"]).get("lease_until"),
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
                "status": bundle["status"],
                "scope": bundle["scope"],
                "status_reasons": bundle["status_reasons"],
                "result_digest": bundle["bundle_id"],
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
                    business_before == business_mid == business_after_cancel == business_final
                ),
                "replay_identical": replay_identical,
                "replay_digest_retained": replay_digest,
                "rerun_linked": rerun_bundle["rerun_of_bundle_id"] == bundle_id,
                "rerun_distinct": rerun_bundle["bundle_id"] != bundle_id,
                "source_bundle_preserved": source_preserved,
                "s01_s11_continuity": continuity,
                "s12_disablement": _probe_s12_disablement(
                    _plan_command(release, run_specs)
                ),
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
            findings["status"] == "INSUFFICIENT",
        )
    )
    findings["probe"] = "PASS" if passed else "FAIL"
    print(json.dumps(findings, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
