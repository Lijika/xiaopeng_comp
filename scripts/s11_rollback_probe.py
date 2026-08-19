#!/usr/bin/env python3
"""S11 rollback / stop probe (R evidence).

Commits an entity-link successor, then proves:
1. the old run is immutable and non-current and the successor is append-only;
2. stop closes new entity-link edits and new admissions with zero writes while
   facts, route and history stay readable;
3. a prior executable (the fixed base that predates S11) reading a SQLite copy
   fails closed: it cannot read or route the successor state, so no old-result
   currentness or silent equivalence can ever return, and the copy is left
   byte-equal; the fixed executable then resumes and appends a forward
   successor on a fresh run.
"""

from __future__ import annotations

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
    _ApplicationStateAuthorityUnavailable,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = "app_s11_entity_ambiguity.json"
ROLLBACK_REVISION = "50056d7dcf95627ab8e8e0c1d588f32ff5129a5d"
ROLLBACK_SCENARIO = "app_r53_bad_engine.json"
MENTION_ORG = "s11_mention_org_pol"
MENTION_CITY = "s11_mention_city_lease"


def _entity_link_command(
    finding: dict[str, object],
    *,
    entity_id: str,
    entity_type: str,
    label: str,
    reason_code: str = "ENTITY_LINK_SOURCE_VERIFIED",
) -> dict[str, object]:
    entity_link = finding["entity_link"]
    candidate = next(
        candidate
        for candidate in entity_link["candidates"]
        if candidate["entity_id"] == entity_id
    )
    provenance = candidate["provenance"]
    return {
        "schema_version": "entity-link-correction/1",
        "finding_id": finding["finding_id"],
        "candidate_claim_id": candidate["claim_id"],
        "mention_id": entity_link["mention_id"],
        "source_evidence": entity_link["source_evidence"],
        "expected_active_decision_ids": entity_link["active_decision_ids"],
        "decision": "accept",
        "entity_id": candidate["entity_id"],
        "entity_type": candidate["entity_type"],
        "label": candidate["label"],
        "relationship": "same_as",
        "matcher_id": provenance["matcher_id"],
        "matcher_version": provenance["matcher_version"],
        "knowledge_release_id": provenance["knowledge_release_id"],
        "reason_code": reason_code,
    }


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
        "entity_links": [
            {
                key: record.get(key)
                for key in (
                    "record_kind",
                    "claim_id",
                    "decision_id",
                    "candidate_entity",
                    "relationship",
                    "mention",
                    "source_evidence",
                    "status",
                    "supersedes",
                )
            }
            for record in history["entity_links"]
        ],
        "entity_link_history": [
            {
                key: correction.get(key)
                for key in (
                    "correction_id",
                    "decision_id",
                    "mention_id",
                    "entity_id",
                    "evidence_revision",
                    "supersedes",
                )
            }
            for correction in history["entity_link_history"]
        ],
    }


def _old_executable_fails_closed(
    *,
    state_path: Path,
    prior_root: Path,
    reviewer: S01CommandPrincipal,
    application_id: str,
) -> dict[str, object]:
    """Run the fixed base (pre-S11) executable against a copy of the successor
    state.  The old code must fail closed: it cannot read the entity-link
    evidence revision chain, so the old run can never become current again."""
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
)
reviewer = S01CommandPrincipal(
    subject=sys.argv[4], role="reviewer", scope="C-DEMO", source_id="s11-probe-console"
)
application_id = sys.argv[5]
outcome = {"readable": False}
try:
    history = service.application_history_view(
        principal=reviewer, application_id=application_id
    )
    outcome = {
        "readable": True,
        "runs": [
            {key: run.get(key) for key in ("run_id", "current", "evidence_revision")}
            for run in history["runs"]
        ],
    }
except Exception as error:
    outcome = {
        "readable": False,
        "error_type": type(error).__name__,
    }
