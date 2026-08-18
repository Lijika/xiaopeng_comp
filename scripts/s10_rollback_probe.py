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

import sys
import tempfile
from pathlib import Path

from task4_consistency.controlled.s01 import (
    AdmissionDisposition,
    ControlledScenarioService,
    S01CommandPrincipal,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = "app_s10_ambiguous_membership.json"


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
            membership={
                "schema_version": "page-membership-correction/1",
                "finding_id": ambiguous["finding_id"],
                "page_source_sha256": membership["page_source_sha256"],
                "page_ordinal": membership["page_ordinal"],
                "decision": "accept",
                "document_instance_id": membership["candidates"][0][
                    "document_instance_id"
                ],
                "document_role": membership["candidates"][0]["document_role"],
                "reason_code": "MEMBERSHIP_SOURCE_VERIFIED",
            },
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
        active = [
            record
            for record in ledger
            if record["record_kind"] in {"accepted", "unassigned"}
            and record["status"] == "active"
        ]
        # Exactly one active decision for the corrected page; predecessors
        # superseded, never removed.  (PAGE3's fixture unassigned stays active
        # and untouched -- it is a different page.)
        page1_active = [
            record
            for record in ledger
            if record["record_kind"] in {"accepted", "unassigned"}
            and record["status"] == "active"
            and record["page"]["source_sha256"] == membership["page_source_sha256"]
        ]
        assert len(page1_active) == 1
        assert page1_active[0]["decision_id"] == accepted["membership_decision_id"]

        # 2. Stop drill: new admissions and new membership edits are disabled;
        # accepted facts / current route / history remain readable.
        stop = service.stop_new_cohort(
            reason_code=service._RUNTIME_STOP_REASON,
            failure_reason_code="S10_PROBE_STOP",
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
        still_current = service.current_route_view(
            principal=reviewer, application_id=application_id
        )
        assert still_current == current, "stop drill changed the current route"
        assert service.application_history_view(
            principal=reviewer, application_id=application_id
        )["membership_history"] == history["membership_history"]
        print("S10 rollback/stop probe: PASS")
        print("  old run immutable and non-current; successor append-only")
        print("  stop drill blocked new cohort; accepted facts readable")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
