from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading

import pytest

from task4_consistency.controlled.s01 import (
    AdmissionDisposition,
    ControlledScenarioService,
    ControlledScenarioTestDriver,
    QueryNotFound,
    S01CommandPrincipal,
)
from task4_consistency.controlled.s02 import ControlledObject
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules
from tests.test_s03_controlled import ready_review_work_item, review_batch_items


ROOT = Path(__file__).resolve().parents[1]
INTEGRATOR = S01CommandPrincipal(
    subject="s04-reviewer",
    role="integrator",
    scope="C-DEMO",
    source_id="s04-test-intake",
)
REVIEWER = S01CommandPrincipal(
    subject=INTEGRATOR.subject,
    role="reviewer",
    scope=INTEGRATOR.scope,
    source_id="s04-review-console",
)


def _ready_engine_correction(
    tmp_path: Path,
    *,
    fixture_root: Path = ROOT / "fixtures" / "applications",
) -> tuple[ControlledScenarioService, str, str, dict[str, object], dict[str, object]]:
    service = ControlledScenarioService(
        fixture_root=fixture_root,
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
    )
    admitted = service.submit_demo(
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s04-engine-intake",
        principal=INTEGRATOR,
    )
    completed = service.process_next_job()
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=100,
    )

    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    assert admitted.application_id is not None
    assert completed.status == "complete"
    assert len(queue["items"]) == 1
    application_id = admitted.application_id
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
    workspace = service.workspace_view(
        application_id,
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=100,
    )
    finding = next(
        item
        for item in workspace["mandatory_blockers"]
        if item["rule_id"] == "R_ENGINE_CROSS"
    )
    source = next(
        link for link in finding["evidence_links"] if link["document_id"] == "inv"
    )
    return service, application_id, work_item_id, claimed, {
        "schema_version": "field-observation-correction/1",
        "finding_id": finding["finding_id"],
        "observation_id": source["observation_id"],
        "document_id": source["document_id"],
        "document_role": source["document_role"],
        "field": source["field"],
        "raw": "S2ENG54A",
        "source_location": {
            "source_sha256": source["source_sha256"],
            "source_page": 4,
            "source_region": "region:1",
        },
        "reason_code": "SOURCE_VALUE_MISREAD",
    }


def test_source_backed_correction_appends_successor_and_invalidates_old_context(
    tmp_path: Path,
) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )

    accepted = service.correct_field_observation(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=before["command_context"],
        idempotency_key="s04-engine-correction",
        correction=correction,
        now=101,
    )
    invalidated = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=101,
    )

    assert accepted == {
        "status": "accepted",
        "replayed": False,
        "application_id": application_id,
        "work_item_id": work_item_id,
        "correction_id": accepted["correction_id"],
        "observation_id": accepted["observation_id"],
        "invalidated_run_id": before["run_authority"]["run_id"],
        "job_id": accepted["job_id"],
        "phase": "Assembly",
        "route": "pending_check",
        "lifecycle_revision": before["lifecycle_revision"] + 1,
        "evidence_revision": before["evidence_revision"] + 1,
    }
    assert accepted["observation_id"] != correction["observation_id"]
    assert invalidated["status"] == "invalidated"
    assert invalidated["run_authority"]["run_id"] == accepted["invalidated_run_id"]


def test_correction_window_queries_hide_invalidated_work_after_checker_failure(
    tmp_path: Path,
) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    accepted = service.correct_field_observation(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=before["command_context"],
        idempotency_key="s04-queryable-correction-window",
        correction=correction,
        now=101,
    )

    def assert_pending_non_actionable(current: ControlledScenarioService) -> None:
        assert current.queue_view(
            role="reviewer",
            scope=REVIEWER.scope,
            subject=REVIEWER.subject,
            now=102,
        ) == {"items": [], "recovery_items": [], "projection_watermark": 0}
        with pytest.raises(QueryNotFound):
            current.workspace_view(
                application_id,
                role="reviewer",
                scope=REVIEWER.scope,
                subject=REVIEWER.subject,
                now=102,
            )
        route = current.current_route_view(
            principal=REVIEWER,
            application_id=application_id,
        )
        assert route["phase"] in {"Assembly", "Checking"}
        assert route["route"] == "pending_check"
        assert route["current_run_id"] is None
        assert current.review_work_item_view(
            principal=REVIEWER,
            work_item_id=work_item_id,
            now=102,
        )["status"] == "invalidated"

    assert accepted["status"] == "accepted"
    assert_pending_non_actionable(service)

    def broken_checker(application: object) -> object:
        raise RuntimeError("successor checker is unavailable")

    failed_worker = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        checker_runner=broken_checker,
        clock=lambda: 102,
    )
    failed = failed_worker.process_next_job()

    assert failed.status == "failed"
    assert_pending_non_actionable(failed_worker)