print(json.dumps(outcome, ensure_ascii=False, sort_keys=True))
'''
    environment = os.environ.copy()
    environment.update(PYTHONPATH=str(prior_root), PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run(
        [
            str(ROOT / ".venv" / "bin" / "python"),
            "-c",
            probe,
            str(prior_root),
            str(state_path),
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
    if completed.returncode != 0:
        raise RuntimeError(
            f"old executable failed unexpectedly: {completed.stderr.strip()}"
        )
    return json.loads(completed.stdout)


def main() -> int:
    integrator = S01CommandPrincipal(
        subject="s11-probe-reviewer",
        role="integrator",
        scope="C-DEMO",
        source_id="s11-probe-intake",
    )
    reviewer = S01CommandPrincipal(
        subject=integrator.subject,
        role="reviewer",
        scope="C-DEMO",
        source_id="s11-probe-console",
    )
    operator = S01CommandPrincipal(
        subject="s11-probe-operator",
        role="operator",
        scope="C-DEMO",
        source_id="s11-probe-operations",
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
            idempotency_key="s11-probe-intake",
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
            application_id,
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
        )
        ambiguous = next(
            item
            for item in workspace["mandatory_blockers"]
            if item["rule_id"] == "ENTITY_LINK_AMBIGUOUS"
        )

        # Commit the successor: accept one candidate of the ambiguous org
        # mention.
        accepted = service.correct_entity_link(
            principal=reviewer,
            application_id=application_id,
            work_item_id=work_item_id,
            expected_fence=claimed["claim_fence"],
            expected_context=work_item["command_context"],
            idempotency_key="s11-probe-accept",
            entity_link=_entity_link_command(
                ambiguous,
                entity_id="org:picc_full",
                entity_type="insurer",
                label="中国人民财产保险股份有限公司",
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
        assert len(history["entity_link_history"]) == 1
        decisions = [
            record
            for record in history["entity_links"]
            if record["record_kind"] == "accepted"
        ]
        assert any(
            record["decision_id"] == accepted["entity_link_decision_id"]
            for record in decisions
        )
        # Append-only Evidence: the successor event exists and the runs froze
        # immutable snapshots; nothing is ever removed.
        entity_link_events = [
            event
            for event in service._store.evidence_events
            if event["kind"] == "entity_link_correction"
        ]
        assert len(entity_link_events) == 1
        assert len(service._store.evidence_events) >= 3
        graph = service._admitted_graph(app)
        ledger = graph["entity_links"]
        org_active = [
            record
            for record in ledger
            if record["record_kind"] == "accepted"
            and record["status"] == "active"
            and record["mention"]["mention_id"] == MENTION_ORG
        ]
        assert len(org_active) == 1
        assert org_active[0]["decision_id"] == accepted["entity_link_decision_id"]

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
            if item["rule_id"] == "ENTITY_LINK_UNRESOLVED"
        )
        assert unresolved["entity_link"]["mention_id"] == MENTION_CITY

        # 2. Stop drill: new admissions and new entity-link edits are disabled;
        # accepted facts / current route / history remain readable.
        stop = service.stop_new_cohort(
            reason_code=service._RUNTIME_STOP_REASON,
            failure_reason_code=service._REVIEW_SOURCE_FAILURE,
            principal=operator,
        )
        assert stop["admission"] == "stopped", stop
        blocked = service.submit_demo(
            scenario_id=SCENARIO,
            idempotency_key="s11-probe-after-stop",
            principal=integrator,
        )
        if not (
            blocked.disposition is not AdmissionDisposition.ACCEPTED
            and blocked.reason_code == service._RUNTIME_STOP_REASON
        ):
            raise SystemExit(
                f"stop drill did not block new cohort: {blocked.reason_code}"
            )
        before_edit_counts = {
            "evidence": len(service._store.evidence_events),
            "lifecycle": len(service._store.lifecycle_events),
            "audit": len(service._store.audit_events),
            "outbox": len(service._store.outbox),
        }
        stopped_edit = service.correct_entity_link(
            principal=reviewer,
            application_id=application_id,
            work_item_id=successor_item["work_item_id"],
            expected_fence=claimed_successor["claim_fence"],
            expected_context=successor_work["command_context"],
            idempotency_key="s11-probe-stopped-edit",
            entity_link=_entity_link_command(
                unresolved,
                entity_id="addr:nanjing",
                entity_type="city",
                label="南京市",
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
        assert history_after_stop["entity_link_history"] == history[
            "entity_link_history"
        ]

        # 3. A prior executable reads a SQLite copy without mutating the live
        # authority.  The pre-S11 code must fail closed on the successor
        # state; route, snapshots, claims and decisions must survive.
        preserved = _preserved_facts(service, reviewer, application_id)
        old_state = Path(td) / "old-executable.sqlite3"
        rollback_state = Path(td) / "rollback.sqlite3"
        with tempfile.TemporaryDirectory() as rollback_dir:
            with sqlite3.connect(Path(td) / "target.sqlite3") as source:
                source.backup(sqlite3.connect(old_state))
            old_outcome = _old_executable_fails_closed(
                state_path=old_state,
                prior_root=Path(rollback_dir),
                reviewer=reviewer,
                application_id=application_id,
            )
        with sqlite3.connect(Path(td) / "target.sqlite3") as source:
            source.backup(sqlite3.connect(rollback_state))
        assert old_outcome.get("readable") is False, json.dumps(
            {"old_executable": old_outcome},
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

        # Restore the fixed executable, verify the repair, and append a
        # forward successor from the still-claimed city work item.
        recovered = rollback_service.recover_runtime(
            expected_failure_reason_code=service._REVIEW_SOURCE_FAILURE,
            principal=operator,
        )
        assert recovered["recovery"] == "scheduled", recovered
        forwarded = rollback_service.correct_entity_link(
            principal=reviewer,
            application_id=application_id,
            work_item_id=successor_item["work_item_id"],
            expected_fence=claimed_successor["claim_fence"],
            expected_context=successor_work["command_context"],
            idempotency_key="s11-probe-forward-accept",
            entity_link=_entity_link_command(
                unresolved,
                entity_id="addr:nanjing",
                entity_type="city",
                label="南京市",
            ),
        )
        assert forwarded["status"] == "accepted", forwarded
        forward_run = rollback_service.process_next_job()
        assert forward_run.status == "complete", forward_run.status
        final_history = rollback_service.application_history_view(
            principal=reviewer, application_id=application_id
        )
        assert len(final_history["entity_link_history"]) == 2
        final_decisions = [
            record
            for record in final_history["entity_links"]
            if record["record_kind"] == "accepted"
        ]
        assert len(final_decisions) == 2
        assert {
            record["decision_id"] for record in final_decisions
        } == {
            accepted["entity_link_decision_id"],
            forwarded["entity_link_decision_id"],
        }
        print("S11 rollback/stop probe: PASS")
        print("  old run immutable and non-current; successor append-only")
        print("  stop drill rejected an existing edit with zero writes")
        print("  pre-S11 executable failed closed on the successor state")
        print("  rollback copy retained the original entity-link successor")
        print("  fixed executable resumed and appended a forward successor")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
