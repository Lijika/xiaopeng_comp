"""S11 application-local entity-link correction over the shared S01/C-DEMO
domain seam.

The Reviewer resolves an unresolved, ambiguous or conflicting entity mention
by appending an explicit accepted link decision (entity id/type/label and
relationship) with the matcher and knowledge-release provenance of the chosen
candidate.  Every prior candidate claim and decision stays immutable and
visible; eligibility for the projection comes only from explicit accepted
facts -- never from candidate confidence, order, count, majority or last
write, and never across application boundaries.
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
SCENARIO = "app_s11_entity_ambiguity.json"
INTEGRATOR = S01CommandPrincipal(
    subject="s11-reviewer",
    role="integrator",
    scope="C-DEMO",
    source_id="s11-test-intake",
)
REVIEWER = S01CommandPrincipal(
    subject=INTEGRATOR.subject,
    role="reviewer",
    scope=INTEGRATOR.scope,
    source_id="s11-review-console",
)
AUDITOR = S01CommandPrincipal(
    subject="s11-auditor",
    role="auditor",
    scope=INTEGRATOR.scope,
    source_id="s11-audit-console",
)

ENTITY_LINK_RULE_IDS = {
    "ENTITY_LINK_UNRESOLVED",
    "ENTITY_LINK_AMBIGUOUS",
    "ENTITY_LINK_CONFLICT",
}
MENTION_ORG = "s11_mention_org_pol"
MENTION_CITY = "s11_mention_city_lease"
MENTION_BRAND = "s11_mention_brand_inv"


def _ready_entity_links(
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
        idempotency_key="s11-intake",
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
    entity_blockers = [
        item
        for item in workspace["mandatory_blockers"]
        if item.get("rule_id") in ENTITY_LINK_RULE_IDS
    ]
    assert len(entity_blockers) >= 3
    return service, str(admitted.application_id), {
        "work_item_id": work_item_id,
        "work_item": work_item,
        "claimed": claimed,
        "blockers": entity_blockers,
        "workspace": workspace,
    }


def _accept_link_command(
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
        and candidate["entity_type"] == entity_type
        and candidate["label"] == label
    )
    provenance = candidate["provenance"]
    return {
        "schema_version": "entity-link-correction/1",
        "finding_id": finding["finding_id"],
        "candidate_claim_id": candidate["claim_id"],
        "mention_id": entity_link["mention_id"],
        "source_evidence": copy.deepcopy(entity_link["source_evidence"]),
        "expected_active_decision_ids": copy.deepcopy(
            entity_link["active_decision_ids"]
        ),
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


def test_entity_link_successor_requires_fresh_current_run(
    tmp_path: Path,
) -> None:
    """A link correction advances Evidence and invalidates the old run; the old
    run stays immutable in history and the route only changes after one fresh
    complete run wins current-run CAS.  A second command against the stale
    (pre-acceptance) context conflicts and creates no second successor."""
    service, application_id, state = _ready_entity_links(tmp_path)
    work_item_id = state["work_item_id"]
    work_item = state["work_item"]
    claimed = state["claimed"]
    ambiguous = next(
        item
        for item in state["blockers"]
        if item["rule_id"] == "ENTITY_LINK_AMBIGUOUS"
    )

    accepted = service.correct_entity_link(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=work_item["command_context"],
        idempotency_key="s11-accept-1",
        entity_link=_accept_link_command(
            ambiguous,
            entity_id="org:picc_full",
            entity_type="insurer",
            label="中国人民财产保险股份有限公司",
        ),
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
    again = service.correct_entity_link(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=work_item["command_context"],
        idempotency_key="s11-accept-2",
        entity_link=_accept_link_command(
            ambiguous,
            entity_id="org:pingan_full",
            entity_type="insurer",
            label="中国平安财产保险股份有限公司",
            reason_code="ENTITY_LINK_AMBIGUITY_RESOLVED",
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
    assert len(history["entity_link_history"]) == 1

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
    # The successor run froze the accepted link decision inside its immutable
    # snapshot; the old run carries no link decision pins.
    assert old_run["entity_link_decisions"] == []
    assert new_run["entity_link_decisions"] == [
        {
            "decision_id": accepted["entity_link_decision_id"],
            "candidate_claim_id": "s11_claim_org_picc",
            "mention_id": MENTION_ORG,
            "entity_id": "org:picc_full",
            "entity_type": "insurer",
            "label": "中国人民财产保险股份有限公司",
            "relationship": "same_as",
            "evidence_revision": 2,
        }
    ]
    ledger = {
        record["decision_id"]: record
        for record in history["entity_links"]
        if record["record_kind"] == "accepted"
    }
    assert ledger[accepted["entity_link_decision_id"]]["status"] == "active"
    assert ledger[accepted["entity_link_decision_id"]]["supersedes"] == []
    assert ledger[accepted["entity_link_decision_id"]]["cycle"] == 1
    assert history["entity_link_history"][0]["cycle"] == 1
    entity_link_audit = next(
        event
        for event in service.audit_timeline(
            principal=AUDITOR,
            application_id=application_id,
        )["events"]
        if event["action"] == "entity_link_corrected"
    )
    assert entity_link_audit["context"]["cycle"] == 1


def test_integrator_entity_link_decisions_have_no_reviewer_authority(
    tmp_path: Path,
) -> None:
    """Integrator input can register candidate claims; Reviewer decisions are
    created only by the controlled correction command."""
    payload = json.loads(
        (ROOT / "fixtures" / "applications" / SCENARIO).read_text(
            encoding="utf-8"
        )
    )
    candidate = payload["graph"]["entity_links"][0]
    payload["graph"]["entity_links"].append(
        {
            "record_kind": "accepted",
            "decision_id": "integrator-accepted",
            "application_id": payload["application_id"],
            "mention": copy.deepcopy(candidate["mention"]),
            "candidate_entity": copy.deepcopy(candidate["candidate_entity"]),
            "relationship": "same_as",
        }
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
        idempotency_key="s11-authority-intake",
        principal=INTEGRATOR,
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=str(admitted.application_id),
    )
    assert history["entity_links"]
    assert {record["record_kind"] for record in history["entity_links"]} == {
        "candidate"
    }
    assert history["entity_link_history"] == []


@pytest.mark.parametrize("invalid_kind", ["malformed", "duplicate"])
def test_invalid_candidate_claim_rejects_admission_without_application(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    payload = json.loads(
        (ROOT / "fixtures" / "applications" / SCENARIO).read_text(
            encoding="utf-8"
        )
    )
    candidates = payload["graph"]["entity_links"]
    if invalid_kind == "malformed":
        candidates[0]["mention"]["mention_id"] = ""
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
        idempotency_key=f"s11-invalid-candidate-{invalid_kind}",
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


def test_entity_link_blockers_surface_every_candidate_without_selection(
    tmp_path: Path,
) -> None:
    """The workspace presents coexisting candidate claims with confidence,
    matcher provenance and frozen knowledge facts without silently selecting a
    winner by type, order, confidence, majority, alias projection or last
    write.  A single low-confidence candidate is unresolved and coexisting
    candidates are ambiguous or conflicting."""
    service, application_id, state = _ready_entity_links(tmp_path)
    blockers = state["blockers"]
    by_mention = {item["entity_link"]["mention_id"]: item for item in blockers}
    assert set(by_mention) == {MENTION_ORG, MENTION_CITY, MENTION_BRAND}
    ambiguous = by_mention[MENTION_ORG]
    assert ambiguous["rule_id"] == "ENTITY_LINK_AMBIGUOUS"
    assert ambiguous["verdict"] == "uncertain"
    assert ambiguous["mandatory"] is True
    assert {
        candidate["entity_id"]
        for candidate in ambiguous["entity_link"]["candidates"]
    } == {"org:picc_full", "org:pingan_full"}
    assert {
        candidate["confidence"]
        for candidate in ambiguous["entity_link"]["candidates"]
    } == {0.97, 0.94}
    assert ambiguous["entity_link"]["low_confidence"] is False
    unresolved = by_mention[MENTION_CITY]
    assert unresolved["rule_id"] == "ENTITY_LINK_UNRESOLVED"
    assert unresolved["entity_link"]["state"] == "unresolved"
    assert unresolved["entity_link"]["low_confidence"] is True
    conflict = by_mention[MENTION_BRAND]
    assert conflict["rule_id"] == "ENTITY_LINK_CONFLICT"
    assert conflict["entity_link"]["state"] == "conflict"
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert {item["record_kind"] for item in history["entity_links"]} == {
        "candidate"
    }
    assert history["entity_link_history"] == []


def test_entity_link_finding_exposes_complete_command_identity(
    tmp_path: Path,
) -> None:
    _, _, state = _ready_entity_links(tmp_path)
    ambiguous = next(
        item
        for item in state["blockers"]
        if item["rule_id"] == "ENTITY_LINK_AMBIGUOUS"
    )["entity_link"]
    assert ambiguous["mention_id"] == MENTION_ORG
    assert ambiguous["mention"]["document_id"] == "pol"
    assert ambiguous["mention"]["field"] == "insured_org"
    assert ambiguous["active_decision_ids"] == []
    assert ambiguous["source_evidence"]["evidence_revision"] == 1
    assert ambiguous["source_evidence"]["event_id"].startswith("evidence_")
    assert {candidate["claim_id"] for candidate in ambiguous["candidates"]} == {
        "s11_claim_org_picc",
        "s11_claim_org_pingan",
    }
    picc = next(
        candidate
        for candidate in ambiguous["candidates"]
        if candidate["entity_id"] == "org:picc_full"
    )
    assert picc["provenance"]["matcher_id"] == "c-demo-entity-matcher/1"
    assert (
        picc["provenance"]["knowledge_release_id"]
        == "c-demo-entity-knowledge/1"
    )
    assert picc["knowledge"]["same_as"] == ["org:picc"]
    assert picc["knowledge"]["conflict_with"] == []


def test_workspace_ledger_exposes_every_candidate_and_provenance(
    tmp_path: Path,
) -> None:
    _, _, state = _ready_entity_links(tmp_path)
    ledger = state["workspace"]["entity_link_ledger"]
    by_mention = {page["mention_id"]: page for page in ledger}
    assert set(by_mention) == {MENTION_ORG, MENTION_CITY, MENTION_BRAND}
    assert by_mention[MENTION_ORG]["state"] == "ambiguous"
    assert by_mention[MENTION_CITY]["state"] == "unresolved"
    assert by_mention[MENTION_CITY]["low_confidence"] is True
    assert by_mention[MENTION_BRAND]["state"] == "conflict"
    assert {
        (
            candidate["claim_id"],
            candidate["provenance"]["method"],
            candidate["provenance"]["source_pointer"],
        )
        for candidate in by_mention[MENTION_ORG]["candidates"]
    } == {
        (
            "s11_claim_org_picc",
            "alias-longest-key",
            "/documents/1/fields/insured_org",
        ),
        (
            "s11_claim_org_pingan",
            "alias-fuzzy",
            "/documents/1/fields/insured_org",
        ),
    }
    assert all(page["decisions"] == [] for page in ledger)
    assert all(page["finding_id"] for page in ledger)


@pytest.mark.parametrize(
    ("field", "value", "reason_code"),
    [
        (
            "mention_id",
            "mention_from_another_application",
            "ENTITY_LINK_MENTION_OUTSIDE_APPLICATION",
        ),
        ("candidate_claim_id", "missing_claim", "ENTITY_LINK_CLAIM_MISMATCH"),
        (
            "source_evidence",
            {"event_id": "evidence_stale", "evidence_revision": 1},
            "STALE_ENTITY_LINK_SOURCE_EVIDENCE",
        ),
        (
            "expected_active_decision_ids",
            ["decision_stale"],
            "STALE_ENTITY_LINK_PREDECESSORS",
        ),
        (
            "matcher_id",
            "c-demo-entity-matcher/expired",
            "ENTITY_LINK_RELEASE_MISMATCH",
        ),
        (
            "knowledge_release_id",
            "c-demo-entity-knowledge/expired",
            "ENTITY_LINK_RELEASE_MISMATCH",
        ),
    ],
)
def test_entity_link_identity_conflicts_have_zero_public_effects(
    tmp_path: Path,
    field: str,
    value: object,
    reason_code: str,
) -> None:
    """A cross-application mention, a wrong claim, stale evidence/predecessors
    and expired or wrong matcher/knowledge releases reject or stale the
    command with zero public business effect."""
    service, application_id, state = _ready_entity_links(tmp_path)
    finding = next(
        item
        for item in state["blockers"]
        if item["rule_id"] == "ENTITY_LINK_AMBIGUOUS"
    )
    command = _accept_link_command(
        finding,
        entity_id="org:picc_full",
        entity_type="insurer",
        label="中国人民财产保险股份有限公司",
    )
    command[field] = value
    route_before = service.current_route_view(
        principal=REVIEWER, application_id=application_id
    )
    audit_before = service.audit_timeline(
        principal=AUDITOR, application_id=application_id
    )

    result = service.correct_entity_link(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=state["work_item_id"],
        expected_fence=state["claimed"]["claim_fence"],
        expected_context=state["work_item"]["command_context"],
        idempotency_key=f"s11-conflict-{field}",
        entity_link=command,
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
    )["entity_link_history"] == []


def test_entity_link_critical_low_confidence_never_auto_links(
    tmp_path: Path,
) -> None:
    """A critical-identity low-confidence candidate remains a reviewable
    blocker; the checker projection never auto-links it as equivalence."""
    service, application_id, state = _ready_entity_links(tmp_path)
    unresolved = next(
        item
        for item in state["blockers"]
        if item["rule_id"] == "ENTITY_LINK_UNRESOLVED"
    )
    entity_link = unresolved["entity_link"]
    assert entity_link["low_confidence"] is True
    assert entity_link["state"] == "unresolved"
    assert len(entity_link["candidates"]) == 1
    assert entity_link["candidates"][0]["confidence"] < 0.6
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    old_run = next(run for run in history["runs"] if run["current"])
    assert old_run["entity_link_decisions"] == []
    # The mention stays a blocker; nothing is selected without the Reviewer.
    assert unresolved["mandatory"] is True


def test_entity_link_no_brand_collapse_conflict_preserves_candidates(
    tmp_path: Path,
) -> None:
    """Coexisting candidates that the frozen knowledge release marks
    ``conflict_with`` each other surface as ENTITY_LINK_CONFLICT; both
    candidates stay visible and no single brand is auto-selected."""
    _, _, state = _ready_entity_links(tmp_path)
    conflict = next(
        item
        for item in state["blockers"]
        if item["rule_id"] == "ENTITY_LINK_CONFLICT"
    )["entity_link"]
    assert conflict["mention_id"] == MENTION_BRAND
    assert {
        candidate["entity_id"] for candidate in conflict["candidates"]
    } == {"brand:faw-vw", "brand:saic-vw"}
    by_id = {
        candidate["entity_id"]: candidate
        for candidate in conflict["candidates"]
    }
    assert by_id["brand:faw-vw"]["knowledge"]["conflict_with"] == [
        "brand:saic-vw"
    ]
    assert by_id["brand:saic-vw"]["knowledge"]["conflict_with"] == [
        "brand:faw-vw"
    ]


def test_entity_link_cross_city_prohibition_is_conflict(
    tmp_path: Path,
) -> None:
    """A plate-prefix mention with candidates from two cities (cross-city
    prohibition) is an explicit conflict, never an inferred winner."""
    payload = json.loads(
        (ROOT / "fixtures" / "applications" / SCENARIO).read_text(
            encoding="utf-8"
        )
    )
    payload["graph"]["entity_links"].extend(
        [
            {
                "record_kind": "candidate",
                "claim_id": "s11_claim_plate_su_a",
                "application_id": payload["application_id"],
                "mention": {
                    "mention_id": "s11_mention_plate",
                    "entity_type": "city",
                    "document_id": "reg",
                    "document_role": "机动车登记证书",
                    "field": "plate_no",
                    "raw": "苏A",
                },
                "candidate_entity": {
                    "entity_id": "addr:nanjing",
                    "entity_type": "city",
                    "label": "江苏南京",
                },
                "confidence": 0.99,
                "provenance": {
                    "matcher_id": "c-demo-entity-matcher/1",
                    "matcher_version": "1",
                    "knowledge_release_id": "c-demo-entity-knowledge/1",
                    "method": "alias-longest-key",
                    "source_pointer": "/documents/0/fields/plate_no",
                },
                "knowledge": {
                    "same_as": [],
                    "conflict_with": ["addr:suzhou"],
                },
            },
            {
                "record_kind": "candidate",
                "claim_id": "s11_claim_plate_su_b",
                "application_id": payload["application_id"],
                "mention": {
                    "mention_id": "s11_mention_plate",
                    "entity_type": "city",
                    "document_id": "reg",
                    "document_role": "机动车登记证书",
                    "field": "plate_no",
                    "raw": "苏A",
                },
                "candidate_entity": {
                    "entity_id": "addr:suzhou",
                    "entity_type": "city",
                    "label": "江苏苏州",
                },
                "confidence": 0.95,
                "provenance": {
                    "matcher_id": "c-demo-entity-matcher/1",
                    "matcher_version": "1",
                    "knowledge_release_id": "c-demo-entity-knowledge/1",
                    "method": "alias-fuzzy",
                    "source_pointer": "/documents/0/fields/plate_no",
                },
                "knowledge": {
                    "same_as": [],
                    "conflict_with": ["addr:nanjing"],
                },
            },
        ]
    )
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / SCENARIO).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    service, application_id, state = _ready_entity_links(
        tmp_path, fixture_root=fixture_root
    )
    plate = next(
        item
        for item in state["blockers"]
        if item["entity_link"]["mention_id"] == "s11_mention_plate"
    )
    assert plate["rule_id"] == "ENTITY_LINK_CONFLICT"
    assert {
        candidate["entity_id"]
        for candidate in plate["entity_link"]["candidates"]
    } == {"addr:nanjing", "addr:suzhou"}


def test_entity_link_priority_never_selects(tmp_path: Path) -> None:
    """Migration differential: the longest-key alias candidate with the
    highest confidence is never auto-selected; priority, order and confidence
    are reviewable facts only, and the mention stays an explicit blocker."""
    service, application_id, state = _ready_entity_links(tmp_path)
    ambiguous = next(
        item
        for item in state["blockers"]
        if item["rule_id"] == "ENTITY_LINK_AMBIGUOUS"
    )
    entity_link = ambiguous["entity_link"]
    by_method = {
        candidate["provenance"]["method"]: candidate
        for candidate in entity_link["candidates"]
    }
    assert set(by_method) == {"alias-longest-key", "alias-fuzzy"}
    assert (
        by_method["alias-longest-key"]["confidence"]
        > by_method["alias-fuzzy"]["confidence"]
    )
    assert entity_link["state"] == "ambiguous"
    assert entity_link["active_decision_ids"] == []
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    run = next(run for run in history["runs"] if run["current"])
    assert run["entity_link_decisions"] == []


def test_entity_link_same_as_cycle_stays_explicit_review(
    tmp_path: Path,
) -> None:
    """A same-as cycle (A same_as B and B same_as A in the frozen knowledge
    release) is cyclic knowledge: it stays an explicit review blocker and is
    never silently collapsed into an equivalence."""
    payload = json.loads(
        (ROOT / "fixtures" / "applications" / SCENARIO).read_text(
            encoding="utf-8"
        )
    )
    links = payload["graph"]["entity_links"]
    picc = next(
        record for record in links if record["claim_id"] == "s11_claim_org_picc"
    )
    pingan = next(
        record
        for record in links
        if record["claim_id"] == "s11_claim_org_pingan"
    )
    picc["knowledge"]["same_as"] = ["org:picc", "org:pingan_full"]
    pingan["knowledge"]["same_as"] = ["org:pingan", "org:picc_full"]
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / SCENARIO).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    service, application_id, state = _ready_entity_links(
        tmp_path, fixture_root=fixture_root
    )
    ambiguous = next(
        item
        for item in state["blockers"]
        if item["rule_id"] == "ENTITY_LINK_AMBIGUOUS"
    )
    entity_link = ambiguous["entity_link"]
    assert entity_link["mention_id"] == MENTION_ORG
    assert entity_link["state"] == "ambiguous"
    assert entity_link["active_decision_ids"] == []
    by_id = {
        candidate["entity_id"]: candidate
        for candidate in entity_link["candidates"]
    }
    assert "org:pingan_full" in by_id["org:picc_full"]["knowledge"]["same_as"]
    assert "org:picc_full" in by_id["org:pingan_full"]["knowledge"]["same_as"]
    assert len(entity_link["candidates"]) == 2
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    run = next(run for run in history["runs"] if run["current"])
    assert run["entity_link_decisions"] == []


def test_entity_link_idempotency_replays_one_successor(tmp_path: Path) -> None:
    """Repeated idempotency key returns the original result and leaves one
    successor, one invalidation, one audit fact and one outbox event."""
    service, application_id, state = _ready_entity_links(tmp_path)
    ambiguous = next(
        item
        for item in state["blockers"]
        if item["rule_id"] == "ENTITY_LINK_AMBIGUOUS"
    )
    command = _accept_link_command(
        ambiguous,
        entity_id="org:picc_full",
        entity_type="insurer",
        label="中国人民财产保险股份有限公司",
    )
    first = service.correct_entity_link(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=state["work_item_id"],
        expected_fence=state["claimed"]["claim_fence"],
        expected_context=state["work_item"]["command_context"],
        idempotency_key="s11-same-key",
        entity_link=command,
        now=101,
    )
    second = service.correct_entity_link(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=state["work_item_id"],
        expected_fence=state["claimed"]["claim_fence"],
        expected_context=state["work_item"]["command_context"],
        idempotency_key="s11-same-key",
        entity_link=command,
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
        for record in history["entity_links"]
        if record["record_kind"] == "accepted"
        and record["mention"]["mention_id"] == MENTION_ORG
        and record["status"] == "active"
    ]
    assert len(records) == 1
    entity_link_audits = [
        event
        for event in service.audit_timeline(
            principal=AUDITOR,
            application_id=application_id,
        )["events"]
        if event["action"] == "entity_link_corrected"
    ]
    assert len(entity_link_audits) == 1


@pytest.mark.parametrize(
    "fault_point",
    (
        "entity_link.evidence",
        "entity_link.lifecycle",
        "entity_link.work_item",
        "entity_link.job",
        "entity_link.outbox",
        "entity_link.audit",
        "entity_link.idempotency",
        "entity_link.publish",
    ),
)
def test_each_entity_link_write_fault_has_zero_public_effect(
    tmp_path: Path,
    fault_point: str,
) -> None:
    service, application_id, state = _ready_entity_links(tmp_path)
    ambiguous = next(
        item
        for item in state["blockers"]
        if item["rule_id"] == "ENTITY_LINK_AMBIGUOUS"
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
            raise OSError("injected entity link write failure")

    faulty = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        scenario_id=SCENARIO,
        fault_injector=fail,
    )
    failed = faulty.correct_entity_link(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=state["work_item_id"],
        expected_fence=state["claimed"]["claim_fence"],
        expected_context=state["work_item"]["command_context"],
        idempotency_key=f"s11-fault-{fault_point}",
        entity_link=_accept_link_command(
            ambiguous,
            entity_id="org:picc_full",
            entity_type="insurer",
            label="中国人民财产保险股份有限公司",
        ),
        now=101,
    )

    assert failed["status"] == "unavailable"
    assert failed["reason_code"] == (
        "AUDIT_UNAVAILABLE"
        if fault_point == "entity_link.audit"
        else "STORAGE_UNAVAILABLE"
    )
    assert observe() == before


def test_entity_link_requires_finding_in_current_run(tmp_path: Path) -> None:
    """A correction must reference a mandatory entity-link finding of the
    current run; a stale finding (other rule) is not correctable."""
    service, application_id, state = _ready_entity_links(tmp_path)
    ambiguous = next(
        item
        for item in state["blockers"]
        if item["rule_id"] == "ENTITY_LINK_AMBIGUOUS"
    )
    command = _accept_link_command(
        ambiguous,
        entity_id="org:picc_full",
        entity_type="insurer",
        label="中国人民财产保险股份有限公司",
    )
    command["finding_id"] = "finding_missing"
    with pytest.raises(ValueError):
        service.correct_entity_link(
            principal=REVIEWER,
            application_id=application_id,
            work_item_id=state["work_item_id"],
            expected_fence=state["claimed"]["claim_fence"],
            expected_context=state["work_item"]["command_context"],
            idempotency_key="s11-bad-finding",
            entity_link=command,
            now=101,
        )


def test_entity_link_accept_must_reference_a_candidate(tmp_path: Path) -> None:
    """An accepted decision must reference one of the mention's coexisting
    candidate claims; accepting an entity with no candidate claim is rejected
    with no successor."""
    service, application_id, state = _ready_entity_links(tmp_path)
    ambiguous = next(
        item
        for item in state["blockers"]
        if item["rule_id"] == "ENTITY_LINK_AMBIGUOUS"
    )
    command = _accept_link_command(
        ambiguous,
        entity_id="org:picc_full",
        entity_type="insurer",
        label="中国人民财产保险股份有限公司",
        reason_code="ENTITY_LINK_AMBIGUITY_RESOLVED",
    )
    command["entity_id"] = "invented_entity_not_in_ledger"
    accepted = service.correct_entity_link(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=state["work_item_id"],
        expected_fence=state["claimed"]["claim_fence"],
        expected_context=state["work_item"]["command_context"],
        idempotency_key="s11-accept-non-candidate",
        entity_link=command,
        now=101,
    )
    assert accepted["status"] == "rejected"
    assert accepted["reason_code"] == "ENTITY_LINK_ACCEPT_NOT_CANDIDATE"
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert history["entity_link_history"] == []


def test_entity_link_reason_and_release_are_closed(tmp_path: Path) -> None:
    """An unregistered reason code, an unknown relationship or a command with
    extra keys is a validation conflict with no successor."""
    service, application_id, state = _ready_entity_links(tmp_path)
    ambiguous = next(
        item
        for item in state["blockers"]
        if item["rule_id"] == "ENTITY_LINK_AMBIGUOUS"
    )
    with pytest.raises(ValueError):
        service.correct_entity_link(
            principal=REVIEWER,
            application_id=application_id,
            work_item_id=state["work_item_id"],
            expected_fence=state["claimed"]["claim_fence"],
            expected_context=state["work_item"]["command_context"],
            idempotency_key="s11-bad-reason",
            entity_link=_accept_link_command(
                ambiguous,
                entity_id="org:picc_full",
                entity_type="insurer",
                label="中国人民财产保险股份有限公司",
                reason_code="ENTITY_LINK_PAGE_UNASSIGNED",
            ),
            now=101,
        )
    command = _accept_link_command(
        ambiguous,
        entity_id="org:picc_full",
        entity_type="insurer",
        label="中国人民财产保险股份有限公司",
    )
    command["relationship"] = "part_of"
    with pytest.raises(ValueError):
        service.correct_entity_link(
            principal=REVIEWER,
            application_id=application_id,
            work_item_id=state["work_item_id"],
            expected_fence=state["claimed"]["claim_fence"],
            expected_context=state["work_item"]["command_context"],
            idempotency_key="s11-bad-relationship",
            entity_link=command,
            now=101,
        )
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert history["entity_link_history"] == []


def test_entity_link_successor_supersedes_active_predecessor(
    tmp_path: Path,
) -> None:
    """A later accepted decision for the same mention supersedes the active
    predecessor; both facts stay immutable in history."""
    service, application_id, state = _ready_entity_links(tmp_path)
    ambiguous = next(
        item
        for item in state["blockers"]
        if item["rule_id"] == "ENTITY_LINK_AMBIGUOUS"
    )
    first = service.correct_entity_link(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=state["work_item_id"],
        expected_fence=state["claimed"]["claim_fence"],
        expected_context=state["work_item"]["command_context"],
        idempotency_key="s11-first-decision",
        entity_link=_accept_link_command(
            ambiguous,
            entity_id="org:picc_full",
            entity_type="insurer",
            label="中国人民财产保险股份有限公司",
        ),
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
    mention = next(
        item
        for item in workspace["entity_link_ledger"]
        if item["mention_id"] == MENTION_ORG
    )
    assert mention["state"] == "selected"
    assert mention["finding_id"] == ambiguous["finding_id"]
    assert mention["active_decision_ids"] == [first["entity_link_decision_id"]]
    assert mention["source_evidence"]["evidence_revision"] == 2

    successor = service.correct_entity_link(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=successor_work_id,
        expected_fence=successor_claim["claim_fence"],
        expected_context=successor_work["command_context"],
        idempotency_key="s11-successor-decision",
        entity_link=_accept_link_command(
            {"finding_id": mention["finding_id"], "entity_link": mention},
            entity_id="org:pingan_full",
            entity_type="insurer",
            label="中国平安财产保险股份有限公司",
            reason_code="ENTITY_LINK_SOURCE_MISASSIGNED",
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
        for item in history["entity_links"]
        if item["record_kind"] == "accepted"
    }
    assert decisions[first["entity_link_decision_id"]]["status"] == "superseded"
    assert decisions[successor["entity_link_decision_id"]]["status"] == "active"
    assert decisions[successor["entity_link_decision_id"]]["supersedes"] == [
        first["entity_link_decision_id"]
    ]
    assert len(
        [
            item
            for item in history["entity_links"]
            if item["record_kind"] == "candidate"
        ]
    ) == 5


def test_field_correction_preserves_entity_link_ledger(tmp_path: Path) -> None:
    """An S04 field-correction successor must carry the admitted graph,
    including the S11 entity-link ledger, or it would erase link facts.
    (M evidence: ``graph.entity_links`` survives every Evidence successor.)"""
    payload = json.loads(
        (ROOT / "fixtures" / "applications" / SCENARIO).read_text(
            encoding="utf-8"
        )
    )
    # Add a cross-document engine mismatch so a field finding exists too.
    payload["documents"][1]["fields"]["engine_no"] = {
        "raw": "WRONG-ENGINE",
        "confidence": 0.99,
    }
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
        idempotency_key="s11-field-intake",
        principal=INTEGRATOR,
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    assert service.process_next_job().status == "complete"
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=100,
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
    links_before = service.application_history_view(
        principal=REVIEWER,
        application_id=admitted.application_id,
    )["entity_links"]
    accepted = service.correct_field_observation(
        principal=REVIEWER,
        application_id=admitted.application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=work_item["command_context"],
        idempotency_key="s11-field-correction",
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
    links_after = service.application_history_view(
        principal=REVIEWER,
        application_id=admitted.application_id,
    )["entity_links"]
    assert links_after == links_before
    assert len(links_after) == 5
    assert {item["record_kind"] for item in links_after} == {"candidate"}


def test_entity_link_late_terminal_cycle_edit_is_rejected(
    tmp_path: Path,
) -> None:
    """After every blocker is resolved and the application reaches automatic
    completion, a late entity-link edit against the sealed cycle is rejected
    with a stable code and never changes the current route."""
    service, application_id, state = _ready_entity_links(tmp_path)

    def _resolve(
        work_item_id: str,
        expected_context: dict[str, object],
        claimed: dict[str, object],
        blocker: dict[str, object],
        *,
        entity_id: str,
        entity_type: str,
        label: str,
        key: str,
        now: int,
    ) -> None:
        accepted = service.correct_entity_link(
            principal=REVIEWER,
            application_id=application_id,
            work_item_id=work_item_id,
            expected_fence=claimed["claim_fence"],
            expected_context=expected_context,
            idempotency_key=key,
            entity_link=_accept_link_command(
                blocker,
                entity_id=entity_id,
                entity_type=entity_type,
                label=label,
                reason_code="ENTITY_LINK_AMBIGUITY_RESOLVED",
            ),
            now=now,
        )
        assert accepted["status"] == "accepted"
        assert service.process_next_job().status == "complete"
        service.refresh_projection()

    _resolve(
        state["work_item_id"],
        state["work_item"]["command_context"],
        state["claimed"],
        next(
            item
            for item in state["blockers"]
            if item["rule_id"] == "ENTITY_LINK_AMBIGUOUS"
        ),
        entity_id="org:picc_full",
        entity_type="insurer",
        label="中国人民财产保险股份有限公司",
        key="s11-accept",
        now=101,
    )
    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=102,
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
        if item["rule_id"] == "ENTITY_LINK_UNRESOLVED"
    )
    _resolve(
        work_item2_id,
        work_item2["command_context"],
        claimed2,
        unresolved,
        entity_id="addr:nanjing",
        entity_type="city",
        label="南京市",
        key="s11-city-accept",
        now=103,
    )
    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=104,
    )
    work_item3_id = queue["items"][0]["work_item_id"]
    work_item3 = service.review_work_item_view(
        principal=REVIEWER, work_item_id=work_item3_id, now=104
    )
    claimed3 = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item3_id,
        expected_context=work_item3["command_context"],
        now=104,
    )
    workspace3 = service.workspace_view(
        application_id,
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=104,
    )
    conflict = next(
        item
        for item in workspace3["mandatory_blockers"]
        if item["rule_id"] == "ENTITY_LINK_CONFLICT"
    )
    _resolve(
        work_item3_id,
        work_item3["command_context"],
        claimed3,
        conflict,
        entity_id="brand:faw-vw",
        entity_type="brand",
        label="一汽-大众汽车有限公司",
        key="s11-brand-accept",
        now=105,
    )
    current = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert current["route"] == "auto_complete"

    # A late edit against the last (completed) work item of the sealed cycle
    # is rejected with a stable code and leaves the current route unchanged.
    late = service.correct_entity_link(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item3_id,
        expected_fence=claimed3["claim_fence"],
        expected_context=work_item3["command_context"],
        idempotency_key="s11-late-terminal",
        entity_link=_accept_link_command(
            conflict,
            entity_id="brand:saic-vw",
            entity_type="brand",
            label="上汽大众汽车有限公司",
            reason_code="ENTITY_LINK_SOURCE_MISASSIGNED",
        ),
        now=106,
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
    assert len(history["entity_link_history"]) == 3