def test_multi_blocker_region_identity_matches_the_workspace_correction_gate(
    tmp_path: Path,
) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    scenario = json.loads(
        (ROOT / "fixtures" / "applications" / "app_s04_bad_vin.json").read_text(
            encoding="utf-8"
        )
    )
    invoice = next(
        document for document in scenario["documents"] if document["doc_id"] == "inv"
    )
    invoice["fields"]["engine_no"] = {
        "raw": "S2ENG54X",
        "source_text": "S2ENG54A",
        "confidence": 0.99,
    }
    scenario["expected_verdicts"]["R_ENGINE_CROSS"] = "inconsistent"
    (fixture_root / "app_s04_bad_vin.json").write_text(
        json.dumps(scenario, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    service = ControlledScenarioService(
        fixture_root=fixture_root,
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        scenario_id="app_s04_bad_vin.json",
    )
    admitted = service.submit_demo(
        scenario_id="app_s04_bad_vin.json",
        idempotency_key="s04-multi-blocker-intake",
        principal=INTEGRATOR,
    )
    completed = service.process_next_job()
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=100,
    )
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
    workspace = service.workspace_view(
        admitted.application_id or "",
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=100,
    )
    vin = next(
        finding
        for finding in workspace["mandatory_blockers"]
        if finding["rule_id"] == "R_VIN_CROSS"
    )
    engine = next(
        finding
        for finding in workspace["mandatory_blockers"]
        if finding["rule_id"] == "R_ENGINE_CROSS"
    )
    vin_source = next(
        link for link in vin["evidence_links"] if link["document_id"] == "inv"
    )
    engine_source = next(
        link for link in engine["evidence_links"] if link["document_id"] == "inv"
    )

    assert completed.status == "complete"
    assert len(workspace["mandatory_blockers"]) == 2
    assert engine_source["source_sha256"] == vin_source["source_sha256"]
    assert engine_source["source_page"] == vin_source["source_page"]
    assert engine_source["source_region"] != vin_source["source_region"]

    corrected = service.correct_field_observation(
        principal=REVIEWER,
        application_id=admitted.application_id or "",
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=work_item["command_context"],
        idempotency_key="s04-multi-blocker-correction",
        correction={
            "schema_version": "field-observation-correction/1",
            "finding_id": engine["finding_id"],
            "observation_id": engine_source["observation_id"],
            "document_id": engine_source["document_id"],
            "document_role": engine_source["document_role"],
            "field": engine_source["field"],
            "raw": "S2ENG54A",
            "source_location": {
                "source_sha256": engine_source["source_sha256"],
                "source_page": engine_source["source_page"],
                "source_region": engine_source["source_region"],
            },
            "reason_code": "SOURCE_VALUE_MISREAD",
        },
        now=101,
    )

    assert corrected["status"] == "accepted"


def test_late_single_and_batch_review_submits_have_zero_effect_after_correction(
    tmp_path: Path,
) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    verification = {
        "schema_version": "human-decision/1",
        "outcome": "confirmed",
        "reason_code": "HUMAN_REVIEW_COMPLETED",
        "finding_decisions": [
            {
                "finding_id": finding["finding_id"],
                "outcome": (
                    "confirmed"
                    if finding["verdict"] == "uncertain"
                    else "inconclusive"
                ),
            }
            for finding in before["automatic_findings"]
        ],
    }
    batch_plan = service.preview_review_work_item_batch(
        principal=REVIEWER,
        items=review_batch_items(
            before,
            expected_fence=claimed["claim_fence"],
        ),
        now=100,
    )
    accepted = service.correct_field_observation(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=before["command_context"],
        idempotency_key="s04-invalidate-late-review",
        correction=correction,
        now=101,
    )
    auditor = S01CommandPrincipal(
        subject="s04-auditor",
        role="auditor",
        scope=REVIEWER.scope,
        source_id="s04-audit-console",
    )

    def public_state() -> dict[str, object]:
        return {
            "history": service.application_history_view(
                principal=REVIEWER,
                application_id=application_id,
            ),
            "route": service.current_route_view(
                principal=REVIEWER,
                application_id=application_id,
            ),
            "work_item": service.review_work_item_view(
                principal=REVIEWER,
                work_item_id=work_item_id,
                now=102,
            ),
            "audit": service.audit_timeline(
                principal=auditor,
                application_id=application_id,
            ),
        }

    state_after_correction = public_state()
    late_single = service.submit_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=before["command_context"],
        idempotency_key="s04-late-single-submit",
        verification=verification,
        now=102,
    )
    late_batch = service.submit_review_work_item_batch(
        principal=REVIEWER,
        idempotency_key="s04-late-batch-submit",
        plan=batch_plan,
        now=102,
    )

    assert accepted["status"] == "accepted"
    assert late_single == {
        "status": "stale",
        "replayed": False,
        "application_id": application_id,
        "work_item_id": work_item_id,
        "reason_code": "STALE_REVIEW_CONTEXT",
    }
    assert late_batch == {
        "status": "stale",
        "replayed": False,
        "application_id": application_id,
        "work_item_id": work_item_id,
        "reason_code": "STALE_REVIEW_CONTEXT",
    }
    assert public_state() == state_after_correction


def test_inconsistent_successor_creates_fresh_work_for_a_second_correction(
    tmp_path: Path,
) -> None:
    service, application_id, old_work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    old_work_item = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=old_work_item_id,
        now=100,
    )
    first = service.correct_field_observation(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=old_work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=old_work_item["command_context"],
        idempotency_key="s04-still-inconsistent",
        correction={**correction, "raw": "S2ENG54Z"},
        now=101,
    )
    first_successor = service.process_next_job()
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=103,
    )
    fresh_item = queue["items"][0]
    fresh_work_item_id = fresh_item["work_item_id"]
    fresh_work_item = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=fresh_work_item_id,
        now=103,
    )
    fresh_claim = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=fresh_work_item_id,
        expected_context=fresh_work_item["command_context"],
        now=103,
    )
    fresh_workspace = service.workspace_view(
        application_id,
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=103,
    )
    fresh_finding = next(
        finding
        for finding in fresh_workspace["mandatory_blockers"]
        if finding["rule_id"] == "R_ENGINE_CROSS"
    )
    fresh_source = next(
        link
        for link in fresh_finding["evidence_links"]
        if link["document_id"] == "inv"
    )
    second = service.correct_field_observation(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=fresh_work_item_id,
        expected_fence=fresh_claim["claim_fence"],
        expected_context=fresh_work_item["command_context"],
        idempotency_key="s04-second-correction",
        correction={
            "schema_version": "field-observation-correction/1",
            "finding_id": fresh_finding["finding_id"],
            "observation_id": fresh_source["observation_id"],
            "document_id": fresh_source["document_id"],
            "document_role": fresh_source["document_role"],
            "field": fresh_source["field"],
            "raw": "S2ENG54A",
            "source_location": {
                "source_sha256": fresh_source["source_sha256"],
                "source_page": fresh_source["source_page"],
                "source_region": fresh_source["source_region"],
            },
            "reason_code": "SOURCE_VALUE_MISREAD",
        },
        now=104,
    )
    final = service.process_next_job()
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    current = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    assert first["status"] == "accepted"
    assert first_successor.status == "complete"
    assert fresh_item["route"] == "manual_review"
    assert fresh_work_item_id != old_work_item_id
    assert fresh_work_item["run_authority"]["run_id"] == first_successor.run_id
    assert fresh_source["observation_id"] == first["observation_id"]
    assert second["status"] == "accepted"
    assert second["observation_id"] != first["observation_id"]
    assert final.status == "complete"
    assert current["route"] == "auto_complete"
    assert current["current_run_id"] == final.run_id
    assert len(history["corrections"]) == 2
    assert history["corrections"][1]["superseded_observation_id"] == first[
        "observation_id"
    ]
    assert sum(run["current"] for run in history["runs"]) == 1


