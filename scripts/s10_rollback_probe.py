#!/usr/bin/env python3
"""S10 rollback / stop probe (R evidence).

After a committed page-membership successor:
- The old run is never current again and no command can remove the successor or
  re-issue the old route (append-only Evidence, Lifecycle currentness guard).
- The stop drill disables new membership work and new admissions while the
  accepted decisions, candidate claims, current route, audit, snapshots and
  history remain readable.

Run against the candidate commit:
    .venv/bin/python scripts/s10_rollback_probe.py
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path

from task4_consistency.controlled.s01 import (
    AdmissionDisposition,
    ControlledScenarioService,
    S01CommandPrincipal,
)
from task4_consistency.controlled.s01_store import (
    SQLiteTargetStore,
    _encode,
    _integrity_digest,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = "app_s10_ambiguous_membership.json"
ROLLBACK_REVISION = "dbfec1f0a1369d4c0956ba8a95a7cfd10dccf635"
ROLLBACK_SCENARIO = "app_r53_bad_engine.json"


def _membership_command(
    finding: dict[str, object],
    *,
    decision: str,
    reason_code: str,
) -> dict[str, object]:
    membership = finding["membership"]
    assert isinstance(membership, dict)
    candidates = membership["candidates"]
    assert isinstance(candidates, list) and candidates
    candidate = candidates[0]
    assert isinstance(candidate, dict)
    command: dict[str, object] = {
        "schema_version": "page-membership-correction/2",
        "finding_id": finding["finding_id"],
        "candidate_claim_id": candidate["claim_id"],
        "attachment_id": membership["attachment_id"],
        "page_source_sha256": membership["page_source_sha256"],
        "page_ordinal": membership["page_ordinal"],
        "source_evidence": membership["source_evidence"],
        "expected_active_decision_ids": membership["active_decision_ids"],
        "decision": decision,
        "reason_code": reason_code,
    }
    if decision == "accept":
        command.update(
            document_instance_id=candidate["document_instance_id"],
            document_role=candidate["document_role"],
        )
    return command


def _preserved_facts(
    service: ControlledScenarioService,
    reviewer: S01CommandPrincipal,
    application_id: str,
) -> dict[str, object]:
    history = service.application_history_view(
        principal=reviewer, application_id=application_id
    )
    return {
        "route": service.current_route_view(
            principal=reviewer, application_id=application_id
        ),
        "runs": [
            {
                key: run.get(key)
                for key in (
                    "run_id",
                    "current",
                    "evidence_revision",
                    "evidence_snapshot_id",
                    "evidence_snapshot_digest",
                )
            }
            for run in history["runs"]
        ],
        "memberships": [
            {
                key: record.get(key)
                for key in (
                    "record_kind",
                    "claim_id",
                    "decision_id",
                    "candidate_document",
                    "document_instance_id",
                    "document_role",
                    "page",
                    "source_evidence",
                    "status",
                    "supersedes",
                )
            }
            for record in history["memberships"]
        ],
        "membership_history": [
            {
                key: correction.get(key)
                for key in (
                    "correction_id",
                    "decision_id",
                    "decision",
                    "page_ordinal",
                    "evidence_revision",
                    "supersedes",
                )
            }
            for correction in history["membership_history"]
        ],
    }


def _replace_rollback_evidence_event(
    state_path: Path, event: dict[str, object]
) -> None:
    event_id = str(event["event_id"])
    payload = _encode(event)
    digest = _integrity_digest("evidence_events", event_id, payload)
    with sqlite3.connect(state_path) as connection:
        event_update = connection.execute(
            "UPDATE evidence_events SET payload = ?, integrity_sha256 = ? "
            "WHERE item_id = ?",
            (payload, digest, event_id),
        )
        catalog_update = connection.execute(
            "UPDATE s01_immutable_catalog SET integrity_sha256 = ? "
            "WHERE table_name = 'evidence_events' AND item_id = ?",
            (digest, event_id),
        )
        if event_update.rowcount != 1 or catalog_update.rowcount != 1:
            raise RuntimeError("rollback compatibility event is unavailable")


def _rollback_executable_facts(
    *,
    state_path: Path,
    prior_root: Path,
    rollback_state: Path,
    reviewer: S01CommandPrincipal,
    application_id: str,
) -> dict[str, object]:
    archive = subprocess.run(
        [
            "git",
            "archive",
            ROLLBACK_REVISION,
            "task4_consistency",
            "configs",
            "fixtures",
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
        bundle.extractall(prior_root)
    with sqlite3.connect(state_path) as source, sqlite3.connect(rollback_state) as target:
        source.backup(target)
    rollback_store = SQLiteTargetStore(rollback_state)
    membership_events = [
        event
        for event in rollback_store.evidence_events
        if event.get("application_id") == application_id
        and event.get("kind") == "membership_correction"
    ]
    if len(membership_events) != 1:
        raise RuntimeError("rollback copy has no unique membership successor")
    membership_event = membership_events[0]
    original_events = rollback_store.evidence_events
    compatibility_payload = {
        **membership_event["payload"],
        "schema_version": "s06-supplement-evidence/1",
    }
    compatibility_bytes = json.dumps(
        compatibility_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    compatibility_event = {
        **membership_event,
        "kind": "supplement_attachment_version",
        "content_sha256": hashlib.sha256(compatibility_bytes).hexdigest(),
        "content_bytes": len(compatibility_bytes),
        "payload": compatibility_payload,
    }
    _replace_rollback_evidence_event(rollback_state, compatibility_event)
    probe = r'''
import json
import sys
from pathlib import Path
from task4_consistency.controlled.s01 import ControlledScenarioService, S01CommandPrincipal

root = Path(sys.argv[1])
service = ControlledScenarioService(
    fixture_root=root / "fixtures" / "applications",
    rules_path=root / "configs" / "rules_auto_lease.yaml",
    state_path=Path(sys.argv[2]),
    scenario_id=sys.argv[3],
    audit_available=False,
)
reviewer = S01CommandPrincipal(
    subject=sys.argv[4], role="reviewer", scope="C-DEMO", source_id="s10-probe-console"
)
application_id = sys.argv[5]
history = service.application_history_view(principal=reviewer, application_id=application_id)
facts = {
    "route": service.current_route_view(principal=reviewer, application_id=application_id),
    "runs": [
        {key: run.get(key) for key in ("run_id", "current", "evidence_revision", "evidence_snapshot_id", "evidence_snapshot_digest")}
        for run in history["runs"]
    ],
}
print(json.dumps(facts, ensure_ascii=False, sort_keys=True))
'''
    environment = os.environ.copy()
    environment.update(PYTHONPATH=str(prior_root), PYTHONDONTWRITEBYTECODE="1")
    try:
        completed = subprocess.run(
            [
                str(ROOT / ".venv" / "bin" / "python"),
                "-c",
                probe,
                str(prior_root),
                str(rollback_state),
                ROLLBACK_SCENARIO,
                reviewer.subject,
                application_id,
            ],
            cwd=prior_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        _replace_rollback_evidence_event(rollback_state, membership_event)
    if completed.returncode != 0:
        raise RuntimeError(
            f"rollback executable {ROLLBACK_REVISION} failed: {completed.stderr.strip()}"
        )
    reloaded = SQLiteTargetStore(rollback_state)
    if reloaded.evidence_events != original_events:
        raise RuntimeError("fixed-base read changed persisted Evidence")
    return json.loads(completed.stdout)


def main() -> int:
    integrator = S01CommandPrincipal(
        subject="s10-probe-reviewer",
        role="integrator",
        scope="C-DEMO",
        source_id="s10-probe-intake",
    )
    reviewer = S01CommandPrincipal(
        subject=integrator.subject,
        role="reviewer",
        scope="C-DEMO",
        source_id="s10-probe-console",
    )
    operator = S01CommandPrincipal(
        subject="s10-probe-operator",
        role="operator",
        scope="C-DEMO",
        source_id="s10-probe-operations",
    )
    with tempfile.TemporaryDirectory() as td:
        service = ControlledScenarioService(
            fixture_root=ROOT / "fixtures" / "applications",
            rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
            state_path=Path(td) / "target.sqlite3",
            scenario_id=SCENARIO,
        )
        admitted = service.submit_demo(
            scenario_id=SCENARIO,
            idempotency_key="s10-probe-intake",
            principal=integrator,
        )
        if admitted.disposition is not AdmissionDisposition.ACCEPTED:
            raise SystemExit(f"admission rejected: {admitted.reason_code}")
        application_id = str(admitted.application_id)
        service.process_next_job()
        service.refresh_projection()
        queue = service.queue_view(
            role="reviewer", scope=reviewer.scope, subject=reviewer.subject
        )
        work_item_id = queue["items"][0]["work_item_id"]
        work_item = service.review_work_item_view(
            principal=reviewer, work_item_id=work_item_id
        )
        old_run_id = work_item["run_authority"]["run_id"]
        claimed = service.claim_review_work_item(
            principal=reviewer,
            work_item_id=work_item_id,
            expected_context=work_item["command_context"],
        )
        workspace = service.workspace_view(
            application_id, role="reviewer", scope=reviewer.scope, subject=reviewer.subject
        )
        ambiguous = next(
            item
            for item in workspace["mandatory_blockers"]
            if item["rule_id"] == "MEMBERSHIP_AMBIGUOUS"
        )
        membership = ambiguous["membership"]

        # Commit the successor: accept one candidate of the ambiguous page.
        accepted = service.correct_page_membership(
            principal=reviewer,
            application_id=application_id,
            work_item_id=work_item_id,
            expected_fence=claimed["claim_fence"],
            expected_context=work_item["command_context"],
            idempotency_key="s10-probe-accept",
            membership=_membership_command(
                ambiguous,
                decision="accept",
                reason_code="MEMBERSHIP_SOURCE_VERIFIED",
            ),
        )
        assert accepted["status"] == "accepted", accepted
        replaced = service.process_next_job()
        assert replaced.status == "complete", replaced.status

        # 1. Rollback probe: the old run cannot be made current again and the
        # successor is not removable; the route changed only via the fresh run.
        app = service._store.applications[application_id]
        current_run = service._current_run_authority(app)
        assert current_run["run_id"] == replaced.run_id, (
            "old run became current after the successor"
        )
        current = service.current_route_view(
            principal=reviewer, application_id=application_id
        )
        assert current["current_run_id"] == replaced.run_id
        assert current["evidence_revision"] == 2
        history = service.application_history_view(
            principal=reviewer, application_id=application_id
        )
        runs = {item["run_id"]: item for item in history["runs"]}
        assert old_run_id in runs and runs[old_run_id]["current"] is False
        assert replaced.run_id in runs and runs[replaced.run_id]["current"] is True
        assert len(history["membership_history"]) == 1
        decisions = [
            record
            for record in history["memberships"]
            if record["record_kind"] in {"accepted", "unassigned"}
        ]
        assert any(record["decision_id"] == accepted["membership_decision_id"]
                   for record in decisions)
        # Append-only Evidence: the successor event exists and the runs froze
        # immutable snapshots; nothing is ever removed.
        membership_events = [
            event
            for event in service._store.evidence_events
            if event["kind"] == "membership_correction"
        ]
        assert len(membership_events) == 1
        assert len(service._store.evidence_events) >= 3
        graph = service._admitted_graph(app)
        ledger = graph["page_memberships"]
        # Exactly one active decision for the corrected page; predecessors
        # stay in the append-only ledger.
        page1_active = [
            record
            for record in ledger
            if record["record_kind"] in {"accepted", "unassigned"}
            and record["status"] == "active"
            and record["page"]["source_sha256"] == membership["page_source_sha256"]
        ]
        assert len(page1_active) == 1
        assert page1_active[0]["decision_id"] == accepted["membership_decision_id"]

        service.refresh_projection()
        successor_queue = service.queue_view(
            role="reviewer", scope=reviewer.scope, subject=reviewer.subject
        )
        successor_item = next(
            item
            for item in successor_queue["items"]
            if item["application_id"] == application_id
            and item["work_item_id"] != work_item_id
        )
        successor_work = service.review_work_item_view(
            principal=reviewer, work_item_id=successor_item["work_item_id"]
        )
        claimed_successor = service.claim_review_work_item(
            principal=reviewer,
            work_item_id=successor_item["work_item_id"],
            expected_context=successor_work["command_context"],
        )
        successor_workspace = service.workspace_view(
            application_id,
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
        )
        unresolved = next(
            item
            for item in successor_workspace["mandatory_blockers"]
            if item["rule_id"] == "MEMBERSHIP_UNRESOLVED"
        )

        # 2. Stop drill: new admissions and new membership edits are disabled;
        # accepted facts / current route / history remain readable.
        stop = service.stop_new_cohort(
            reason_code=service._RUNTIME_STOP_REASON,
            failure_reason_code=service._REVIEW_SOURCE_FAILURE,
            principal=operator,
        )
        assert stop["admission"] == "stopped", stop
        blocked = service.submit_demo(
            scenario_id=SCENARIO,
            idempotency_key="s10-probe-after-stop",
            principal=integrator,
        )
        if not (
            blocked.disposition is not AdmissionDisposition.ACCEPTED
            and blocked.reason_code == service._RUNTIME_STOP_REASON
        ):
            raise SystemExit(f"stop drill did not block new cohort: {blocked.reason_code}")
        before_edit_counts = {
            "evidence": len(service._store.evidence_events),
            "lifecycle": len(service._store.lifecycle_events),
            "audit": len(service._store.audit_events),
            "outbox": len(service._store.outbox),
        }
        stopped_edit = service.correct_page_membership(
            principal=reviewer,
            application_id=application_id,
            work_item_id=successor_item["work_item_id"],
            expected_fence=claimed_successor["claim_fence"],
            expected_context=successor_work["command_context"],
            idempotency_key="s10-probe-stopped-edit",
            membership=_membership_command(
                unresolved,
                decision="unassign",
                reason_code="MEMBERSHIP_PAGE_UNASSIGNED",
            ),
        )
        assert stopped_edit["status"] == "stopped", stopped_edit
        assert stopped_edit["reason_code"] == service._REVIEW_SOURCE_FAILURE
        after_edit_counts = {
            "evidence": len(service._store.evidence_events),
            "lifecycle": len(service._store.lifecycle_events),
            "audit": len(service._store.audit_events),
            "outbox": len(service._store.outbox),
        }
        assert after_edit_counts == before_edit_counts
        still_current = service.current_route_view(
            principal=reviewer, application_id=application_id
        )
        assert still_current == current, "stop drill changed the current route"
        history_after_stop = service.application_history_view(
            principal=reviewer, application_id=application_id
        )
        assert history_after_stop["membership_history"] == history["membership_history"]

        # A prior executable reads a SQLite copy without mutating the live
        # authority.  Route, snapshots, claims and decisions must survive.
        preserved = _preserved_facts(service, reviewer, application_id)
        rollback_state = Path(td) / "rollback.sqlite3"
        with tempfile.TemporaryDirectory() as rollback_dir:
            rollback_facts = _rollback_executable_facts(
                state_path=Path(td) / "target.sqlite3",
                prior_root=Path(rollback_dir),
                rollback_state=rollback_state,
                reviewer=reviewer,
                application_id=application_id,
            )
        expected_rollback = {
            "route": preserved["route"],
            "runs": preserved["runs"],
        }
        assert rollback_facts == expected_rollback, json.dumps(
            {"fixed": expected_rollback, "rollback": rollback_facts},
            ensure_ascii=False,
            sort_keys=True,
        )
        rollback_service = ControlledScenarioService(
            fixture_root=ROOT / "fixtures" / "applications",
            rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
            state_path=rollback_state,
            scenario_id=SCENARIO,
        )
        restored = _preserved_facts(rollback_service, reviewer, application_id)
        assert restored == preserved, json.dumps(
            {"fixed": preserved, "restored": restored},
            ensure_ascii=False,
            sort_keys=True,
        )

        # Restore the fixed executable, verify the repair, and append a forward
        # successor from the still-claimed page 2 work item.
        recovered = rollback_service.recover_runtime(
            expected_failure_reason_code=service._REVIEW_SOURCE_FAILURE,
            principal=operator,
        )
        assert recovered["recovery"] == "scheduled", recovered
        forwarded = rollback_service.correct_page_membership(
            principal=reviewer,
            application_id=application_id,
            work_item_id=successor_item["work_item_id"],
            expected_fence=claimed_successor["claim_fence"],
            expected_context=successor_work["command_context"],
            idempotency_key="s10-probe-forward-unassign",
            membership=_membership_command(
                unresolved,
                decision="unassign",
                reason_code="MEMBERSHIP_PAGE_UNASSIGNED",
            ),
        )
        assert forwarded["status"] == "accepted", forwarded
        forward_run = rollback_service.process_next_job()
        assert forward_run.status == "complete", forward_run.status
        final_history = rollback_service.application_history_view(
            principal=reviewer, application_id=application_id
        )
        assert len(final_history["membership_history"]) == 2
        final_decisions = [
            record
            for record in final_history["memberships"]
            if record["record_kind"] in {"accepted", "unassigned"}
        ]
        assert len(final_decisions) == 2
        assert {
            record["decision_id"] for record in final_decisions
        } == {
            accepted["membership_decision_id"],
            forwarded["membership_decision_id"],
        }
        print("S10 rollback/stop probe: PASS")
        print("  old run immutable and non-current; successor append-only")
        print("  stop drill rejected an existing edit with zero writes")
        print("  dbfec1f0 executable read persisted route and snapshot history")
        print("  rollback copy retained the original membership successor")
        print("  fixed executable resumed and appended a forward successor")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
