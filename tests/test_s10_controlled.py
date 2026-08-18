"""S10 page-membership correction over the shared S01/C-DEMO domain seam.

The Reviewer resolves an unassigned, wrong or ambiguous document-page
membership by appending an explicit accepted decision (document instance and
role) or an explicit unassign.  Every prior candidate claim and decision stays
immutable and visible; eligibility for the checker projection comes only from
explicit accepted facts -- never from candidate confidence, order, count,
majority or last write.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from task4_consistency.controlled.s01 import (
    AdmissionDisposition,
    ControlledScenarioService,
    QueryNotFound,
    S01CommandPrincipal,
)

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = "app_s10_ambiguous_membership.json"
INTEGRATOR = S01CommandPrincipal(
    subject="s10-reviewer",
    role="integrator",
    scope="C-DEMO",
    source_id="s10-test-intake",
)
REVIEWER = S01CommandPrincipal(
    subject=INTEGRATOR.subject,
    role="reviewer",
    scope=INTEGRATOR.scope,
    source_id="s10-review-console",
)
AUDITOR = S01CommandPrincipal(
    subject="s10-auditor",
    role="auditor",
    scope=INTEGRATOR.scope,
    source_id="s10-audit-console",
)

PAGE1 = "1010101010101010101010101010101010101010101010101010101010101010"  # ambiguous
PAGE2 = "2020202020202020202020202020202020202020202020202020202020202020"  # unresolved


def _ready_membership(
    tmp_path: Path,
    *,
    fixture_root: Path | None = None,
) -> tuple[ControlledScenarioService, str, dict[str, object]]:
    service = ControlledScenarioService(
        fixture_root=fixture_root or ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        scenario_id=SCENARIO,
    )
    admitted = service.submit_demo(
        scenario_id=SCENARIO,
        idempotency_key="s10-intake",
        principal=INTEGRATOR,
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    assert admitted.application_id is not None
    completed = service.process_next_job()
    assert completed.status == "complete"
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=100,
    )
    assert len(queue["items"]) == 1
    work_item_id = queue["items"][0]["work_item_id"]
    work_item = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    claimed = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item_id,
        expected_context=work_item["command_context"],
        now=100,
    )
    assert claimed["status"] == "claimed"
    workspace = service.workspace_view(
        admitted.application_id,
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=100,
    )
    membership_blockers = [
        item
        for item in workspace["mandatory_blockers"]
        if item.get("rule_id") in {"MEMBERSHIP_UNRESOLVED", "MEMBERSHIP_AMBIGUOUS"}
    ]
    assert len(membership_blockers) == 2
    return service, str(admitted.application_id), {
        "work_item_id": work_item_id,
        "work_item": work_item,
        "claimed": claimed,
        "blockers": membership_blockers,
        "workspace": workspace,
    }


def _accept_command(
    finding: dict[str, object],
    *,
    instance_id: str,
    role: str = "机动车登记证书",
    reason_code: str = "MEMBERSHIP_SOURCE_VERIFIED",
) -> dict[str, object]:
    membership = finding["membership"]
    candidate = next(
        candidate
        for candidate in membership["candidates"]
        if candidate["document_instance_id"] == instance_id
        and candidate["document_role"] == role
    )
    return {
        "schema_version": "page-membership-correction/2",
        "finding_id": finding["finding_id"],
        "candidate_claim_id": candidate["claim_id"],
        "attachment_id": membership["attachment_id"],
        "page_source_sha256": membership["page_source_sha256"],
        "page_ordinal": membership["page_ordinal"],
        "source_evidence": copy.deepcopy(membership["source_evidence"]),
        "expected_active_decision_ids": copy.deepcopy(
            membership["active_decision_ids"]
        ),
        "decision": "accept",
        "document_instance_id": instance_id,
        "document_role": role,
        "reason_code": reason_code,
    }


def _unassign_command(finding: dict[str, object], reason_code: str) -> dict[str, object]:
    membership = finding["membership"]
    return {
        "schema_version": "page-membership-correction/2",
        "finding_id": finding["finding_id"],
        "candidate_claim_id": membership["candidates"][0]["claim_id"],
        "attachment_id": membership["attachment_id"],
        "page_source_sha256": membership["page_source_sha256"],
        "page_ordinal": membership["page_ordinal"],
        "source_evidence": copy.deepcopy(membership["source_evidence"]),
        "expected_active_decision_ids": copy.deepcopy(
            membership["active_decision_ids"]
        ),
        "decision": "unassign",
        "reason_code": reason_code,
    }


def test_integrator_membership_decisions_have_no_reviewer_authority(
    tmp_path: Path,
) -> None:
    """Integrator input can register candidate claims; Reviewer decisions are
    created only by the controlled correction command."""
    payload = json.loads(
        (ROOT / "fixtures" / "applications" / SCENARIO).read_text(encoding="utf-8")
    )
    candidate = payload["graph"]["page_memberships"][0]
    payload["graph"]["page_memberships"].extend(
        [
            {
                "record_kind": "accepted",
                "decision_id": "integrator-accepted",
                "application_id": payload["application_id"],
                "page": copy.deepcopy(candidate["page"]),
                "document_instance_id": candidate["candidate_document"][
                    "document_instance_id"
                ],
                "document_role": candidate["candidate_document"]["document_role"],
            },
            {
                "record_kind": "unassigned",
                "decision_id": "integrator-unassigned",
                "application_id": payload["application_id"],
                "page": copy.deepcopy(candidate["page"]),
            },
        ]
    )
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / SCENARIO).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    service = ControlledScenarioService(
        fixture_root=fixture_root,
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        scenario_id=SCENARIO,
    )
    admitted = service.submit_demo(
        scenario_id=SCENARIO,
        idempotency_key="s10-authority-intake",
        principal=INTEGRATOR,
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=str(admitted.application_id),
    )
    assert history["memberships"]
    assert {record["record_kind"] for record in history["memberships"]} == {
        "candidate"
    }
    assert history["membership_history"] == []


@pytest.mark.parametrize("invalid_kind", ["malformed", "duplicate"])
def test_invalid_candidate_claim_rejects_admission_without_application(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    payload = json.loads(
        (ROOT / "fixtures" / "applications" / SCENARIO).read_text(encoding="utf-8")
    )
    candidates = payload["graph"]["page_memberships"]
    if invalid_kind == "malformed":
        candidates[0]["page"]["source_sha256"] = "unknown"
    else:
        candidates.append(copy.deepcopy(candidates[0]))
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / SCENARIO).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    service = ControlledScenarioService(
        fixture_root=fixture_root,
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        scenario_id=SCENARIO,
    )

    rejected = service.submit_demo(
        scenario_id=SCENARIO,
        idempotency_key=f"s10-invalid-candidate-{invalid_kind}",
        principal=INTEGRATOR,
    )

    assert rejected.disposition is AdmissionDisposition.REJECTED
    assert rejected.reason_code == "INVALID_CANONICAL_ENVELOPE"
    assert rejected.application_id is None
    assert service.fact_counts() == {
        "applications": 0,
        "receipts": 0,
        "lifecycle_events": 0,
        "evidence_events": 0,
        "audit_events": 0,
        "jobs": 0,
        "attempts": 0,
        "runs": 0,
        "findings": 0,
        "outbox": 0,
    }
    assert service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
    )["items"] == []


def test_membership_blockers_surface_every_candidate_without_selection(
    tmp_path: Path,
) -> None:
    """The workspace presents coexisting candidate claims and provenance without
    silently selecting a winner by type, order, confidence, majority or last
    write.  A single candidate is unresolved and coexisting candidates are
    ambiguous."""
    service, application_id, state = _ready_membership(tmp_path)
    blockers = state["blockers"]
    by_sha = {item["membership"]["page_source_sha256"]: item for item in blockers}
    assert set(by_sha) == {PAGE1, PAGE2}
    ambiguous = by_sha[PAGE1]
    assert ambiguous["rule_id"] == "MEMBERSHIP_AMBIGUOUS"
    assert ambiguous["verdict"] == "uncertain"
    assert ambiguous["mandatory"] is True
    assert {c["document_instance_id"] for c in ambiguous["membership"]["candidates"]} == {
        "reg_cert_instance_a",
        "reg_cert_instance_b",
    }
    unresolved = by_sha[PAGE2]
    assert unresolved["rule_id"] == "MEMBERSHIP_UNRESOLVED"
    assert unresolved["membership"]["state"] == "unresolved"
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert {item["record_kind"] for item in history["memberships"]} == {
        "candidate"
    }


def test_membership_finding_exposes_complete_command_identity(tmp_path: Path) -> None:
    _, _, state = _ready_membership(tmp_path)
    ambiguous = next(
        item
        for item in state["blockers"]
        if item["rule_id"] == "MEMBERSHIP_AMBIGUOUS"
    )["membership"]
    assert ambiguous["attachment_id"] == "s10-attachment-1"
    assert ambiguous["page_ordinal"] == 1
    assert ambiguous["active_decision_ids"] == []
    assert ambiguous["source_evidence"]["evidence_revision"] == 1
    assert ambiguous["source_evidence"]["event_id"].startswith("evidence_")
    assert {candidate["claim_id"] for candidate in ambiguous["candidates"]} == {
        "s10_claim_page1_a",
        "s10::claim_page1_b",
    }


def test_workspace_ledger_exposes_every_candidate_and_provenance(
    tmp_path: Path,
) -> None:
    _, _, state = _ready_membership(tmp_path)
    ledger = state["workspace"]["membership_ledger"]
    by_page = {
        (page["attachment_id"], page["page_ordinal"]): page for page in ledger
    }
    assert set(by_page) == {
        ("s10-attachment-1", 1),
        ("s10-attachment-2", 2),
    }
    assert by_page[("s10-attachment-1", 1)]["state"] == "ambiguous"
    assert by_page[("s10-attachment-2", 2)]["state"] == "unresolved"
    assert {
        (candidate["claim_id"], candidate["provenance"]["source_pointer"])
        for candidate in by_page[("s10-attachment-1", 1)]["candidates"]
    } == {
        ("s10_claim_page1_a", "/pages/0"),
        ("s10::claim_page1_b", "/pages/0"),
    }
    assert all(page["decisions"] == [] for page in ledger)
    assert all(page["finding_id"] for page in ledger)


def test_same_sha_attachment_pages_remain_distinct_membership_targets(
    tmp_path: Path,
) -> None:
    payload = json.loads(
        (ROOT / "fixtures" / "applications" / SCENARIO).read_text(encoding="utf-8")
    )
    payload["graph"]["page_memberships"][2]["page"]["source_sha256"] = PAGE1
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / SCENARIO).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    _, _, state = _ready_membership(tmp_path, fixture_root=fixture_root)
    identities = {
        (
            item["membership"]["attachment_id"],
            item["membership"]["page_ordinal"],
        )
        for item in state["blockers"]
    }
    assert identities == {
        ("s10-attachment-1", 1),
        ("s10-attachment-2", 2),
    }


@pytest.mark.parametrize(
    ("field", "value", "reason_code"),
    [
        ("page_ordinal", 100, "MEMBERSHIP_PAGE_OUTSIDE_APPLICATION"),
        (
            "attachment_id",
            "attachment_from_another_application",
            "MEMBERSHIP_PAGE_OUTSIDE_APPLICATION",
        ),
        ("candidate_claim_id", "missing_claim", "MEMBERSHIP_CLAIM_MISMATCH"),
        (
            "source_evidence",
            {"event_id": "evidence_stale", "evidence_revision": 1},
            "STALE_MEMBERSHIP_SOURCE_EVIDENCE",
        ),
        (
            "expected_active_decision_ids",
            ["decision_stale"],
            "STALE_MEMBERSHIP_PREDECESSORS",
        ),
    ],
)
def test_membership_identity_conflicts_have_zero_public_effects(
    tmp_path: Path,
    field: str,
    value: object,
    reason_code: str,
) -> None:
    service, application_id, state = _ready_membership(tmp_path)
    finding = next(
        item
        for item in state["blockers"]
        if item["rule_id"] == "MEMBERSHIP_AMBIGUOUS"
    )
    command = _accept_command(finding, instance_id="reg_cert_instance_a")
    command[field] = value
    route_before = service.current_route_view(
        principal=REVIEWER, application_id=application_id
    )
    audit_before = service.audit_timeline(
        principal=AUDITOR, application_id=application_id
    )

    result = service.correct_page_membership(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=state["work_item_id"],
        expected_fence=state["claimed"]["claim_fence"],
        expected_context=state["work_item"]["command_context"],
        idempotency_key=f"s10-conflict-{field}",
        membership=command,
        now=101,
    )

    assert result["status"] in {"rejected", "stale"}
    assert result["reason_code"] == reason_code
    assert service.current_route_view(
        principal=REVIEWER, application_id=application_id
    ) == route_before
    assert service.audit_timeline(
        principal=AUDITOR, application_id=application_id
    ) == audit_before
    assert service.application_history_view(
        principal=REVIEWER, application_id=application_id
    )["membership_history"] == []


def test_membership_successor_requires_fresh_current_run(tmp_path: Path) -> None:
    """A correction advances Evidence and invalidates the old run; the old run
    stays immutable in history and the route only changes after one fresh
    complete run wins current-run CAS.  A second command against the stale
    (pre-acceptance) context conflicts and creates no second successor."""
    service, application_id, state = _ready_membership(tmp_path)
    work_item_id = state["work_item_id"]
    work_item = state["work_item"]
    claimed = state["claimed"]
    ambiguous = next(item for item in state["blockers"] if item["rule_id"] == "MEMBERSHIP_AMBIGUOUS")

    accepted = service.correct_page_membership(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=work_item["command_context"],
        idempotency_key="s10-accept-1",
        membership=_accept_command(ambiguous, instance_id="reg_cert_instance_a"),
        now=101,
    )
    assert accepted["status"] == "accepted"
    assert accepted["evidence_revision"] == 2
    assert accepted["phase"] == "Assembly"
    assert accepted["route"] == "pending_check"
    assert accepted["invalidated_run_id"] == work_item["run_authority"]["run_id"]

    pending = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    # While the replacement run is pending there is no current run and the
    # route is not changed by the failed second command below.
    assert pending["route"] == "pending_check"
    assert pending["current_run_id"] is None
    assert pending["evidence_revision"] == 2

    # A concurrent/duplicate command against the old (stale) context conflicts.
    again = service.correct_page_membership(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=work_item["command_context"],
        idempotency_key="s10-accept-2",
        membership=_accept_command(
            ambiguous,
            instance_id="reg_cert_instance_b",
            reason_code="MEMBERSHIP_SOURCE_MISASSIGNED",
        ),
        now=102,
    )
    assert again["status"] == "stale"
    assert again["reason_code"] == "STALE_REVIEW_CONTEXT"
    unchanged = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert unchanged == pending
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert len(history["membership_history"]) == 1

    # Now the replacement run completes and wins current-run CAS.
    replaced = service.process_next_job()
    assert replaced.status == "complete"
    assert replaced.evidence_revision == 2
    current = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert current["route"] == "manual_review"
    assert current["current_run_id"] == replaced.run_id
    assert current["evidence_revision"] == 2
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    run_ids = [item["run_id"] for item in history["runs"]]
    old_run_id = work_item["run_authority"]["run_id"]
    assert old_run_id in run_ids  # old run retained immutable
    assert replaced.run_id in run_ids
    old_run = next(item for item in history["runs"] if item["run_id"] == old_run_id)
    new_run = next(item for item in history["runs"] if item["run_id"] == replaced.run_id)
    assert old_run["current"] is False
    assert new_run["current"] is True
    assert old_run["evidence_revision"] == 1
    assert new_run["evidence_revision"] == 2
    ledger = {
        record["decision_id"]: record
        for record in history["memberships"]
        if record["record_kind"] in {"accepted", "unassigned"}
    }
    assert ledger[accepted["membership_decision_id"]]["status"] == "active"
    assert ledger[accepted["membership_decision_id"]]["supersedes"] == []
    assert ledger[accepted["membership_decision_id"]]["cycle"] == 1
    assert history["membership_history"][0]["cycle"] == 1
    membership_audit = next(
        event
        for event in service.audit_timeline(
            principal=AUDITOR,
            application_id=application_id,
        )["events"]
        if event["action"] == "page_membership_corrected"
    )
    assert membership_audit["context"]["cycle"] == 1


def test_accepted_instance_changes_and_pins_the_frozen_snapshot(
    tmp_path: Path,
) -> None:
    service, application_id, state = _ready_membership(tmp_path)
    old_history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    old_run = next(run for run in old_history["runs"] if run["current"])
    ambiguous = next(
        item
        for item in state["blockers"]
        if item["rule_id"] == "MEMBERSHIP_AMBIGUOUS"
    )

    accepted = service.correct_page_membership(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=state["work_item_id"],
        expected_fence=state["claimed"]["claim_fence"],
        expected_context=state["work_item"]["command_context"],
        idempotency_key="s10-accept-instance-b",
        membership=_accept_command(ambiguous, instance_id="reg_cert_instance_b"),
        now=101,
    )
    assert accepted["status"] == "accepted"
    completed = service.process_next_job()
    assert completed.status == "complete"

    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    new_run = next(run for run in history["runs"] if run["current"])
    assert new_run["evidence_snapshot_digest"] != old_run["evidence_snapshot_digest"]
    assert new_run["evidence_document_instance_ids"] == ["reg_cert_instance_b"]
    assert new_run["membership_decisions"] == [
        {
            "decision_id": accepted["membership_decision_id"],
            "candidate_claim_id": "s10::claim_page1_b",
            "attachment_id": "s10-attachment-1",
            "page_source_sha256": PAGE1,
            "page_ordinal": 1,
            "document_instance_id": "reg_cert_instance_b",
            "document_role": "机动车登记证书",
            "decision": "accept",
            "evidence_revision": 2,
        }
    ]
    assert old_run["current"] is True
    assert next(
        run for run in history["runs"] if run["run_id"] == old_run["run_id"]
    )["current"] is False


def test_membership_unassign_is_an_explicit_decision(
    tmp_path: Path,
) -> None:
    """An explicit unassign is a Reviewer decision that preserves every source
    candidate and keeps the page outside checker evidence."""
    service, application_id, state = _ready_membership(tmp_path)
    work_item_id = state["work_item_id"]
    work_item = state["work_item"]
    claimed = state["claimed"]
    ambiguous = next(item for item in state["blockers"] if item["rule_id"] == "MEMBERSHIP_AMBIGUOUS")

    unassigned = service.correct_page_membership(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=work_item["command_context"],
        idempotency_key="s10-unassign",
        membership=_unassign_command(
            ambiguous, reason_code="MEMBERSHIP_PAGE_UNASSIGNED"
        ),
        now=101,
    )
    assert unassigned["status"] == "accepted"
    assert unassigned["decision"] == "unassign"
    assert unassigned["evidence_revision"] == 2
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    by_id = {
        record["decision_id"]: record
        for record in history["memberships"]
        if record["record_kind"] in {"accepted", "unassigned"}
    }
    unassign_record = by_id[unassigned["membership_decision_id"]]
    assert unassign_record["status"] == "active"
    assert unassign_record["supersedes"] == []
    replaced = service.process_next_job()
    assert replaced.status == "complete"
    current = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    # Only the unresolved PAGE2 remains a membership blocker.
    service.refresh_projection()
    workspace = service.workspace_view(
        application_id,
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=102,
    )
    blockers = [
        item
        for item in workspace["mandatory_blockers"]
        if item.get("rule_id") in {"MEMBERSHIP_UNRESOLVED", "MEMBERSHIP_AMBIGUOUS"}
    ]
    assert {item["membership"]["page_source_sha256"] for item in blockers} == {PAGE2}
    page1 = next(
        item
        for item in workspace["membership_ledger"]
        if item["attachment_id"] == "s10-attachment-1"
        and item["page_ordinal"] == 1
    )
    assert page1["state"] == "unassigned"
    assert current["route"] == "manual_review"


def test_membership_sequential_corrections_rerun_to_auto_complete(
    tmp_path: Path,
) -> None:
    """Accepting the ambiguous page and then explicitly unassigning the last
    unresolved page removes every membership blocker and the application
    returns through readiness and a new run to automatic completion, with both
    runs retained in immutable history."""
    service, application_id, state = _ready_membership(tmp_path)
    work_item_id = state["work_item_id"]
    work_item = state["work_item"]
    claimed = state["claimed"]
    ambiguous = next(item for item in state["blockers"] if item["rule_id"] == "MEMBERSHIP_AMBIGUOUS")

    accepted = service.correct_page_membership(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=work_item["command_context"],
        idempotency_key="s10-accept",
        membership=_accept_command(ambiguous, instance_id="reg_cert_instance_a"),
        now=101,
    )
    replaced = service.process_next_job()
    assert replaced.status == "complete"
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=102,
    )
    work_item2_id = queue["items"][0]["work_item_id"]
    work_item2 = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item2_id,
        now=102,
    )
    claimed2 = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item2_id,
        expected_context=work_item2["command_context"],
        now=102,
    )
    workspace2 = service.workspace_view(
        application_id,
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=102,
    )
    blockers2 = [
        item
        for item in workspace2["mandatory_blockers"]
        if item.get("rule_id") in {"MEMBERSHIP_UNRESOLVED", "MEMBERSHIP_AMBIGUOUS"}
    ]
    assert {item["membership"]["page_source_sha256"] for item in blockers2} == {PAGE2}

    unassigned = service.correct_page_membership(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item2_id,
        expected_fence=claimed2["claim_fence"],
        expected_context=work_item2["command_context"],
        idempotency_key="s10-unassign",
        membership=_unassign_command(
            blockers2[0], reason_code="MEMBERSHIP_PAGE_UNASSIGNED"
        ),
        now=103,
    )
    assert unassigned["status"] == "accepted"
    assert unassigned["decision"] == "unassign"
    replaced2 = service.process_next_job()
    assert replaced2.status == "complete"
    current = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    # After the unassign there are no membership blockers left -> auto complete.
    assert current["route"] == "auto_complete"
    assert current["current_run_id"] == replaced2.run_id
    assert current["evidence_revision"] == 3
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    run_ids = [item["run_id"] for item in history["runs"]]
    assert work_item["run_authority"]["run_id"] in run_ids
    assert replaced.run_id in run_ids
    assert replaced2.run_id in run_ids
    # The accepted P1 decision was superseded by the P2 unassign? No: the
    # unassign only withdraws decision facts of its own page.  The accepted P1
    # decision remains active history (immutable).
    by_id = {
        record["decision_id"]: record
        for record in history["memberships"]
        if record["record_kind"] in {"accepted", "unassigned"}
    }
    assert by_id[accepted["membership_decision_id"]]["status"] == "active"
    assert by_id[unassigned["membership_decision_id"]]["status"] == "active"
    assert len(history["membership_history"]) == 2


def test_later_membership_decision_supersedes_active_predecessor(
    tmp_path: Path,
) -> None:
    service, application_id, state = _ready_membership(tmp_path)
    ambiguous = next(
        item
        for item in state["blockers"]
        if item["rule_id"] == "MEMBERSHIP_AMBIGUOUS"
    )
    first = service.correct_page_membership(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=state["work_item_id"],
        expected_fence=state["claimed"]["claim_fence"],
        expected_context=state["work_item"]["command_context"],
        idempotency_key="s10-first-membership-decision",
        membership=_accept_command(ambiguous, instance_id="reg_cert_instance_a"),
        now=101,
    )
    assert first["status"] == "accepted"
    assert service.process_next_job().status == "complete"
    service.refresh_projection()

    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=102,
    )
    successor_work_id = queue["items"][0]["work_item_id"]
    successor_work = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=successor_work_id,
        now=102,
    )
    successor_claim = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=successor_work_id,
        expected_context=successor_work["command_context"],
        now=102,
    )
    workspace = service.workspace_view(
        application_id,
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=102,
    )
    page = next(
        item
        for item in workspace["membership_ledger"]
        if item["attachment_id"] == "s10-attachment-1"
        and item["page_ordinal"] == 1
    )
    assert page["state"] == "selected"
    assert page["finding_id"] == ambiguous["finding_id"]
    assert page["active_decision_ids"] == [first["membership_decision_id"]]
    assert page["source_evidence"]["evidence_revision"] == 2

    successor = service.correct_page_membership(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=successor_work_id,
        expected_fence=successor_claim["claim_fence"],
        expected_context=successor_work["command_context"],
        idempotency_key="s10-successor-membership-decision",
        membership=_accept_command(
            {"finding_id": page["finding_id"], "membership": page},
            instance_id="reg_cert_instance_b",
            reason_code="MEMBERSHIP_SOURCE_MISASSIGNED",
        ),
        now=103,
    )

    assert successor["status"] == "accepted"
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    decisions = {
        item["decision_id"]: item
        for item in history["memberships"]
        if item["record_kind"] in {"accepted", "unassigned"}
    }
    assert decisions[first["membership_decision_id"]]["status"] == "superseded"
    assert decisions[successor["membership_decision_id"]]["status"] == "active"
    assert decisions[successor["membership_decision_id"]]["supersedes"] == [
        first["membership_decision_id"]
    ]
    assert len(
        [item for item in history["memberships"] if item["record_kind"] == "candidate"]
    ) == 3


def test_membership_ambiguous_role_input_is_conflict_without_revision(
    tmp_path: Path,
) -> None:
    """Ambiguous role input (accept without instance+role, or unassign with a
    role) is a validation conflict and never creates a successor."""
    service, application_id, state = _ready_membership(tmp_path)
    ambiguous = next(item for item in state["blockers"] if item["rule_id"] == "MEMBERSHIP_AMBIGUOUS")
    membership = ambiguous["membership"]
    with pytest.raises(ValueError):
        service.correct_page_membership(
            principal=REVIEWER,
            application_id=application_id,
            work_item_id=state["work_item_id"],
            expected_fence=state["claimed"]["claim_fence"],
            expected_context=state["work_item"]["command_context"],
            idempotency_key="s10-bad-role",
            membership={
                "schema_version": "page-membership-correction/1",
                "finding_id": ambiguous["finding_id"],
                "page_source_sha256": membership["page_source_sha256"],
                "page_ordinal": membership["page_ordinal"],
                "decision": "accept",
                "reason_code": "MEMBERSHIP_SOURCE_VERIFIED",
            },
            now=101,
        )
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert history["membership_history"] == []


def test_membership_page_outside_application_is_rejected(tmp_path: Path) -> None:
    """A page from another attachment or application cannot become a target."""
    service, application_id, state = _ready_membership(tmp_path)
    ambiguous = next(item for item in state["blockers"] if item["rule_id"] == "MEMBERSHIP_AMBIGUOUS")
    command = _accept_command(ambiguous, instance_id="reg_cert_instance_a")
    command["attachment_id"] = "attachment_from_another_application"
    rejected = service.correct_page_membership(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=state["work_item_id"],
        expected_fence=state["claimed"]["claim_fence"],
        expected_context=state["work_item"]["command_context"],
        idempotency_key="s10-cross-app",
        membership=command,
        now=101,
    )
    assert rejected["status"] == "rejected"
    assert rejected["reason_code"] == "MEMBERSHIP_PAGE_OUTSIDE_APPLICATION"


def test_membership_idempotency_replays_one_successor(tmp_path: Path) -> None:
    """Repeated idempotency key returns the original result and leaves one
    successor, one invalidation, one audit fact and one outbox event."""
    service, application_id, state = _ready_membership(tmp_path)
    ambiguous = next(item for item in state["blockers"] if item["rule_id"] == "MEMBERSHIP_AMBIGUOUS")
    command = _accept_command(ambiguous, instance_id="reg_cert_instance_a")
    first = service.correct_page_membership(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=state["work_item_id"],
        expected_fence=state["claimed"]["claim_fence"],
        expected_context=state["work_item"]["command_context"],
        idempotency_key="s10-same-key",
        membership=command,
        now=101,
    )
    second = service.correct_page_membership(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=state["work_item_id"],
        expected_fence=state["claimed"]["claim_fence"],
        expected_context=state["work_item"]["command_context"],
        idempotency_key="s10-same-key",
        membership=command,
        now=102,
    )
    assert first["status"] == "accepted"
    assert second["status"] == "accepted"
    assert second["replayed"] is True
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    records = [
        record
        for record in history["memberships"]
        if record["record_kind"] in {"accepted", "unassigned"}
        and record["page"]["source_sha256"] == PAGE1
        and record["status"] == "active"
    ]
    assert len(records) == 1
    membership_audits = [
        event
        for event in service.audit_timeline(
            principal=AUDITOR,
            application_id=application_id,
        )["events"]
        if event["action"] == "page_membership_corrected"
    ]
    assert len(membership_audits) == 1


@pytest.mark.parametrize(
    "fault_point",
    (
        "membership.evidence",
        "membership.lifecycle",
        "membership.work_item",
        "membership.job",
        "membership.outbox",
        "membership.audit",
        "membership.idempotency",
        "membership.publish",
    ),
)
def test_each_membership_write_fault_has_zero_public_effect(
    tmp_path: Path,
    fault_point: str,
) -> None:
    service, application_id, state = _ready_membership(tmp_path)
    ambiguous = next(
        item
        for item in state["blockers"]
        if item["rule_id"] == "MEMBERSHIP_AMBIGUOUS"
    )

    def observe() -> dict[str, object]:
        return {
            "route": service.current_route_view(
                principal=REVIEWER,
                application_id=application_id,
            ),
            "work_item": service.review_work_item_view(
                principal=REVIEWER,
                work_item_id=state["work_item_id"],
                now=100,
            ),
            "workspace": service.workspace_view(
                application_id,
                role="reviewer",
                scope=REVIEWER.scope,
                subject=REVIEWER.subject,
                now=100,
            ),
            "history": service.application_history_view(
                principal=REVIEWER,
                application_id=application_id,
            ),
            "audit": service.audit_timeline(
                principal=AUDITOR,
                application_id=application_id,
            ),
        }

    before = observe()

    def fail(selected: str) -> None:
        if selected == fault_point:
            raise OSError("injected membership write failure")

    faulty = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        scenario_id=SCENARIO,
        fault_injector=fail,
    )
    failed = faulty.correct_page_membership(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=state["work_item_id"],
        expected_fence=state["claimed"]["claim_fence"],
        expected_context=state["work_item"]["command_context"],
        idempotency_key=f"s10-fault-{fault_point}",
        membership=_accept_command(ambiguous, instance_id="reg_cert_instance_a"),
        now=101,
    )

    assert failed["status"] == "unavailable"
    assert failed["reason_code"] == (
        "AUDIT_UNAVAILABLE"
        if fault_point == "membership.audit"
        else "STORAGE_UNAVAILABLE"
    )
    assert observe() == before


def test_membership_requires_finding_in_current_run(tmp_path: Path) -> None:
    """A correction must reference a mandatory membership finding of the current
    run; a stale finding (other rule) is not correctable."""
    service, application_id, state = _ready_membership(tmp_path)
    ambiguous = next(item for item in state["blockers"] if item["rule_id"] == "MEMBERSHIP_AMBIGUOUS")
    command = _accept_command(ambiguous, instance_id="reg_cert_instance_a")
    command["finding_id"] = "finding_missing"
    with pytest.raises(ValueError):
        service.correct_page_membership(
            principal=REVIEWER,
            application_id=application_id,
            work_item_id=state["work_item_id"],
            expected_fence=state["claimed"]["claim_fence"],
            expected_context=state["work_item"]["command_context"],
            idempotency_key="s10-bad-finding",
            membership=command,
            now=101,
        )


def test_field_correction_preserves_page_membership_ledger(tmp_path: Path) -> None:
    """An S04 field-correction successor must carry the admitted graph,
    including the S10 page-membership ledger, or it would erase memberships.
    (M evidence: ``graph.page_memberships`` survives every Evidence successor.)"""
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        scenario_id="app_s10_membership_field.json",
    )
    admitted = service.submit_demo(
        scenario_id="app_s10_membership_field.json",
        idempotency_key="s10-field-intake",
        principal=INTEGRATOR,
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    completed = service.process_next_job()
    assert completed.status == "complete"
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer", scope=REVIEWER.scope, subject=REVIEWER.subject, now=100
    )
    work_item_id = queue["items"][0]["work_item_id"]
    work_item = service.review_work_item_view(
        principal=REVIEWER, work_item_id=work_item_id, now=100
    )
    claimed = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item_id,
        expected_context=work_item["command_context"],
        now=100,
    )
    workspace = service.workspace_view(
        admitted.application_id,
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=100,
    )
    field_finding = workspace["selected_finding"]
    assert field_finding["rule_id"] == "R_ENGINE_CROSS"
    source = next(
        link
        for link in field_finding["evidence_links"]
        if link["document_id"] == "reg"
    )
    memberships_before = service.application_history_view(
        principal=REVIEWER,
        application_id=admitted.application_id,
    )["memberships"]
    accepted = service.correct_field_observation(
        principal=REVIEWER,
        application_id=admitted.application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=work_item["command_context"],
        idempotency_key="s10-field-correction",
        correction={
            "schema_version": "field-observation-correction/1",
            "finding_id": field_finding["finding_id"],
            "observation_id": source["observation_id"],
            "document_id": source["document_id"],
            "document_role": source["document_role"],
            "field": source["field"],
            "raw": "S2ENG54A",
            "source_location": {
                key: source[key]
                for key in ("source_sha256", "source_page", "source_region")
            },
            "reason_code": "SOURCE_VALUE_MISREAD",
        },
        now=101,
    )
    assert accepted["status"] == "accepted"
    memberships_after = service.application_history_view(
        principal=REVIEWER,
        application_id=admitted.application_id,
    )["memberships"]
    assert memberships_after == memberships_before
    assert len(memberships_after) == 3
    assert {item["record_kind"] for item in memberships_after} == {"candidate"}


def test_membership_accept_must_reference_a_candidate(tmp_path: Path) -> None:
    """An accepted decision must reference one of the page's coexisting
    candidate claims; accepting an instance/role with no source evidence in the
    application is rejected with no successor."""
    service, application_id, state = _ready_membership(tmp_path)
    ambiguous = next(
        item for item in state["blockers"] if item["rule_id"] == "MEMBERSHIP_AMBIGUOUS"
    )
    command = _accept_command(
        ambiguous,
        instance_id="reg_cert_instance_a",
        reason_code="MEMBERSHIP_SOURCE_MISASSIGNED",
    )
    command["document_instance_id"] = "invented_instance_not_in_ledger"
    accepted = service.correct_page_membership(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=state["work_item_id"],
        expected_fence=state["claimed"]["claim_fence"],
        expected_context=state["work_item"]["command_context"],
        idempotency_key="s10-accept-non-candidate",
        membership=command,
        now=101,
    )
    assert accepted["status"] == "rejected"
    assert accepted["reason_code"] == "MEMBERSHIP_ACCEPT_NOT_CANDIDATE"
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert history["membership_history"] == []


def test_membership_reason_must_match_decision(tmp_path: Path) -> None:
    """An ``accept`` may not carry an unassign reason and an ``unassign`` may
    not carry an accept/instance reason: contradictory pairings are validation
    conflicts with no successor."""
    service, application_id, state = _ready_membership(tmp_path)
    ambiguous = next(
        item for item in state["blockers"] if item["rule_id"] == "MEMBERSHIP_AMBIGUOUS"
    )
    with pytest.raises(ValueError):
        service.correct_page_membership(
            principal=REVIEWER,
            application_id=application_id,
            work_item_id=state["work_item_id"],
            expected_fence=state["claimed"]["claim_fence"],
            expected_context=state["work_item"]["command_context"],
            idempotency_key="s10-accept-unassign-reason",
            membership=_accept_command(
                ambiguous,
                instance_id="reg_cert_instance_a",
                reason_code="MEMBERSHIP_PAGE_UNASSIGNED",
            ),
            now=101,
        )
    unresolved = next(
        item for item in state["blockers"] if item["rule_id"] == "MEMBERSHIP_UNRESOLVED"
    )
    with pytest.raises(ValueError):
        service.correct_page_membership(
            principal=REVIEWER,
            application_id=application_id,
            work_item_id=state["work_item_id"],
            expected_fence=state["claimed"]["claim_fence"],
            expected_context=state["work_item"]["command_context"],
            idempotency_key="s10-unassign-instance-reason",
            membership=_unassign_command(
                unresolved, reason_code="MEMBERSHIP_INSTANCE_WRONG"
            ),
            now=101,
        )
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert history["membership_history"] == []


def test_membership_late_terminal_cycle_edit_is_rejected(tmp_path: Path) -> None:
    """After the application reaches automatic completion, a late membership
    edit against the sealed cycle is rejected with a stable code and never
    changes the current route."""
    service, application_id, state = _ready_membership(tmp_path)
    ambiguous = next(
        item for item in state["blockers"] if item["rule_id"] == "MEMBERSHIP_AMBIGUOUS"
    )
    service.correct_page_membership(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=state["work_item_id"],
        expected_fence=state["claimed"]["claim_fence"],
        expected_context=state["work_item"]["command_context"],
        idempotency_key="s10-accept",
        membership=_accept_command(ambiguous, instance_id="reg_cert_instance_a"),
        now=101,
    )
    service.process_next_job()
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer", scope=REVIEWER.scope, subject=REVIEWER.subject, now=102
    )
    work_item2_id = queue["items"][0]["work_item_id"]
    work_item2 = service.review_work_item_view(
        principal=REVIEWER, work_item_id=work_item2_id, now=102
    )
    claimed2 = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item2_id,
        expected_context=work_item2["command_context"],
        now=102,
    )
    workspace2 = service.workspace_view(
        application_id,
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=102,
    )
    unresolved = next(
        item
        for item in workspace2["mandatory_blockers"]
        if item["rule_id"] == "MEMBERSHIP_UNRESOLVED"
    )
    service.correct_page_membership(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item2_id,
        expected_fence=claimed2["claim_fence"],
        expected_context=work_item2["command_context"],
        idempotency_key="s10-unassign",
        membership=_unassign_command(unresolved, reason_code="MEMBERSHIP_PAGE_UNASSIGNED"),
        now=103,
    )
    replaced = service.process_next_job()
    assert replaced.status == "complete"
    current = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert current["route"] == "auto_complete"

    # A late edit against the last (completed) work item of the sealed cycle is
    # rejected with a stable code and leaves the current route unchanged.
    late = service.correct_page_membership(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item2_id,
        expected_fence=claimed2["claim_fence"],
        expected_context=work_item2["command_context"],
        idempotency_key="s10-late-terminal",
        membership=_unassign_command(unresolved, reason_code="MEMBERSHIP_PAGE_UNASSIGNED"),
        now=104,
    )
    assert late["status"] == "stale"
    assert late["reason_code"] in {
        "STALE_WORK_ITEM_CLAIM",
        "STALE_REVIEW_CONTEXT",
    }
    unchanged = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert unchanged == current
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert len(history["membership_history"]) == 2  # no late successor recorded