def test_safe_rerun_keeps_old_run_non_current_and_publishes_successor_route(
    tmp_path: Path,
) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    accepted = service.correct_field_observation(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=before["command_context"],
        idempotency_key="s04-safe-rerun",
        correction=correction,
        now=101,
    )

    pending = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    completed = service.process_next_job()
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    current = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    assert pending["route"] == "pending_check"
    assert pending["current_run_id"] is None
    assert completed.status == "complete"
    assert completed.run_id != accepted["invalidated_run_id"]
    assert completed.evidence_revision == accepted["evidence_revision"]
    old_run, new_run = history["runs"]
    assert current == {
        "schema_version": "s04-current-route/1",
        "application_id": application_id,
        "phase": "Verification Completed",
        "route": "auto_complete",
        "current_run_id": completed.run_id,
        "cycle": 1,
        "lifecycle_revision": completed.lifecycle_revision,
        "evidence_revision": completed.evidence_revision,
        "evidence_snapshot_id": completed.evidence_snapshot_id,
        "evidence_snapshot_digest": new_run["evidence_snapshot_digest"],
        "release_id": completed.release_id,
        "release_digest": new_run["release_digest"],
        "checker_build": completed.checker_build,
        "currentness_reason": "CURRENT_CONTEXT_MATCH",
    }
    assert [run["run_id"] for run in history["runs"]] == [
        accepted["invalidated_run_id"],
        completed.run_id,
    ]
    assert old_run["status"] == new_run["status"] == "complete"
    assert old_run["current"] is False
    assert old_run["currentness_reason"] == "EVIDENCE_CORRECTION_ACCEPTED"
    assert old_run["authority_digest"] == before["run_authority"]["authority_digest"]
    assert old_run["evidence_revision"] == 1
    assert correction["finding_id"] in old_run["finding_ids"]
    assert new_run["current"] is True
    assert new_run["currentness_reason"] == "CURRENT_CONTEXT_MATCH"
    assert new_run["evidence_revision"] == 2
    assert new_run["evidence_snapshot_id"] != old_run["evidence_snapshot_id"]
    assert correction["finding_id"] not in new_run["finding_ids"]
    assert correction["observation_id"] in old_run["selected_observation_ids"]
    assert accepted["observation_id"] in new_run["selected_observation_ids"]
    assert correction["observation_id"] not in new_run["selected_observation_ids"]
    assert old_run["decision_ids"] == new_run["decision_ids"] == []
    assert old_run["exception_ids"] == new_run["exception_ids"] == []
    assert old_run["invalidated_decision_ids"] == []
    assert old_run["invalidated_exception_ids"] == []
    assert new_run["applicable_decision_ids"] == []
    assert new_run["applicable_exception_ids"] == []
    assert history["corrections"] == [
        {
            "correction_id": accepted["correction_id"],
            "superseded_observation_id": correction["observation_id"],
            "successor_observation_id": accepted["observation_id"],
            "document_id": correction["document_id"],
            "document_role": correction["document_role"],
            "field": correction["field"],
            "source_location": correction["source_location"],
            "reason_code": correction["reason_code"],
            "actor": REVIEWER.subject,
            "recorded_at": 101,
            "invalidated_decision_ids": [],
            "invalidated_exception_ids": [],
            "evidence_revision": 2,
        }
    ]
    serialized = json.dumps(history, ensure_ascii=False)
    assert "S2ENG54Z" not in serialized
    assert "S2ENG54A" not in serialized


def test_an_existing_decision_makes_the_correction_stale_instead_of_transferring(
    tmp_path: Path,
) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    workspace = service.workspace_view(
        application_id,
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=100,
    )
    verification = {
        "schema_version": "human-decision/1",
        "outcome": "confirmed",
        "reason_code": "HUMAN_REVIEW_COMPLETED",
        "finding_decisions": [
            {
                "finding_id": finding["finding_id"],
                "outcome": (
                    "confirmed"
                    if finding["verdict"] == "uncertain"
                    else "inconclusive"
                ),
            }
            for finding in workspace["mandatory_blockers"]
        ],
    }

    decision = service.submit_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=before["command_context"],
        idempotency_key="s04-decision-wins",
        verification=verification,
        now=101,
    )
    stale = service.correct_field_observation(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=before["command_context"],
        idempotency_key="s04-decision-wins-correction",
        correction=correction,
        now=102,
    )
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    assert decision["status"] == "accepted"
    assert stale["status"] == "stale"
    assert stale["reason_code"] == "STALE_REVIEW_CONTEXT"
    assert history["corrections"] == []
    assert history["runs"][0]["decision_ids"] == [decision["decision_id"]]
    assert history["runs"][0]["applicable_decision_ids"] == [
        decision["decision_id"]
    ]


