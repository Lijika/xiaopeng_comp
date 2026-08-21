"""T14 S12 React shell app factory (Ticket #48).

Builds the real FastAPI application wired to a deterministic S12 authority:
real S01 business services over the fixed competition fixtures, a real
bootstrapped S08 governed release, an independent label manifest, and one
pre-frozen plan.  Launched by ``tests/test_t14_s12_react.spec.js`` through
uvicorn ``--factory`` so the browser drives the released routes end-to-end.
"""

from __future__ import annotations

import json as _json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

S12_CREDENTIAL = "t14-s12-operator-credential"
S12_SUBJECT = "t14-s12-evaluation-operator"
S12_WORKER_SUBJECT = "t14-s12-evaluation-worker"


def _build_fixture_authorities(work_root: Path):
    """One deterministic S12 authority: real business harness, governed
    release, independent labels, one pre-frozen plan."""
    from tests.test_s12_controlled import (
        _business_authority_bindings,
        _make_business_harness,
        _make_governed_release,
        _reference_plan_command,
        _write_label_manifest,
    )
    from task4_consistency.controlled.s12 import (
        EvaluationService,
        LabelManifestStore,
    )

    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    work_root.mkdir(parents=True, exist_ok=True)
    business_services, admitted, snapshots, _business_path = (
        _make_business_harness(work_root / "business", rules_path)
    )
    governance_service, release_id, release_digest, _manifest = (
        _make_governed_release(work_root / "governance")
    )
    labels = {f"opp-{index}": "consistent" for index in range(len(admitted))}
    label_root, manifest_id, manifest_digest = _write_label_manifest(
        work_root, labels
    )
    measure, publication_guard = _business_authority_bindings(
        business_services, governance_service
    )
    service = EvaluationService(
        state_path=work_root / "evaluation.sqlite3",
        clock=lambda: 1700000000,
        snapshot_provider=lambda application_id, snapshot_id: business_services[
            0
        ].evaluation_evidence_snapshot(
            application_id=application_id, snapshot_id=snapshot_id
        ),
        release_provider=lambda release_id_, release_digest_: governance_service.resolve_evaluation_release(
            release_id=release_id_, release_digest=release_digest_
        ),
        label_manifest_provider=LabelManifestStore(label_root).resolve,
        business_state_provider=measure,
        business_publication_guard=publication_guard,
        worker_subject=S12_WORKER_SUBJECT,
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
    (work_root / "fixture.json").write_text(
        _json.dumps(
            {
                "plan_id": plan["plan_id"],
                "plan_digest": plan["plan_digest"],
                "opportunity_count": len(plan["opportunities"]),
                "scope": plan["scope"],
                "frozen_at": plan["frozen_at"],
                "budget": plan["budget"],
                "stop_rule": plan["stop_rule"],
            }
        ),
        encoding="utf-8",
    )
    return service


def create_t14_s12_test_app():
    """The shared FastAPI app with a fully configured S12 authority and no
    S01 background runtime.  Fixture construction runs once per process under
    ``TASK4_T14_FIXTURE_ROOT``; restarts of the same process reuse it."""
    import task4_consistency.web.app as web

    web.S01_BACKGROUND_ENABLED = False
    web.S01_REQUIRE_CONFIGURED_STARTUP = False

    fixture_root = Path(os.environ["TASK4_T14_FIXTURE_ROOT"])
    # Rebuild the providers against the persisted state so a restarted server
    # observes the same frozen rows and bundle bytes.
    service = _build_fixture_authorities(fixture_root)

    web.S12_SERVICE = service
    web.S12_CREDENTIAL = S12_CREDENTIAL
    web.S12_SUBJECT = S12_SUBJECT
    web.S12_WORKER_SUBJECT = S12_WORKER_SUBJECT
    react_dir = os.environ.get("TASK4_T14_REACT_DIR", "").strip()
    web.S01_REACT_INDEX = (
        Path(react_dir).resolve() / "index.html"
        if react_dir
        else web.S01_REACT_STATIC / "index.html"
    )
    return web.app
