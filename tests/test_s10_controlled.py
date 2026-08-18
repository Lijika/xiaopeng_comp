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

PAGE1 = "1010101010101010101010101010101010101010101010101010101010101010"  # ambiguous
PAGE2 = "2020202020202020202020202020202020202020202020202020202020202020"  # unresolved
PAGE3 = "3030303030303030303030303030303030303030303030303030303030303030"  # explicit unassigned


def _ready_membership(
    tmp_path: Path,
) -> tuple[ControlledScenarioService, str, dict[str, object]]:
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
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
    }


def _accept_command(
    finding: dict[str, object],
    *,
    instance_id: str,
    role: str = "机动车登记证书",
    reason_code: str = "MEMBERSHIP_SOURCE_VERIFIED",
) -> dict[str, object]:
    membership = finding["membership"]
    return {
        "schema_version": "page-membership-correction/1",
        "finding_id": finding["finding_id"],
        "page_source_sha256": membership["page_source_sha256"],
        "page_ordinal": membership["page_ordinal"],
        "decision": "accept",
        "document_instance_id": instance_id,
        "document_role": role,
        "reason_code": reason_code,
    }


def _unassign_command(finding: dict[str, object], reason_code: str) -> dict[str, object]:
    membership = finding["membership"]
    return {
        "schema_version": "page-membership-correction/1",
        "finding_id": finding["finding_id"],
        "page_source_sha256": membership["page_source_sha256"],
        "page_ordinal": membership["page_ordinal"],
        "decision": "unassign",
        "reason_code": reason_code,
    }


def test_membership_blockers_surface_every_candidate_without_selection(
    tmp_path: Path,
) -> None:
    """The workspace presents coexisting candidate claims and provenance without
    silently selecting a winner by type, order, confidence, majority or last
    write.  An explicitly unassigned page is not a blocker; an unresolved page
    and an ambiguous page are."""
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
    ledger_pages = {
        item["page"]["source_sha256"]: item
        for item in history["memberships"]
        if item["record_kind"] in {"accepted", "unassigned"}
    }
    assert ledger_pages[PAGE3]["record_kind"] == "unassigned"
    assert ledger_pages[PAGE3]["status"] == "active"


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
    # The accepted decision superseded both fixture-accepted predecessors.
    ledger = {
        record["decision_id"]: record
        for record in history["memberships"]
        if record["record_kind"] in {"accepted", "unassigned"}
    }
    assert ledger["s10_fixture_accept_page1_a"]["status"] == "superseded"
    assert ledger["s10_fixture_accept_page1_b"]["status"] == "superseded"
    assert ledger[accepted["membership_decision_id"]]["status"] == "active"


def test_membership_unassign_withdraws_accepted_predecessors(
    tmp_path: Path,
) -> None:
    """An explicit unassign is a first-class accepted decision that withdraws or
    supersedes every active accepted predecessor of the page, appends the fact,
    and keeps every prior candidate/decision visible (append-only)."""
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
    # Both fixture-accepted predecessors were withdrawn by the unassign.
    assert by_id["s10_fixture_accept_page1_a"]["status"] == "superseded"
    assert by_id["s10_fixture_accept_page1_b"]["status"] == "superseded"
    unassign_record = by_id[unassigned["membership_decision_id"]]
    assert unassign_record["status"] == "active"
    assert set(unassign_record["supersedes"]) == {
        "s10_fixture_accept_page1_a",
        "s10_fixture_accept_page1_b",
    }
    # The page is now explicitly unassigned and outside the checker projection.
    effective = service._effective_page_memberships(
        service._admitted_graph(service._store.applications[application_id])[
            "page_memberships"
        ]
    )
    assert effective[PAGE1]["kind"] == "unassigned"

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
    command["page_source_sha256"] = "f" * 64
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
    detail = service._admitted_graph(service._store.applications[application_id])
    records = [
        record
        for record in detail["page_memberships"]
        if record["record_kind"] in {"accepted", "unassigned"}
        and record["page"]["source_sha256"] == PAGE1
        and record["status"] == "active"
    ]
    assert len(records) == 1
    membership_audits = [
        event
        for event in service._store.audit_events
        if event["action"] == "page_membership_corrected"
    ]
    assert len(membership_audits) == 1


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
    graph = service._admitted_graph(
        service._store.applications[admitted.application_id]
    )
    memberships = graph.get("page_memberships")
    assert isinstance(memberships, list) and len(memberships) >= 4
    assert {item["record_kind"] for item in memberships} == {
        "candidate",
        "accepted",
        "unassigned",
    }
    # The Evidence event for the field correction carried the same graph.
    event = next(
        event
        for event in service._store.evidence_events
        if event["kind"] == "field_correction"
        and event["application_id"] == admitted.application_id
    )
    assert event["payload"]["graph"]["page_memberships"]


def test_membership_accept_must_reference_a_candidate(tmp_path: Path) -> None:
    """An accepted decision must reference one of the page's coexisting
    candidate claims; accepting an instance/role with no source evidence in the
    application is rejected with no successor."""
    service, application_id, state = _ready_membership(tmp_path)
    ambiguous = next(
        item for item in state["blockers"] if item["rule_id"] == "MEMBERSHIP_AMBIGUOUS"
    )
    accepted = service.correct_page_membership(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=state["work_item_id"],
        expected_fence=state["claimed"]["claim_fence"],
        expected_context=state["work_item"]["command_context"],
        idempotency_key="s10-accept-non-candidate",
        membership=_accept_command(
            ambiguous,
            instance_id="invented_instance_not_in_ledger",
            reason_code="MEMBERSHIP_SOURCE_MISASSIGNED",
        ),
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