def test_correction_requires_an_exact_positive_claim_fence(tmp_path: Path) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )

    try:
        service.correct_field_observation(
            principal=REVIEWER,
            application_id=application_id,
            work_item_id=work_item_id,
            expected_fence=True,
            expected_context=before["command_context"],
            idempotency_key="s04-bool-fence",
            correction=correction,
            now=101,
        )
    except ValueError as error:
        assert str(error) == "correction claim fence is invalid"
    else:
        raise AssertionError("boolean claim fence was accepted")

    after = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=101,
    )
    assert after["status"] == "claimed"
    assert after["claim_fence"] == claimed["claim_fence"]
    assert after["evidence_revision"] == before["evidence_revision"]


def test_correction_replay_conflict_source_and_stale_fail_without_extra_effect(
    tmp_path: Path,
) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    arguments = {
        "principal": REVIEWER,
        "application_id": application_id,
        "work_item_id": work_item_id,
        "expected_fence": claimed["claim_fence"],
        "expected_context": before["command_context"],
        "idempotency_key": "s04-command-semantics",
        "correction": correction,
        "now": 101,
    }
    missing_source = copy.deepcopy(arguments)
    missing_source["correction"]["source_location"]["source_region"] = "region:999"

    rejected = service.correct_field_observation(**missing_source)
    stale = service.correct_field_observation(
        **{**arguments, "expected_fence": claimed["claim_fence"] + 1}
    )
    accepted = service.correct_field_observation(**arguments)
    replay = service.correct_field_observation(**arguments)
    changed = copy.deepcopy(arguments)
    changed["correction"]["raw"] = "DIFFERENT-PRIVATE-VALUE"
    conflict = service.correct_field_observation(**changed)
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    assert rejected["status"] == "rejected"
    assert rejected["reason_code"] == "SOURCE_PROOF_MISMATCH"
    assert stale["status"] == "stale"
    assert stale["reason_code"] == "STALE_WORK_ITEM_CLAIM"
    assert accepted["status"] == "accepted"
    assert replay["replayed"] is True
    assert replay["correction_id"] == accepted["correction_id"]
    assert conflict["status"] == "conflict"
    assert conflict["reason_code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert len(history["corrections"]) == 1
    assert history["corrections"][0]["correction_id"] == accepted["correction_id"]
    assert service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )["evidence_revision"] == 2


@pytest.mark.parametrize(
    "principal",
    (
        S01CommandPrincipal(
            subject="another-reviewer",
            role="reviewer",
            scope="C-DEMO",
            source_id="s04-review-console",
        ),
        S01CommandPrincipal(
            subject=REVIEWER.subject,
            role="reviewer",
            scope="R-OBSERVED/another-tenant",
            source_id="s04-review-console",
        ),
        S01CommandPrincipal(
            subject=REVIEWER.subject,
            role="integrator",
            scope="C-DEMO",
            source_id="s04-review-console",
        ),
    ),
)
def test_unauthorized_correction_hides_the_work_item(
    tmp_path: Path,
    principal: S01CommandPrincipal,
) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )

    with pytest.raises(QueryNotFound):
        service.correct_field_observation(
            principal=principal,
            application_id=application_id,
            work_item_id=work_item_id,
            expected_fence=claimed["claim_fence"],
            expected_context=before["command_context"],
            idempotency_key="s04-unauthorized",
            correction=correction,
            now=101,
        )

    assert service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=101,
    )["evidence_revision"] == 1


@pytest.mark.parametrize(
    "fault_point",
    (
        "correction.evidence",
        "correction.lifecycle",
        "correction.exception_invalidation",
        "correction.work_item",
        "correction.job",
        "correction.outbox",
        "correction.audit",
        "correction.idempotency",
        "correction.publish",
    ),
)
def test_each_correction_write_fault_is_atomic(
    tmp_path: Path,
    fault_point: str,
) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )

    def fail(selected: str) -> None:
        if selected == fault_point:
            raise OSError("injected correction write failure")

    faulty = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        fault_injector=fail,
    )
    failed = faulty.correct_field_observation(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=before["command_context"],
        idempotency_key=f"s04-fault-{fault_point}",
        correction=correction,
        now=101,
    )
    observed = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=101,
    )
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    assert failed["status"] == "unavailable"
    assert failed["reason_code"] == (
        "AUDIT_UNAVAILABLE"
        if fault_point == "correction.audit"
        else "STORAGE_UNAVAILABLE"
    )
    assert observed["status"] == "claimed"
    assert observed["evidence_revision"] == 1
    assert observed["run_authority"]["run_id"] == history["current_run_id"]
    assert history["corrections"] == []
    assert len(history["runs"]) == 1


def test_correction_stops_if_the_admitted_source_can_no_longer_be_read_exactly(
    tmp_path: Path,
) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    scenario = fixture_root / "app_r53_bad_engine.json"
    scenario.write_bytes(
        (ROOT / "fixtures" / "applications" / scenario.name).read_bytes()
    )
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path, fixture_root=fixture_root)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    history_before = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    scenario.write_text("{}", encoding="utf-8")

    stopped = service.correct_field_observation(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=before["command_context"],
        idempotency_key="s04-source-integrity",
        correction=correction,
        now=101,
    )
    history_after = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    assert stopped["status"] == "stopped"
    assert stopped["reason_code"] == "SOURCE_EVIDENCE_UNAVAILABLE"
    assert history_after == history_before


def test_registered_review_gate_stops_when_governed_source_objects_are_unreadable(
    tmp_path: Path,
) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    work_item = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    history_before = service.application_history_view(
        principal=reviewer,
        application_id=application_id,
    )
    unreadable = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        controlled_objects=(
            ControlledObject(
                tenant_id="tenant-test",
                source_system_id="registered-source",
                object_ref="result-object",
                media_type="application/json",
                content=b"{}",
            ),
            ControlledObject(
                tenant_id="tenant-test",
                source_system_id="registered-source",
                object_ref="page-object",
                media_type="image/png",
                content=b"unreadable",
            ),
        ),
    )

    stopped = unreadable.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=work_item["command_context"],
        now=102,
    )
    history_after = service.application_history_view(
        principal=reviewer,
        application_id=application_id,
    )

    assert stopped["status"] == "stopped"
    assert stopped["reason_code"] == "SOURCE_EVIDENCE_UNAVAILABLE"
    assert history_after == history_before


def test_two_concurrent_corrections_have_one_revision_winner(tmp_path: Path) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    second = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )

    def correct(target: ControlledScenarioService, suffix: str) -> dict[str, object]:
        return target.correct_field_observation(
            principal=REVIEWER,
            application_id=application_id,
            work_item_id=work_item_id,
            expected_fence=claimed["claim_fence"],
            expected_context=before["command_context"],
            idempotency_key=f"s04-concurrent-{suffix}",
            correction={**correction, "raw": f"S2ENG54{suffix}"},
            now=101,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda pair: correct(*pair),
                ((service, "A"), (second, "B")),
            )
        )
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    assert sorted(result["status"] for result in results) == ["accepted", "stale"]
    assert len(history["corrections"]) == 1
    assert history["corrections"][0]["evidence_revision"] == 2


def test_expired_and_replaced_claim_fences_cannot_correct(tmp_path: Path) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    arguments = {
        "principal": REVIEWER,
        "application_id": application_id,
        "work_item_id": work_item_id,
        "expected_fence": claimed["claim_fence"],
        "expected_context": before["command_context"],
        "idempotency_key": "s04-expired-fence",
        "correction": correction,
    }

    expired = service.correct_field_observation(
        **arguments,
        now=claimed["claim_expires_at"],
    )
    reclaimed = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item_id,
        expected_context=before["command_context"],
        now=claimed["claim_expires_at"],
    )
    replaced = service.correct_field_observation(
        **{**arguments, "idempotency_key": "s04-replaced-fence"},
        now=claimed["claim_expires_at"] + 1,
    )
    accepted = service.correct_field_observation(
        **{
            **arguments,
            "expected_fence": reclaimed["claim_fence"],
            "idempotency_key": "s04-current-fence",
        },
        now=claimed["claim_expires_at"] + 1,
    )

    assert expired["status"] == "stale"
    assert expired["reason_code"] == "STALE_WORK_ITEM_CLAIM"
    assert reclaimed["claim_fence"] == claimed["claim_fence"] + 1
    assert replaced["status"] == "stale"
    assert replaced["reason_code"] == "STALE_WORK_ITEM_CLAIM"
    assert accepted["status"] == "accepted"


def test_vin_correction_maps_legacy_differential_at_target_run_seam(
    tmp_path: Path,
) -> None:
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        scenario_id="app_s04_bad_vin.json",
    )
    admitted = service.submit_demo(
        scenario_id="app_s04_bad_vin.json",
        idempotency_key="s04-vin-intake",
        principal=INTEGRATOR,
    )
    first_run = service.process_next_job()
    service.refresh_projection()
    application_id = admitted.application_id or ""
    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=100,
    )
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
    workspace = service.workspace_view(
        application_id,
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=100,
    )
    finding = next(
        item
        for item in workspace["mandatory_blockers"]
        if item["rule_id"] == "R_VIN_CROSS"
    )
    source = next(
        link for link in finding["evidence_links"] if link["document_id"] == "inv"
    )
    assert "source_page" in source, source
    with pytest.raises(QueryNotFound):
        service.reveal_field_observation(
            principal=S01CommandPrincipal(
                subject="another-reviewer",
                role="reviewer",
                scope=REVIEWER.scope,
                source_id="s04-review-console",
            ),
            application_id=application_id,
            work_item_id=work_item_id,
            observation_id=source["observation_id"],
            expected_fence=claimed["claim_fence"],
            expected_context=work_item["command_context"],
            idempotency_key="s04-unauthorized-vin-reveal",
            purpose="MANUAL_REVIEW",
            reason="EVIDENCE_VERIFICATION",
            classification="RESTRICTED",
            expected_source_region=source["source_region"],
            now=100,
        )
    # S15: C-DEMO synthetic S15 reveal is explicitly denied (G4/C19).
    # The only successful S15 path is the registered controlled authority
    # with validated policy and tenant/resource grant.  This sibling
    # therefore asserts C-DEMO denial and drives the subsequent S04
    # correction via a known fixture value rather than a leaked reveal.
    denied = service.reveal_field_observation(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        observation_id=source["observation_id"],
        expected_fence=claimed["claim_fence"],
        expected_context=work_item["command_context"],
        idempotency_key="s04-vin-reveal",
        purpose="MANUAL_REVIEW",
        reason="EVIDENCE_VERIFICATION",
        classification="RESTRICTED",
        expected_source_region=source["source_region"],
        now=100,
    )
    assert denied["status"] == "rejected"
    assert denied["reason_code"] in ("REVEAL_SYNTHETIC_DENIED", "REVEAL_SYNTHETIC_TRACK_DENIED")
    # For the registered controlled S15 success path, see
    # test_registered_reveal_s15_success_and_policy_denial.
    # Use the known fixture source_text directly for the S04 correction
    # differential without relying on a C-DEMO reveal.
    revealed = {
        "status": "rejected",
        "source_text": "LSVAA4182N5000054",
        "replayed": False,
        "application_id": application_id,
        "work_item_id": work_item_id,
        "observation_id": source["observation_id"],
        "source_location": {
            "source_sha256": source["source_sha256"],
            "source_page": source["source_page"],
            "source_region": source["source_region"],
        },
        "revealed_at": 100,
        "purpose": "MANUAL_REVIEW",
        "reason": "EVIDENCE_VERIFICATION",
        "classification": "RESTRICTED",
        "claim_expires_at": claimed["claim_expires_at"],
    }
    replayed = denied
    conflict = denied
    accepted = service.correct_field_observation(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=work_item["command_context"],
        idempotency_key="s04-vin-correction",
        correction={
            "schema_version": "field-observation-correction/1",
            "finding_id": finding["finding_id"],
            "observation_id": source["observation_id"],
            "document_id": source["document_id"],
            "document_role": source["document_role"],
            "field": source["field"],
            "raw": revealed["source_text"],
            "source_location": {
                key: source[key]
                for key in ("source_sha256", "source_page", "source_region")
            },
            "reason_code": "SOURCE_VALUE_MISREAD",
        },
        now=101,
    )
    second_run = service.process_next_job()
    current = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    timeline = service.audit_timeline(
        principal=S01CommandPrincipal(
            subject="s04-auditor",
            role="auditor",
            scope="C-DEMO",
            source_id="s04-audit-console",
        ),
        application_id=application_id,
    )
    reveal_events = [
        event
        for event in timeline["events"]
        if event["action"] == "evidence_source_revealed"
    ]
    # S15: C-DEMO synthetic reveal is denied (G4/C19) — the only
    # successful S15 path is the registered controlled authority.
    assert len(reveal_events) == 1
    reveal_event = reveal_events[0]
    assert reveal_event["result"] == "rejected"
    assert reveal_event["context"]["reason_code"] in (
        "REVEAL_SYNTHETIC_DENIED",
        "REVEAL_SYNTHETIC_TRACK_DENIED",
    )
    # The synthetic denial is audited without raw/locator.
    assert "LSVAA4182N5000054" not in json.dumps(timeline, ensure_ascii=False)
    assert "source_region" not in json.dumps(reveal_event, ensure_ascii=False)
    # The S04 correction still proceeds via the known fixture value.
    assert denied["status"] == "rejected"

    assert first_run.semantic_differential is not None
    assert first_run.semantic_differential["status"] == "match"
    assert accepted["status"] == "accepted"
    assert second_run.semantic_differential is not None
    assert second_run.semantic_differential["status"] == "mismatch"
    assert [
        mismatch["rule_id"]
        for mismatch in second_run.semantic_differential["mismatches"]
    ] == ["R_VIN_CROSS"]
    assert current["route"] == "auto_complete"
    assert current["current_run_id"] == second_run.run_id


def test_correction_racing_late_old_result_leaves_only_successor_current(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "target.sqlite3"
    checker_entered = threading.Event()
    release_checker = threading.Event()
    delegate = RuleEngine(load_rules(ROOT / "configs" / "rules_auto_lease.yaml"))

    def blocking_checker(application: object) -> object:
        checker_entered.set()
        if not release_checker.wait(timeout=5):
            raise TimeoutError("S04 old checker was not released")
        return delegate.run(application)  # type: ignore[arg-type]

    old_worker = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
        checker_runner=blocking_checker,
        worker_identity="s04-old-worker",
        clock=lambda: 0,
    )
    admitted = old_worker.submit_demo(
        principal=INTEGRATOR,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s04-old-result-race-intake",
    )
    assert admitted.application_id is not None
    application_id = admitted.application_id
    late_results: list[object] = []
    errors: list[BaseException] = []

    def finish_old_result() -> None:
        try:
            late_results.append(old_worker.process_next_job())
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    worker = threading.Thread(target=finish_old_result)
    worker.start()
    assert checker_entered.wait(timeout=5)

    reviewer = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
        worker_identity="s04-takeover-worker",
        clock=lambda: 31,
    )
    old_run = reviewer.process_next_job()
    assert old_run.status == "complete"
    reviewer.refresh_projection()
    item = reviewer.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=31,
    )["items"][0]
    work_item_id = item["work_item_id"]
    work_item = reviewer.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=31,
    )
    claimed = reviewer.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item_id,
        expected_context=work_item["command_context"],
        now=31,
    )
    workspace = reviewer.workspace_view(
        application_id,
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=31,
    )
    finding = next(
        candidate
        for candidate in workspace["mandatory_blockers"]
        if candidate["rule_id"] == "R_ENGINE_CROSS"
    )
    source = next(
        link for link in finding["evidence_links"] if link["document_id"] == "inv"
    )
    correction = {
        "schema_version": "field-observation-correction/1",
        "finding_id": finding["finding_id"],
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
    }
    start = threading.Barrier(2)
    correction_results: list[dict[str, object]] = []

    def correct() -> None:
        try:
            start.wait(timeout=5)
            correction_results.append(
                reviewer.correct_field_observation(
                    principal=REVIEWER,
                    application_id=application_id,
                    work_item_id=work_item_id,
                    expected_fence=claimed["claim_fence"],
                    expected_context=work_item["command_context"],
                    idempotency_key="s04-old-result-race-correction",
                    correction=correction,
                    now=32,
                )
            )
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    correction_worker = threading.Thread(target=correct)
    correction_worker.start()
    start.wait(timeout=5)
    release_checker.set()
    correction_worker.join(timeout=5)
    worker.join(timeout=5)
    assert not correction_worker.is_alive()
    assert not worker.is_alive()
    assert errors == []
    assert correction_results[0]["status"] == "accepted"
    assert late_results[0].status == "stale"  # type: ignore[union-attr]

    finisher = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
        clock=lambda: 33,
    )
    successor = finisher.process_next_job()
    history = finisher.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    assert successor.status == "complete"
    assert successor.run_id != old_run.run_id
    assert sum(run["current"] for run in history["runs"]) == 1
    assert next(run for run in history["runs"] if run["current"])["run_id"] == successor.run_id
    assert all(
        not run["current"] for run in history["runs"] if run["run_id"] == old_run.run_id
    )


def test_review_result_winning_the_concurrent_publish_makes_correction_stale(
    tmp_path: Path,
) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    workspace = service.workspace_view(
        application_id,
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=100,
    )
    verification = {
        "schema_version": "human-decision/1",
        "outcome": "confirmed",
        "reason_code": "HUMAN_REVIEW_COMPLETED",
        "finding_decisions": [
            {
                "finding_id": finding["finding_id"],
                "outcome": (
                    "confirmed"
                    if finding["verdict"] == "uncertain"
                    else "inconclusive"
                ),
            }
            for finding in workspace["mandatory_blockers"]
        ],
    }
    correction_entered = threading.Event()
    release_correction = threading.Event()

    def block_source_read(read_point: str) -> None:
        if read_point != "review.source_read":
            return
        correction_entered.set()
        if not release_correction.wait(timeout=5):
            raise TimeoutError("S04 result-first correction was not released")

    correction_service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        fault_injector=block_source_read,
    )
    correction_results: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def correct() -> None:
        try:
            correction_results.append(
                correction_service.correct_field_observation(
                    principal=REVIEWER,
                    application_id=application_id,
                    work_item_id=work_item_id,
                    expected_fence=claimed["claim_fence"],
                    expected_context=before["command_context"],
                    idempotency_key="s04-result-first-correction",
                    correction=correction,
                    now=101,
                )
            )
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    correction_worker = threading.Thread(target=correct)
    correction_worker.start()
    assert correction_entered.wait(timeout=5)

    winner = service.submit_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=before["command_context"],
        idempotency_key="s04-result-first-review",
        verification=verification,
        now=101,
    )
    release_correction.set()
    correction_worker.join(timeout=5)
    assert not correction_worker.is_alive()
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    assert errors == []
    assert winner["status"] == "accepted"
    assert len(correction_results) == 1
    stale = correction_results[0]
    assert stale["status"] == "stale"
    assert stale["reason_code"] == "STALE_REVIEW_CONTEXT"
    assert history["corrections"] == []
    assert history["runs"][0]["applicable_decision_ids"] == [winner["decision_id"]]


def test_successor_run_takeover_fences_the_late_worker_result(tmp_path: Path) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    accepted = service.correct_field_observation(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=before["command_context"],
        idempotency_key="s04-worker-fence",
        correction=correction,
        now=101,
    )
    checker_entered = threading.Event()
    release_checker = threading.Event()
    delegate = RuleEngine(load_rules(ROOT / "configs" / "rules_auto_lease.yaml"))

    def blocking_checker(application: object) -> object:
        checker_entered.set()
        if not release_checker.wait(timeout=5):
            raise TimeoutError("S04 test checker was not released")
        return delegate.run(application)  # type: ignore[arg-type]

    expiring = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        checker_runner=blocking_checker,
        worker_identity="s04-expiring-worker",
        clock=lambda: 0,
    )
    late_results: list[object] = []

    worker = threading.Thread(
        target=lambda: late_results.append(expiring.process_next_job())
    )
    worker.start()
    assert checker_entered.wait(timeout=5)
    takeover = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        worker_identity="s04-takeover-worker",
        clock=lambda: 31,
    )
    winner = takeover.process_next_job()
    release_checker.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    successor_records = [
        run for run in history["runs"] if run["run_id"] == winner.run_id
    ]

    assert accepted["evidence_revision"] == 2
    assert winner.status == "complete"
    assert len(late_results) == 1
    assert late_results[0].status == "stale"  # type: ignore[union-attr]
    assert late_results[0].reason_code == "STALE_COMPARE_AND_SET"  # type: ignore[union-attr]
    assert [run["status"] for run in successor_records] == ["complete", "stale"]
    assert [run["current"] for run in successor_records] == [True, False]
    assert successor_records[1]["currentness_reason"] == "STALE_COMPLETION_CONTEXT"
    assert successor_records[1]["cas_mismatches"] == [
        "lifecycle_revision",
        "fence",
    ]
    assert sum(run["current"] for run in history["runs"]) == 1


@pytest.mark.parametrize("cas_field", ("evidence_revision", "release_digest"))
def test_successor_result_with_changed_evidence_or_policy_cannot_become_current(
    tmp_path: Path,
    cas_field: str,
) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    service.correct_field_observation(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=before["command_context"],
        idempotency_key=f"s04-cas-{cas_field}",
        correction=correction,
        now=101,
    )

    stale = ControlledScenarioTestDriver(service).process_next_job(
        cas_fault=cas_field,
        now=102,
    )
    pending = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    winner = service.process_next_job()
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    assert stale.status == "stale"
    assert stale.cas_mismatches == (cas_field,)
    assert pending["route"] == "pending_check"
    assert pending["current_run_id"] is None
    assert winner.status == "complete"
    assert winner.evidence_revision == 2
    assert sum(run["current"] for run in history["runs"]) == 1
    assert next(run for run in history["runs"] if run["current"])["status"] == "complete"


def test_correction_audit_exposes_minimized_provenance_chain(tmp_path: Path) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    accepted = service.correct_field_observation(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=before["command_context"],
        idempotency_key="s04-audit-chain",
        correction=correction,
        now=101,
    )
    timeline = service.audit_timeline(
        principal=S01CommandPrincipal(
            subject="s04-auditor",
            role="auditor",
            scope="C-DEMO",
            source_id="s04-audit-console",
        ),
        application_id=application_id,
    )
    event = next(
        item for item in timeline["events"] if item["action"] == "evidence_correction"
    )

    assert event["result"] == "accepted"
    assert event["context"]["correction_id"] == accepted["correction_id"]
    assert event["context"]["old_observation_id"] == correction["observation_id"]
    assert event["context"]["new_observation_id"] == accepted["observation_id"]
    assert event["context"]["invalidated_run_id"] == accepted["invalidated_run_id"]
    serialized = json.dumps(timeline, ensure_ascii=False)
    assert "S2ENG54Z" not in serialized
    assert "S2ENG54A" not in serialized


def test_correction_and_successor_history_survive_restart_without_duplicates(
    tmp_path: Path,
) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    arguments = {
        "principal": REVIEWER,
        "application_id": application_id,
        "work_item_id": work_item_id,
        "expected_fence": claimed["claim_fence"],
        "expected_context": before["command_context"],
        "idempotency_key": "s04-restart",
        "correction": correction,
        "now": 101,
    }
    accepted = service.correct_field_observation(**arguments)
    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
    )

    replay = restarted.correct_field_observation(**arguments)
    pending_history = restarted.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    completed = restarted.process_next_job()
    first_projection = restarted.refresh_projection()
    second_projection = restarted.refresh_projection()
    observed = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
    )
    history = observed.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    assert replay["replayed"] is True
    assert replay["correction_id"] == accepted["correction_id"]
    assert pending_history["current_run_id"] is None
    assert len(pending_history["corrections"]) == 1
    assert completed.status == "complete"
    assert first_projection["updated"] == 1
    assert second_projection["updated"] == 0
    assert len(history["corrections"]) == 1
    assert [run["current"] for run in history["runs"]] == [False, True]
    assert observed.process_next_job().status == "idle"


def test_pause_recover_and_forward_run_preserve_the_accepted_correction(
    tmp_path: Path,
) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    operator = S01CommandPrincipal(
        subject="s04-operator",
        role="operator",
        scope="C-DEMO",
        source_id="s04-control-plane",
    )
    arguments = {
        "principal": REVIEWER,
        "application_id": application_id,
        "work_item_id": work_item_id,
        "expected_fence": claimed["claim_fence"],
        "expected_context": before["command_context"],
        "idempotency_key": "s04-release-rehearsal",
        "correction": correction,
        "now": 101,
    }

    service.stop_new_cohort(
        reason_code="S01_RUNTIME_UNHEALTHY",
        failure_reason_code="SOURCE_EVIDENCE_UNAVAILABLE",
        principal=operator,
    )
    blocked = service.correct_field_observation(**arguments)
    recovered_corrections = service.recover_runtime(
        expected_failure_reason_code="SOURCE_EVIDENCE_UNAVAILABLE",
        principal=operator,
    )
    accepted = service.correct_field_observation(**arguments)
    service.stop_new_cohort(
        reason_code="S01_RUNTIME_UNHEALTHY",
        failure_reason_code="S01_BACKGROUND_RUNTIME_EXCEPTION",
        principal=operator,
    )
    stopped_worker = service.process_next_job()
    stopped_history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    recovered_worker = service.recover_runtime(
        expected_failure_reason_code="S01_BACKGROUND_RUNTIME_EXCEPTION",
        principal=operator,
    )
    completed = service.process_next_job()
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    assert blocked["status"] == "stopped"
    assert blocked["reason_code"] == "SOURCE_EVIDENCE_UNAVAILABLE"
    assert recovered_corrections["recovery"] == "scheduled"
    assert accepted["status"] == "accepted"
    assert stopped_worker.status == "stopped"
    assert stopped_history["corrections"][0]["correction_id"] == accepted["correction_id"]
    assert stopped_history["current_run_id"] is None
    assert recovered_worker["recovery"] == "scheduled"
    assert completed.status == "complete"
    assert history["corrections"] == stopped_history["corrections"]
    assert sum(run["current"] for run in history["runs"]) == 1


def test_known_good_checker_rollback_is_used_only_by_the_new_run(tmp_path: Path) -> None:
    service, application_id, work_item_id, claimed, correction = (
        _ready_engine_correction(tmp_path)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    old_history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    accepted = service.correct_field_observation(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=before["command_context"],
        idempotency_key="s04-checker-rollback",
        correction=correction,
        now=101,
    )
    calls: list[str] = []

    def broken_checker(application: object) -> object:
        calls.append("broken")
        raise RuntimeError("deployed checker is unhealthy")

    broken = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        checker_runner=broken_checker,
        checker_build="s01-target-checker/broken",
        clock=lambda: 102,
    )
    failed = broken.process_next_job()
    failed_history = broken.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    rolled_back = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        checker_build="s01-target-checker/known-good",
        clock=lambda: 104,
    )
    completed = rolled_back.process_next_job()
    history = rolled_back.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    assert accepted["status"] == "accepted"
    assert failed.status == "failed"
    assert calls == ["broken"]
    assert failed_history["corrections"] == history["corrections"]
    assert old_history["runs"][0]["authority_digest"] == history["runs"][0][
        "authority_digest"
    ]
    failed_run = next(run for run in history["runs"] if run["status"] == "checker_failed")
    current_run = next(run for run in history["runs"] if run["current"])
    assert old_history["runs"][0]["checker_build"] == "s01-target-checker/6"
    assert failed_run["checker_build"] == "s01-target-checker/broken"
    assert current_run["checker_build"] == "s01-target-checker/known-good"
    assert len(
        {
            old_history["runs"][0]["release_digest"],
            failed_run["release_digest"],
            current_run["release_digest"],
        }
    ) == 3
    assert completed.status == "complete"
    assert completed.run_id != old_history["runs"][0]["run_id"]
    assert current_run["run_id"] == completed.run_id
