from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
from pathlib import Path
from threading import Barrier, Lock

import pytest

from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules
from task4_consistency.controlled.s01 import (
    AdmissionDisposition,
    ControlledScenarioService,
    QueryNotFound,
    S01CommandPrincipal,
)
from tests.test_s02_controlled import (
    INTEGRATOR,
    ROOT,
    TENANT_SCOPE,
    _detection_result,
    _registered_service,
)


def ready_review_work_item(
    tmp_path: Path,
) -> tuple[ControlledScenarioService, S01CommandPrincipal, str, str]:
    service, submission = _registered_service(tmp_path)
    reviewer = S01CommandPrincipal(
        subject=INTEGRATOR.subject,
        role="reviewer",
        scope=TENANT_SCOPE,
        source_id="s03-review-console",
    )
    admission = service.submit_registered(
        submission=submission,
        idempotency_key="s03-happy-path-intake",
        principal=INTEGRATOR,
    )
    completed = service.process_next_job()
    service.refresh_projection()

    assert admission.disposition is AdmissionDisposition.ACCEPTED
    assert admission.application_id is not None
    assert completed.status == "complete"
    queue = service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=101,
    )
    assert len(queue["items"]) == 1
    workspace = service.workspace_view(
        admission.application_id,
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=101,
    )
    assert workspace["selected_finding"]["verdict"] == "uncertain"
    return (
        service,
        reviewer,
        admission.application_id,
        queue["items"][0]["work_item_id"],
    )


def review_batch_items(
    work_item: dict[str, object], *, expected_fence: int
) -> list[dict[str, object]]:
    return [
        {
            "work_item_id": work_item["work_item_id"],
            "finding_id": finding["finding_id"],
            "outcome": (
                "confirmed" if finding["verdict"] == "uncertain" else "inconclusive"
            ),
            "reason_code": "HUMAN_REVIEW_COMPLETED",
            "expected_fence": expected_fence,
            "expected_context": work_item["command_context"],
        }
        for finding in work_item["automatic_findings"]
    ]


def add_review_application(
    service: ControlledScenarioService,
    submission: dict[str, object],
    *,
    principal: S01CommandPrincipal,
    suffix: str,
) -> tuple[S01CommandPrincipal, str, str]:
    command = deepcopy(submission)
    command["envelope_id"] = f"s03-envelope-{suffix}"
    command["upstream_application_ref"] = f"s03-application-{suffix}"
    command["stream_id"] = f"s03-stream-{suffix}"
    command["document_binding"]["source_document_ref"] = f"s03-document-{suffix}"
    command["attachments"][0]["source_attachment_ref"] = (
        f"s03-attachment-{suffix}"
    )
    command["attachments"][0]["page_ref"] = f"s03-page-{suffix}"
    command["producer"]["run_id"] = f"s03-producer-run-{suffix}"
    admission = service.submit_registered(
        submission=command,
        idempotency_key=f"s03-intake-{suffix}",
        principal=principal,
    )
    completed = service.process_next_job()
    service.refresh_projection()
    reviewer = S01CommandPrincipal(
        subject=principal.subject,
        role="reviewer",
        scope=principal.scope,
        source_id="s03-review-console",
    )

    assert admission.disposition is AdmissionDisposition.ACCEPTED
    assert admission.application_id is not None
    assert completed.status == "complete"
    matches = [
        item
        for item in service.queue_view(
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=101,
        )["items"]
        if item["application_id"] == admission.application_id
    ]
    assert len(matches) == 1
    return reviewer, admission.application_id, matches[0]["work_item_id"]


def test_reviewer_can_claim_visible_uncertain_work_item(tmp_path: Path) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )

    assert initial["status"] == "unclaimed"
    assert initial["claim_subject"] is None
    assert initial["claim_fence"] == 0
    assert initial["claim_expires_at"] == 0

    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    observed = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )

    assert claimed == {
        "status": "claimed",
        "application_id": application_id,
        "work_item_id": work_item_id,
        "claim_subject": reviewer.subject,
        "claim_fence": 1,
        "claim_expires_at": 1001,
    }
    assert observed["status"] == "claimed"
    assert observed["claim_subject"] == reviewer.subject
    assert observed["claim_fence"] == 1
    assert observed["claim_expires_at"] == 1001


def test_reviewer_can_renew_release_and_reclaim_work_item(tmp_path: Path) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    expected_context = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )["command_context"]
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=expected_context,
        now=101,
    )

    renewed = service.renew_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=expected_context,
        idempotency_key="s03-renew",
        now=150,
    )
    released = service.release_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=expected_context,
        idempotency_key="s03-release",
        now=151,
    )
    observed_release = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=151,
    )
    reclaimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=expected_context,
        now=152,
    )

    assert renewed == {
        "status": "renewed",
        "application_id": application_id,
        "work_item_id": work_item_id,
        "claim_subject": reviewer.subject,
        "claim_fence": 1,
        "claim_expires_at": 1050,
        "replayed": False,
    }
    assert released == {
        "status": "released",
        "application_id": application_id,
        "work_item_id": work_item_id,
        "claim_fence": 1,
        "released_at": 151,
        "replayed": False,
    }
    assert observed_release["status"] == "released"
    assert observed_release["claim_subject"] is None
    assert observed_release["claim_fence"] == 1
    assert observed_release["claim_expires_at"] == 151
    assert reclaimed["status"] == "claimed"
    assert reclaimed["claim_fence"] == 2


def test_renew_and_release_retry_replays_without_duplicate_facts(tmp_path: Path) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    renew_args = {
        "principal": reviewer,
        "work_item_id": work_item_id,
        "expected_fence": claimed["claim_fence"],
        "expected_context": initial["command_context"],
        "idempotency_key": "s03-renew-response-loss",
    }
    renewed = service.renew_review_work_item(**renew_args, now=150)
    after_renew = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=150,
    )
    renew_fact_count = sum(
        record.get("record_type") == "work_item_renewed"
        for record in service._store.review_records
    )
    renew_replay = service.renew_review_work_item(**renew_args, now=9999)
    assert renewed["status"] == "renewed"
    assert renewed["replayed"] is False
    assert renew_replay == {**renewed, "replayed": True}
    assert service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=150,
    ) == after_renew
    assert sum(
        record.get("record_type") == "work_item_renewed"
        for record in service._store.review_records
    ) == renew_fact_count

    release_args = {
        "principal": reviewer,
        "work_item_id": work_item_id,
        "expected_fence": claimed["claim_fence"],
        "expected_context": initial["command_context"],
        "idempotency_key": "s03-release-response-loss",
    }
    released = service.release_review_work_item(**release_args, now=151)
    release_fact_count = sum(
        record.get("record_type") == "work_item_released"
        for record in service._store.review_records
    )
    release_replay = service.release_review_work_item(**release_args, now=9999)
    assert released == {
        "status": "released",
        "application_id": application_id,
        "work_item_id": work_item_id,
        "claim_fence": claimed["claim_fence"],
        "released_at": 151,
        "replayed": False,
    }
    assert release_replay == {**released, "replayed": True}
    assert sum(
        record.get("record_type") == "work_item_released"
        for record in service._store.review_records
    ) == release_fact_count


@pytest.mark.parametrize(
    ("command_name", "idempotency_key", "audit_action"),
    (
        (
            "renew_review_work_item",
            "s03-concurrent-renew",
            "review_work_item_renewed",
        ),
        (
            "release_review_work_item",
            "s03-concurrent-release",
            "review_work_item_released",
        ),
    ),
)
def test_lifecycle_response_is_stable_across_store_revision_race(
    tmp_path: Path,
    command_name: str,
    idempotency_key: str,
    audit_action: str,
) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    ready_to_persist = Barrier(2)
    counter_lock = Lock()
    audit_writes = 0

    def synchronize_first_writes(write_point: str) -> None:
        nonlocal audit_writes
        if write_point != "review.audit":
            return
        with counter_lock:
            audit_writes += 1
            should_wait = audit_writes <= 2
        if should_wait:
            ready_to_persist.wait()

    contenders = [
        ControlledScenarioService(
            fixture_root=service.fixture_root,
            rules_path=service.rules_path,
            state_path=tmp_path / "target.sqlite3",
            fault_injector=synchronize_first_writes,
        )
        for _ in range(2)
    ]
    command = {
        "principal": reviewer,
        "work_item_id": work_item_id,
        "expected_fence": claimed["claim_fence"],
        "expected_context": initial["command_context"],
        "idempotency_key": idempotency_key,
        "now": 150,
    }
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda contender: getattr(contender, command_name)(**command),
                contenders,
            )
        )

    assert {result["status"] for result in results} == {
        "renewed" if command_name.startswith("renew") else "released"
    }
    assert sorted(result["replayed"] for result in results) == [False, True]
    timeline = service.audit_timeline(
        principal=S01CommandPrincipal(
            subject="tenant-auditor",
            role="auditor",
            scope=reviewer.scope,
            source_id="s03-audit-console",
        ),
        application_id=application_id,
    )
    assert sum(event["action"] == audit_action for event in timeline["events"]) == 1


@pytest.mark.parametrize(
    "command_name",
    ("renew_review_work_item", "release_review_work_item"),
)
def test_lifecycle_command_requires_explicit_idempotency_key_without_changes(
    tmp_path: Path,
    command_name: str,
) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    before = {
        "work_item": service.review_work_item_view(
            principal=reviewer,
            work_item_id=work_item_id,
            now=102,
        ),
        "audit": service.audit_timeline(
            principal=auditor,
            application_id=application_id,
        ),
    }

    with pytest.raises(ValueError, match="idempotency key is invalid"):
        getattr(service, command_name)(
            principal=reviewer,
            work_item_id=work_item_id,
            expected_fence=claimed["claim_fence"],
            expected_context=initial["command_context"],
            idempotency_key=None,
            now=102,
        )

    assert service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    ) == before["work_item"]
    assert service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    ) == before["audit"]


def test_structured_human_decision_is_atomic_immutable_and_idempotent(
    tmp_path: Path,
) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    before = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    workspace = service.workspace_view(
        application_id,
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=101,
    )
    automatic_before = [
        {
            key: finding[key]
            for key in ("finding_id", "rule_id", "verdict", "severity", "reason_code")
        }
        for finding in workspace["mandatory_blockers"]
    ]
    verification = {
        "schema_version": "human-decision/1",
        "outcome": "confirmed",
        "reason_code": "HUMAN_REVIEW_COMPLETED",
        "finding_decisions": [
            {
                "finding_id": finding["finding_id"],
                "outcome": (
                    "confirmed" if finding["verdict"] == "uncertain" else "inconclusive"
                ),
            }
            for finding in automatic_before
        ],
    }

    accepted = service.submit_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=initial["command_context"],
        idempotency_key="s03-human-decision",
        verification=verification,
        now=102,
    )
    replay = service.submit_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=initial["command_context"],
        idempotency_key="s03-human-decision",
        verification=verification,
        now=103,
    )
    conflict = service.submit_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=initial["command_context"],
        idempotency_key="s03-human-decision",
        verification={
            **verification,
            "reason_code": "HUMAN_REVIEW_RECONSIDERED",
        },
        now=103,
    )
    after = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=103,
    )
    queue = service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=103,
    )
    timeline = service.audit_timeline(
        principal=S01CommandPrincipal(
            subject="tenant-auditor",
            role="auditor",
            scope=reviewer.scope,
            source_id="s03-audit-console",
        ),
        application_id=application_id,
    )
    terminal_claim = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=after["command_context"],
        now=104,
    )
    final = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=104,
    )
    restarted = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
    )
    after_restart = restarted.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=104,
    )

    assert accepted["status"] == "accepted"
    assert accepted["replayed"] is False
    assert accepted["application_id"] == application_id
    assert accepted["work_item_id"] == work_item_id
    assert accepted["claim_fence"] == 1
    assert accepted["lifecycle_revision"] == before["lifecycle_revision"] + 1
    assert accepted["evidence_revision"] == before["evidence_revision"]
    assert accepted["route"] == "human_complete"
    assert replay == {**accepted, "replayed": True}
    assert conflict == {
        "status": "conflict",
        "replayed": False,
        "application_id": application_id,
        "work_item_id": work_item_id,
        "reason_code": "IDEMPOTENCY_KEY_CONFLICT",
    }
    assert after["status"] == "completed"
    assert after["phase"] == "Verification Completed"
    assert after["route"] == "human_complete"
    assert after["lifecycle_revision"] == before["lifecycle_revision"] + 1
    assert after["evidence_revision"] == before["evidence_revision"]
    assert after["automatic_findings"] == before["automatic_findings"] == automatic_before
    assert any(finding["verdict"] == "uncertain" for finding in automatic_before)
    assert after["run_authority"] == before["run_authority"]
    release_id = next(
        event["context"]["release_id"]
        for event in timeline["events"]
        if "release_id" in event["context"]
    )
    assert after["decision"] == {
        "decision_id": accepted["decision_id"],
        "schema_version": "human-decision/1",
        "outcome": "confirmed",
        "reason_code": "HUMAN_REVIEW_COMPLETED",
        "finding_decisions": verification["finding_decisions"],
        "reviewer_subject": reviewer.subject,
        "reviewer_role": reviewer.role,
        "reviewer_source_id": reviewer.source_id,
        "assigned_subject": reviewer.subject,
        "cycle": 1,
        "finding_ids": [
            finding["finding_id"] for finding in automatic_before
        ],
        "evidence_snapshot_id": workspace["evidence_snapshot_id"],
        "release_id": release_id,
        "fixed_context": initial["command_context"],
        "claim_fence": 1,
        "submitted_at": 102,
    }
    assert queue["items"] == []
    assert timeline["events"][-1]["action"] == "human_decision_submitted"
    assert timeline["events"][-1]["context"] == {
        "application_id": application_id,
        "run_id": before["run_authority"]["run_id"],
        "reason_code": "HUMAN_REVIEW_COMPLETED",
        "route": "human_complete",
        "lifecycle_revision": before["lifecycle_revision"] + 1,
        "evidence_revision": before["evidence_revision"],
        "work_item_id": work_item_id,
        "decision_id": accepted["decision_id"],
        "outcome": "confirmed",
        "claim_fence": 1,
    }
    assert timeline["integrity"] == "verified"
    assert terminal_claim == {
        "status": "completed",
        "application_id": application_id,
        "work_item_id": work_item_id,
        "reason_code": "WORK_ITEM_COMPLETED",
    }
    assert final == after
    assert after_restart == after


def test_competing_reviewer_sessions_cannot_both_claim_one_work_item(
    tmp_path: Path,
) -> None:
    first, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    second = ControlledScenarioService(
        fixture_root=first.fixture_root,
        rules_path=first.rules_path,
        state_path=tmp_path / "target.sqlite3",
    )
    second_reviewer = S01CommandPrincipal(
        subject=reviewer.subject,
        role=reviewer.role,
        scope=reviewer.scope,
        source_id="s03-second-review-console",
    )
    expected_context = first.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )["command_context"]
    start = Barrier(2)

    def claim(
        service: ControlledScenarioService,
        principal: S01CommandPrincipal,
    ) -> dict[str, object]:
        start.wait()
        return service.claim_review_work_item(
            principal=principal,
            work_item_id=work_item_id,
            expected_context=expected_context,
            now=101,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(claim, first, reviewer),
            executor.submit(claim, second, second_reviewer),
        )
        results = [future.result() for future in futures]

    observed = first.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claimed = [result for result in results if result["status"] == "claimed"]
    conflicted = [result for result in results if result["status"] == "conflict"]

    assert len(claimed) == 1
    assert conflicted == [
        {
            "status": "conflict",
            "application_id": application_id,
            "work_item_id": work_item_id,
            "reason_code": "WORK_ITEM_ALREADY_CLAIMED",
        }
    ]
    assert observed["status"] == "claimed"
    assert observed["claim_fence"] == 1


def test_expired_and_taken_over_claims_cannot_mutate_with_a_stale_fence(
    tmp_path: Path,
) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    expected_context = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )["command_context"]
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=expected_context,
        now=101,
    )
    verification = {
        "schema_version": "human-decision/1",
        "outcome": "confirmed",
        "reason_code": "HUMAN_REVIEW_COMPLETED",
        "finding_decisions": [
            {"finding_id": finding["finding_id"], "outcome": "confirmed"}
            for finding in service.review_work_item_view(
                principal=reviewer,
                work_item_id=work_item_id,
                now=101,
            )["automatic_findings"]
        ],
    }
    expired_renew = service.renew_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=expected_context,
        idempotency_key="s03-expired-renew",
        now=claimed["claim_expires_at"],
    )
    expired_release = service.release_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=expected_context,
        idempotency_key="s03-expired-release",
        now=claimed["claim_expires_at"],
    )
    expired_submit = service.submit_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=expected_context,
        idempotency_key="s03-expired-submit",
        verification=verification,
        now=claimed["claim_expires_at"],
    )
    taken_over = service.claim_review_work_item(
        principal=S01CommandPrincipal(
            subject=reviewer.subject,
            role=reviewer.role,
            scope=reviewer.scope,
            source_id="s03-takeover-review-console",
        ),
        work_item_id=work_item_id,
        expected_context=expected_context,
        now=claimed["claim_expires_at"] + 1,
    )
    before_stale_commands = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=claimed["claim_expires_at"] + 1,
    )
    audit_before = service.audit_timeline(
        principal=S01CommandPrincipal(
            subject="tenant-auditor",
            role="auditor",
            scope=reviewer.scope,
            source_id="s03-audit-console",
        ),
        application_id=application_id,
    )

    stale_renew = service.renew_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=expected_context,
        idempotency_key="s03-stale-renew",
        now=claimed["claim_expires_at"] + 2,
    )
    stale_release = service.release_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=expected_context,
        idempotency_key="s03-stale-release",
        now=claimed["claim_expires_at"] + 2,
    )
    stale_submit = service.submit_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=expected_context,
        idempotency_key="s03-taken-over-submit",
        verification=verification,
        now=claimed["claim_expires_at"] + 2,
    )
    after_stale_commands = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=claimed["claim_expires_at"] + 2,
    )
    audit_after = service.audit_timeline(
        principal=S01CommandPrincipal(
            subject="tenant-auditor",
            role="auditor",
            scope=reviewer.scope,
            source_id="s03-audit-console",
        ),
        application_id=application_id,
    )

    stale_claim = {
        "status": "stale",
        "application_id": application_id,
        "work_item_id": work_item_id,
        "reason_code": "STALE_WORK_ITEM_CLAIM",
    }
    assert expired_renew == stale_claim
    assert expired_release == stale_claim
    assert expired_submit == {**stale_claim, "replayed": False}
    assert taken_over["status"] == "claimed"
    assert taken_over["claim_fence"] == claimed["claim_fence"] + 1
    assert stale_renew == stale_claim
    assert stale_release == stale_claim
    assert stale_submit == {**stale_claim, "replayed": False}
    assert after_stale_commands == before_stale_commands
    assert audit_after == audit_before


def test_each_review_command_rejects_each_stale_expected_context(
    tmp_path: Path,
) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    expected_context = initial["command_context"]
    mismatches = (
        {**expected_context, "lifecycle_revision": expected_context["lifecycle_revision"] + 1},
        {**expected_context, "evidence_revision": expected_context["evidence_revision"] + 1},
        {**expected_context, "run_id": "stale-run"},
        {
            **expected_context,
            "projection_watermark": expected_context["projection_watermark"] + 1,
        },
        {**expected_context, "current_context": "0" * 64},
    )
    stale = {
        "status": "stale",
        "application_id": application_id,
        "work_item_id": work_item_id,
        "reason_code": "STALE_REVIEW_CONTEXT",
    }

    for mismatch in mismatches:
        assert service.claim_review_work_item(
            principal=reviewer,
            work_item_id=work_item_id,
            expected_context=mismatch,
            now=101,
        ) == stale
    assert service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    ) == initial

    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=expected_context,
        now=101,
    )
    claimed_view = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    queue_before = service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=101,
    )
    audit_before = service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    )
    verification = {
        "schema_version": "human-decision/1",
        "outcome": "confirmed",
        "reason_code": "HUMAN_REVIEW_COMPLETED",
        "finding_decisions": [
            {"finding_id": finding["finding_id"], "outcome": "confirmed"}
            for finding in claimed_view["automatic_findings"]
        ],
    }

    for index, mismatch in enumerate(mismatches):
        assert service.renew_review_work_item(
            principal=reviewer,
            work_item_id=work_item_id,
            expected_fence=claimed["claim_fence"],
            expected_context=mismatch,
            idempotency_key=f"s03-context-renew-{index}",
            now=102,
        ) == stale
        assert service.release_review_work_item(
            principal=reviewer,
            work_item_id=work_item_id,
            expected_fence=claimed["claim_fence"],
            expected_context=mismatch,
            idempotency_key=f"s03-context-release-{index}",
            now=102,
        ) == stale
        assert service.submit_review_work_item(
            principal=reviewer,
            work_item_id=work_item_id,
            expected_fence=claimed["claim_fence"],
            expected_context=mismatch,
            idempotency_key=f"s03-stale-context-{index}",
            verification=verification,
            now=102,
        ) == {**stale, "replayed": False}

    assert service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    ) == claimed_view
    assert service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=102,
    ) == queue_before
    assert service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    ) == audit_before

def test_review_commands_hide_unauthorized_identity_and_resource_without_changes(
    tmp_path: Path,
) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    claimed_view = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    verification = {
        "schema_version": "human-decision/1",
        "outcome": "confirmed",
        "reason_code": "HUMAN_REVIEW_COMPLETED",
        "finding_decisions": [
            {"finding_id": finding["finding_id"], "outcome": "confirmed"}
            for finding in claimed_view["automatic_findings"]
        ],
    }
    before = {
        "queue": service.queue_view(
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=101,
        ),
        "work_item": claimed_view,
        "lifecycle": service.workspace_view(
            application_id,
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=101,
        ),
        "audit": service.audit_timeline(
            principal=auditor,
            application_id=application_id,
        ),
    }
    denied = (
        (
            S01CommandPrincipal(
                subject=reviewer.subject,
                role="reviewer",
                scope="C-DEMO",
                source_id="s03-review-console",
            ),
            work_item_id,
        ),
        (
            S01CommandPrincipal(
                subject=reviewer.subject,
                role="reviewer",
                scope="R-OBSERVED/other-organization",
                source_id="s03-review-console",
            ),
            work_item_id,
        ),
        (
            S01CommandPrincipal(
                subject=reviewer.subject,
                role="integrator",
                scope=reviewer.scope,
                source_id="s03-review-console",
            ),
            work_item_id,
        ),
        (
            S01CommandPrincipal(
                subject="unassigned-reviewer",
                role="reviewer",
                scope=reviewer.scope,
                source_id="s03-review-console",
            ),
            work_item_id,
        ),
        (reviewer, "unknown-review-work-item"),
        (
            S01CommandPrincipal(
                subject=reviewer.subject,
                role="reviewer",
                scope=reviewer.scope,
                source_id="s03-review-console",
                expires_at=101,
            ),
            work_item_id,
        ),
    )

    for index, (denied_principal, target_id) in enumerate(denied):
        commands = (
            lambda: service.claim_review_work_item(
                principal=denied_principal,
                work_item_id=target_id,
                expected_context=initial["command_context"],
                now=101,
            ),
            lambda: service.renew_review_work_item(
                principal=denied_principal,
                work_item_id=target_id,
                expected_fence=claimed["claim_fence"],
                expected_context=initial["command_context"],
                idempotency_key=f"s03-denied-renew-{index}",
                now=101,
            ),
            lambda: service.release_review_work_item(
                principal=denied_principal,
                work_item_id=target_id,
                expected_fence=claimed["claim_fence"],
                expected_context=initial["command_context"],
                idempotency_key=f"s03-denied-release-{index}",
                now=101,
            ),
            lambda: service.submit_review_work_item(
                principal=denied_principal,
                work_item_id=target_id,
                expected_fence=claimed["claim_fence"],
                expected_context=initial["command_context"],
                idempotency_key=f"s03-denied-{index}",
                verification=verification,
                now=101,
            ),
        )
        for command in commands:
            with pytest.raises(QueryNotFound) as error:
                command()
            assert error.value.args == (target_id,)
        with pytest.raises(QueryNotFound) as error:
            service.review_work_item_view(
                principal=denied_principal,
                work_item_id=target_id,
                now=101,
            )
        assert error.value.args == (target_id,)

    assert service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=101,
    ) == before["queue"]
    assert service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    ) == before["work_item"]
    assert service.workspace_view(
        application_id,
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=101,
    ) == before["lifecycle"]
    assert service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    ) == before["audit"]


def test_claim_audit_failure_is_atomic(tmp_path: Path) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )
    before = {
        "queue": service.queue_view(
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=101,
        ),
        "work_item": service.review_work_item_view(
            principal=reviewer,
            work_item_id=work_item_id,
            now=101,
        ),
        "lifecycle": service.workspace_view(
            application_id,
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=101,
        ),
        "audit": service.audit_timeline(
            principal=auditor,
            application_id=application_id,
        ),
    }
    fired = False

    def fail_review_audit(write_point: str) -> None:
        nonlocal fired
        if write_point == "review.audit":
            fired = True
            raise OSError("injected review audit failure")

    faulty = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
        fault_injector=fail_review_audit,
    )

    result = faulty.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=before["work_item"]["command_context"],
        now=101,
    )

    assert fired is True
    assert result == {
        "status": "unavailable",
        "application_id": application_id,
        "work_item_id": work_item_id,
        "reason_code": "AUDIT_UNAVAILABLE",
    }
    assert service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=101,
    ) == before["queue"]
    assert service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    ) == before["work_item"]
    assert service.workspace_view(
        application_id,
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=101,
    ) == before["lifecycle"]
    assert service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    ) == before["audit"]


def test_renew_audit_failure_is_atomic(tmp_path: Path) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    before = {
        "queue": service.queue_view(
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=102,
        ),
        "work_item": service.review_work_item_view(
            principal=reviewer,
            work_item_id=work_item_id,
            now=102,
        ),
        "lifecycle": service.workspace_view(
            application_id,
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=102,
        ),
        "audit": service.audit_timeline(
            principal=auditor,
            application_id=application_id,
        ),
    }
    fired = False

    def fail_review_audit(write_point: str) -> None:
        nonlocal fired
        if write_point == "review.audit":
            fired = True
            raise OSError("injected review audit failure")

    faulty = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
        fault_injector=fail_review_audit,
    )

    result = faulty.renew_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=initial["command_context"],
        idempotency_key="s03-renew-audit-failure",
        now=102,
    )

    assert fired is True
    assert result == {
        "status": "unavailable",
        "application_id": application_id,
        "work_item_id": work_item_id,
        "reason_code": "AUDIT_UNAVAILABLE",
    }
    assert service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=102,
    ) == before["queue"]
    assert service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    ) == before["work_item"]
    assert service.workspace_view(
        application_id,
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=102,
    ) == before["lifecycle"]
    assert service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    ) == before["audit"]


def test_release_audit_failure_is_atomic(tmp_path: Path) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    before = {
        "queue": service.queue_view(
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=102,
        ),
        "work_item": service.review_work_item_view(
            principal=reviewer,
            work_item_id=work_item_id,
            now=102,
        ),
        "lifecycle": service.workspace_view(
            application_id,
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=102,
        ),
        "audit": service.audit_timeline(
            principal=auditor,
            application_id=application_id,
        ),
    }
    fired = False

    def fail_review_audit(write_point: str) -> None:
        nonlocal fired
        if write_point == "review.audit":
            fired = True
            raise OSError("injected review audit failure")

    faulty = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
        fault_injector=fail_review_audit,
    )

    result = faulty.release_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=initial["command_context"],
        idempotency_key="s03-release-audit-failure",
        now=102,
    )

    assert fired is True
    assert result == {
        "status": "unavailable",
        "application_id": application_id,
        "work_item_id": work_item_id,
        "reason_code": "AUDIT_UNAVAILABLE",
    }
    assert service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=102,
    ) == before["queue"]
    assert service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    ) == before["work_item"]
    assert service.workspace_view(
        application_id,
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=102,
    ) == before["lifecycle"]
    assert service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    ) == before["audit"]


def test_submit_audit_failure_is_atomic_and_retryable(tmp_path: Path) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    claimed_view = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    )
    verification = {
        "schema_version": "human-decision/1",
        "outcome": "confirmed",
        "reason_code": "HUMAN_REVIEW_COMPLETED",
        "finding_decisions": [
            {"finding_id": finding["finding_id"], "outcome": "confirmed"}
            for finding in initial["automatic_findings"]
        ],
    }
    before = {
        "queue": service.queue_view(
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=102,
        ),
        "work_item": claimed_view,
        "lifecycle": service.workspace_view(
            application_id,
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=102,
        ),
        "audit": service.audit_timeline(
            principal=auditor,
            application_id=application_id,
        ),
    }
    fired = False

    def fail_review_audit(write_point: str) -> None:
        nonlocal fired
        if write_point == "review.audit":
            fired = True
            raise OSError("injected review audit failure")

    faulty = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
        fault_injector=fail_review_audit,
    )

    result = faulty.submit_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=initial["command_context"],
        idempotency_key="s03-submit-audit-failure",
        verification=verification,
        now=102,
    )

    assert fired is True
    assert result == {
        "status": "unavailable",
        "replayed": False,
        "application_id": application_id,
        "work_item_id": work_item_id,
        "reason_code": "AUDIT_UNAVAILABLE",
    }
    assert service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=102,
    ) == before["queue"]
    assert service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    ) == before["work_item"]
    assert service.workspace_view(
        application_id,
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=102,
    ) == before["lifecycle"]
    assert service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    ) == before["audit"]

    recovered = service.submit_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=initial["command_context"],
        idempotency_key="s03-submit-audit-failure",
        verification=verification,
        now=102,
    )
    assert recovered["status"] == "accepted"
    assert recovered["replayed"] is False


def test_single_and_batch_submit_faults_share_stable_contract_and_are_atomic(
    tmp_path: Path,
) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    claimed_view = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    )
    verification = {
        "schema_version": "human-decision/1",
        "outcome": "confirmed",
        "reason_code": "HUMAN_REVIEW_COMPLETED",
        "finding_decisions": [
            {"finding_id": finding["finding_id"], "outcome": "confirmed"}
            for finding in initial["automatic_findings"]
        ],
    }
    plan = service.preview_review_work_item_batch(
        principal=reviewer,
        items=review_batch_items(
            claimed_view,
            expected_fence=claimed["claim_fence"],
        ),
        now=102,
    )
    before = {
        "queue": service.queue_view(
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=102,
        ),
        "work_item": claimed_view,
        "audit": service.audit_timeline(
            principal=S01CommandPrincipal(
                subject="tenant-auditor",
                role="auditor",
                scope=reviewer.scope,
                source_id="s03-audit-console",
            ),
            application_id=application_id,
        ),
    }

    for write_point, reason_code in (
        ("review.lifecycle", "STORAGE_UNAVAILABLE"),
        ("review.audit", "AUDIT_UNAVAILABLE"),
    ):
        def fail_write(point: str, *, expected: str = write_point) -> None:
            if point == expected:
                raise OSError(f"injected {expected} failure")

        faulty = ControlledScenarioService(
            fixture_root=service.fixture_root,
            rules_path=service.rules_path,
            state_path=tmp_path / "target.sqlite3",
            fault_injector=fail_write,
        )
        single = faulty.submit_review_work_item(
            principal=reviewer,
            work_item_id=work_item_id,
            expected_fence=claimed["claim_fence"],
            expected_context=initial["command_context"],
            idempotency_key=f"s03-single-{reason_code.lower()}",
            verification=verification,
            now=102,
        )
        batch = faulty.submit_review_work_item_batch(
            principal=reviewer,
            idempotency_key=f"s03-batch-{reason_code.lower()}",
            plan=plan,
            now=102,
        )
        stable = lambda result: {
            key: result[key] for key in ("status", "replayed", "reason_code")
        }
        assert stable(single) == stable(batch) == {
            "status": "unavailable",
            "replayed": False,
            "reason_code": reason_code,
        }
        assert single["application_id"] == batch["application_id"] == application_id
        assert service.queue_view(
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=102,
        ) == before["queue"]
        assert service.review_work_item_view(
            principal=reviewer,
            work_item_id=work_item_id,
            now=102,
        ) == before["work_item"]
        assert service.audit_timeline(
            principal=S01CommandPrincipal(
                subject="tenant-auditor",
                role="auditor",
                scope=reviewer.scope,
                source_id="s03-audit-console",
            ),
            application_id=application_id,
        ) == before["audit"]


def test_batch_preview_is_minimized_submit_ready_and_side_effect_free(
    tmp_path: Path,
) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    claimed_view = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    )
    items = review_batch_items(
        claimed_view,
        expected_fence=claimed["claim_fence"],
    )
    before = {
        "queue": service.queue_view(
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=102,
        ),
        "work_item": claimed_view,
        "lifecycle": service.workspace_view(
            application_id,
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=102,
        ),
        "audit": service.audit_timeline(
            principal=auditor,
            application_id=application_id,
        ),
    }

    plan = service.preview_review_work_item_batch(
        principal=reviewer,
        items=items,
        now=102,
    )

    assert plan == {
        "schema_version": "review-batch-plan/1",
        "items": items,
    }
    assert set(plan) == {"schema_version", "items"}
    assert all(
        set(item)
        == {
            "work_item_id",
            "finding_id",
            "outcome",
            "reason_code",
            "expected_fence",
            "expected_context",
        }
        for item in plan["items"]
    )
    assert service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=102,
    ) == before["queue"]
    assert service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    ) == before["work_item"]
    assert service.workspace_view(
        application_id,
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=102,
    ) == before["lifecycle"]
    assert service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    ) == before["audit"]


def test_batch_commit_is_atomic_per_finding_immutable_and_idempotent(
    tmp_path: Path,
) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    before = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    )
    audit_before = service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    )
    plan = service.preview_review_work_item_batch(
        principal=reviewer,
        items=review_batch_items(
            before,
            expected_fence=claimed["claim_fence"],
        ),
        now=102,
    )

    accepted = service.submit_review_work_item_batch(
        principal=reviewer,
        idempotency_key="s03-batch-decision",
        plan=plan,
        now=102,
    )
    replay = service.submit_review_work_item_batch(
        principal=reviewer,
        idempotency_key="s03-batch-decision",
        plan=plan,
        now=103,
    )
    conflicting_plan = {
        **plan,
        "items": [dict(item) for item in plan["items"]],
    }
    conflicting_plan["items"][0]["outcome"] = "not_confirmed"
    conflict = service.submit_review_work_item_batch(
        principal=reviewer,
        idempotency_key="s03-batch-decision",
        plan=conflicting_plan,
        now=103,
    )
    after = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=103,
    )
    audit_after = service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    )
    restarted = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
    )

    assert len(plan["items"]) > 1
    assert accepted["status"] == "accepted"
    assert accepted["replayed"] is False
    assert accepted["application_id"] == application_id
    assert accepted["run_id"] == before["run_authority"]["run_id"]
    assert accepted["work_item_ids"] == [work_item_id]
    assert accepted["lifecycle_revision"] == before["lifecycle_revision"] + 1
    assert accepted["evidence_revision"] == before["evidence_revision"]
    assert accepted["route"] == "human_complete"
    assert len(accepted["items"]) == len(plan["items"])
    assert replay == {**accepted, "replayed": True}
    assert conflict == {
        "status": "conflict",
        "replayed": False,
        "application_id": application_id,
        "reason_code": "IDEMPOTENCY_KEY_CONFLICT",
    }
    assert after["status"] == "completed"
    assert after["phase"] == "Verification Completed"
    assert after["route"] == "human_complete"
    assert after["lifecycle_revision"] == before["lifecycle_revision"] + 1
    assert after["evidence_revision"] == before["evidence_revision"]
    assert after["automatic_findings"] == before["automatic_findings"]
    assert after["run_authority"] == before["run_authority"]
    assert after["completed_finding_ids"] == [
        item["finding_id"] for item in plan["items"]
    ]
    assert len(after["decisions"]) == len(plan["items"])
    assert after["decision"] is None
    assert [decision["decision_id"] for decision in after["decisions"]] == [
        item["decision_id"] for item in accepted["items"]
    ]
    for decision, item in zip(after["decisions"], plan["items"], strict=True):
        assert decision == {
            "decision_id": decision["decision_id"],
            "schema_version": "human-decision/1",
            "outcome": item["outcome"],
            "reason_code": item["reason_code"],
            "finding_decisions": [
                {
                    "finding_id": item["finding_id"],
                    "outcome": item["outcome"],
                }
            ],
            "reviewer_subject": reviewer.subject,
            "reviewer_role": reviewer.role,
            "reviewer_source_id": reviewer.source_id,
            "assigned_subject": reviewer.subject,
            "cycle": 1,
            "finding_ids": [item["finding_id"]],
            "evidence_snapshot_id": decision["evidence_snapshot_id"],
            "release_id": decision["release_id"],
            "fixed_context": item["expected_context"],
            "claim_fence": claimed["claim_fence"],
            "submitted_at": 102,
        }
    batch_audit = audit_after["events"][len(audit_before["events"]) :]
    assert len(batch_audit) == len(plan["items"])
    assert all(event["action"] == "human_decision_submitted" for event in batch_audit)
    assert [event["context"]["finding_id"] for event in batch_audit] == [
        item["finding_id"] for item in plan["items"]
    ]
    assert [event["context"]["decision_id"] for event in batch_audit] == [
        item["decision_id"] for item in accepted["items"]
    ]
    assert service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=103,
    )["items"] == []
    assert restarted.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=103,
    ) == after
    assert restarted.audit_timeline(
        principal=auditor,
        application_id=application_id,
    ) == audit_after


def test_batch_rejects_mixed_application_and_run_atomically(tmp_path: Path) -> None:
    service, submission = _registered_service(tmp_path)
    reviewer, first_application_id, first_work_item_id = add_review_application(
        service,
        submission,
        principal=INTEGRATOR,
        suffix="batch-first",
    )
    _, second_application_id, second_work_item_id = add_review_application(
        service,
        submission,
        principal=INTEGRATOR,
        suffix="batch-second",
    )
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )
    plans = []
    for work_item_id in (first_work_item_id, second_work_item_id):
        initial = service.review_work_item_view(
            principal=reviewer,
            work_item_id=work_item_id,
            now=101,
        )
        claimed = service.claim_review_work_item(
            principal=reviewer,
            work_item_id=work_item_id,
            expected_context=initial["command_context"],
            now=101,
        )
        claimed_view = service.review_work_item_view(
            principal=reviewer,
            work_item_id=work_item_id,
            now=102,
        )
        plans.append(
            service.preview_review_work_item_batch(
                principal=reviewer,
                items=review_batch_items(
                    claimed_view,
                    expected_fence=claimed["claim_fence"],
                ),
                now=102,
            )
        )
    mixed_plan = {
        "schema_version": "review-batch-plan/1",
        "items": plans[0]["items"] + plans[1]["items"],
    }
    application_ids = (first_application_id, second_application_id)
    work_item_ids = (first_work_item_id, second_work_item_id)
    before = {
        "queue": service.queue_view(
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=102,
        ),
        "work_items": [
            service.review_work_item_view(
                principal=reviewer,
                work_item_id=work_item_id,
                now=102,
            )
            for work_item_id in work_item_ids
        ],
        "lifecycles": [
            service.workspace_view(
                application_id,
                role="reviewer",
                scope=reviewer.scope,
                subject=reviewer.subject,
                now=102,
            )
            for application_id in application_ids
        ],
        "audits": [
            service.audit_timeline(
                principal=auditor,
                application_id=application_id,
            )
            for application_id in application_ids
        ],
    }

    rejected = service.submit_review_work_item_batch(
        principal=reviewer,
        idempotency_key="s03-mixed-application-run",
        plan=mixed_plan,
        now=102,
    )

    assert rejected == {
        "status": "rejected",
        "replayed": False,
        "reason_code": "MIXED_REVIEW_BATCH",
    }
    assert service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=102,
    ) == before["queue"]
    assert [
        service.review_work_item_view(
            principal=reviewer,
            work_item_id=work_item_id,
            now=102,
        )
        for work_item_id in work_item_ids
    ] == before["work_items"]
    assert [
        service.workspace_view(
            application_id,
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=102,
        )
        for application_id in application_ids
    ] == before["lifecycles"]
    assert [
        service.audit_timeline(
            principal=auditor,
            application_id=application_id,
        )
        for application_id in application_ids
    ] == before["audits"]


def test_batch_rejects_stale_context_and_released_state_without_changes(
    tmp_path: Path,
) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    claimed_view = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    )
    plan = service.preview_review_work_item_batch(
        principal=reviewer,
        items=review_batch_items(
            claimed_view,
            expected_fence=claimed["claim_fence"],
        ),
        now=102,
    )
    stale_context_plan = {
        **plan,
        "items": [dict(item) for item in plan["items"]],
    }
    stale_context_plan["items"][0]["expected_context"] = {
        **stale_context_plan["items"][0]["expected_context"],
        "run_id": "stale-run",
    }
    audit_before = service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    )

    assert service.submit_review_work_item_batch(
        principal=reviewer,
        idempotency_key="s03-batch-stale-context",
        plan=stale_context_plan,
        now=102,
    ) == {
        "status": "stale",
        "replayed": False,
        "application_id": application_id,
        "work_item_id": work_item_id,
        "reason_code": "STALE_REVIEW_CONTEXT",
    }
    assert service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    ) == claimed_view
    assert service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    ) == audit_before

    released = service.release_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=initial["command_context"],
        idempotency_key="s03-batch-release-state",
        now=102,
    )
    released_view = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=103,
    )
    released_audit = service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    )
    assert released["status"] == "released"
    assert service.submit_review_work_item_batch(
        principal=reviewer,
        idempotency_key="s03-batch-released-state",
        plan=plan,
        now=103,
    ) == {
        "status": "stale",
        "replayed": False,
        "application_id": application_id,
        "work_item_id": work_item_id,
        "reason_code": "STALE_WORK_ITEM_CLAIM",
    }
    assert service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=103,
    ) == released_view
    assert service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    ) == released_audit


def test_batch_rejects_stale_entry_atomically_with_stable_error_contract(
    tmp_path: Path,
) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    before = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    )
    plan = service.preview_review_work_item_batch(
        principal=reviewer,
        items=review_batch_items(
            before,
            expected_fence=claimed["claim_fence"],
        ),
        now=102,
    )
    stale_plan = {
        **plan,
        "items": [dict(item) for item in plan["items"]],
    }
    stale_plan["items"][0]["expected_fence"] += 1
    audit_before = service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    )
    queue_before = service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=102,
    )
    lifecycle_before = service.workspace_view(
        application_id,
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=102,
    )

    rejected = service.submit_review_work_item_batch(
        principal=reviewer,
        idempotency_key="s03-batch-stale-fence",
        plan=stale_plan,
        now=102,
    )

    assert rejected == {
        "status": "stale",
        "replayed": False,
        "application_id": application_id,
        "work_item_id": work_item_id,
        "reason_code": "STALE_WORK_ITEM_CLAIM",
    }
    assert service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    ) == before
    assert service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=102,
    ) == queue_before
    assert service.workspace_view(
        application_id,
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=102,
    ) == lifecycle_before
    assert service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    ) == audit_before


def test_batch_preview_and_submit_reject_mixed_authority_and_claim_state_atomically(
    tmp_path: Path,
) -> None:
    service, submission = _registered_service(tmp_path)
    reviewer, first_application_id, first_work_item_id = add_review_application(
        service,
        submission,
        principal=INTEGRATOR,
        suffix="batch-authorized",
    )
    other_integrator = S01CommandPrincipal(
        subject="other-reviewer",
        role="integrator",
        scope=INTEGRATOR.scope,
        source_id=INTEGRATOR.source_id,
    )
    other_reviewer, other_application_id, other_work_item_id = add_review_application(
        service,
        submission,
        principal=other_integrator,
        suffix="batch-other-assignee",
    )
    _, released_application_id, released_work_item_id = add_review_application(
        service,
        submission,
        principal=INTEGRATOR,
        suffix="batch-released",
    )
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )

    authorities = (
        (reviewer, first_application_id, first_work_item_id),
        (other_reviewer, other_application_id, other_work_item_id),
        (reviewer, released_application_id, released_work_item_id),
    )
    plans: list[dict[str, object]] = []
    claims: list[dict[str, object]] = []
    for owner, _, work_item_id in authorities:
        initial = service.review_work_item_view(
            principal=owner,
            work_item_id=work_item_id,
            now=101,
        )
        claimed = service.claim_review_work_item(
            principal=owner,
            work_item_id=work_item_id,
            expected_context=initial["command_context"],
            now=101,
        )
        claimed_view = service.review_work_item_view(
            principal=owner,
            work_item_id=work_item_id,
            now=102,
        )
        plans.append(
            service.preview_review_work_item_batch(
                principal=owner,
                items=review_batch_items(
                    claimed_view,
                    expected_fence=claimed["claim_fence"],
                ),
                now=102,
            )
        )
        claims.append(claimed)
    service.release_review_work_item(
        principal=reviewer,
        work_item_id=released_work_item_id,
        expected_fence=claims[2]["claim_fence"],
        expected_context=plans[2]["items"][0]["expected_context"],
        idempotency_key="s03-mixed-batch-release-state",
        now=102,
    )

    def public_state() -> dict[str, object]:
        return {
            "queues": [
                service.queue_view(
                    role=owner.role,
                    scope=owner.scope,
                    subject=owner.subject,
                    now=103,
                )
                for owner in (reviewer, other_reviewer)
            ],
            "work_items": [
                service.review_work_item_view(
                    principal=owner,
                    work_item_id=work_item_id,
                    now=103,
                )
                for owner, _, work_item_id in authorities
            ],
            "lifecycles": [
                service.workspace_view(
                    application_id,
                    role=owner.role,
                    scope=owner.scope,
                    subject=owner.subject,
                    now=103,
                )
                for owner, application_id, _ in authorities
            ],
            "audits": [
                service.audit_timeline(
                    principal=auditor,
                    application_id=application_id,
                )
                for _, application_id, _ in authorities
            ],
        }

    before = public_state()
    denied_principals = (
        S01CommandPrincipal(
            subject=reviewer.subject,
            role="integrator",
            scope=reviewer.scope,
            source_id=reviewer.source_id,
        ),
        S01CommandPrincipal(
            subject=reviewer.subject,
            role="reviewer",
            scope="R-OBSERVED/other-organization",
            source_id=reviewer.source_id,
        ),
        S01CommandPrincipal(
            subject=reviewer.subject,
            role="reviewer",
            scope=reviewer.scope,
            source_id=reviewer.source_id,
            expires_at=103,
        ),
    )
    for index, denied in enumerate(denied_principals):
        with pytest.raises(QueryNotFound) as preview_error:
            service.preview_review_work_item_batch(
                principal=denied,
                items=plans[0]["items"],
                now=103,
            )
        assert preview_error.value.args == (first_work_item_id,)
        with pytest.raises(QueryNotFound) as submit_error:
            service.submit_review_work_item_batch(
                principal=denied,
                idempotency_key=f"s03-batch-denied-{index}",
                plan=plans[0],
                now=103,
            )
        assert submit_error.value.args == (first_work_item_id,)
        assert public_state() == before

    mixed_assignment_plan = {
        "schema_version": "review-batch-plan/1",
        "items": plans[0]["items"] + plans[1]["items"],
    }
    with pytest.raises(QueryNotFound) as preview_error:
        service.preview_review_work_item_batch(
            principal=reviewer,
            items=mixed_assignment_plan["items"],
            now=103,
        )
    assert preview_error.value.args == (other_work_item_id,)
    with pytest.raises(QueryNotFound) as submit_error:
        service.submit_review_work_item_batch(
            principal=reviewer,
            idempotency_key="s03-batch-mixed-assignment",
            plan=mixed_assignment_plan,
            now=103,
        )
    assert submit_error.value.args == (other_work_item_id,)
    assert public_state() == before

    mixed_claim_plan = {
        "schema_version": "review-batch-plan/1",
        "items": plans[0]["items"] + plans[2]["items"],
    }
    assert service.preview_review_work_item_batch(
        principal=reviewer,
        items=mixed_claim_plan["items"],
        now=103,
    ) == {
        "status": "stale",
        "application_id": released_application_id,
        "work_item_id": released_work_item_id,
        "reason_code": "STALE_WORK_ITEM_CLAIM",
    }
    assert public_state() == before
    assert service.submit_review_work_item_batch(
        principal=reviewer,
        idempotency_key="s03-batch-mixed-claim",
        plan=mixed_claim_plan,
        now=103,
    ) == {
        "status": "stale",
        "replayed": False,
        "application_id": released_application_id,
        "work_item_id": released_work_item_id,
        "reason_code": "STALE_WORK_ITEM_CLAIM",
    }
    assert public_state() == before


def test_verification_write_gate_stops_when_source_evidence_is_unreadable(
    tmp_path: Path,
) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    before = {
        "queue": service.queue_view(
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=102,
        ),
        "work_item": initial,
        "lifecycle": service.workspace_view(
            application_id,
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=102,
        ),
        "audit": service.audit_timeline(
            principal=auditor,
            application_id=application_id,
        ),
    }

    def fail_source_read(read_point: str) -> None:
        if read_point == "review.source_read":
            raise OSError("injected source-read failure")

    unreadable = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
        fault_injector=fail_source_read,
    )
    stopped = {
        "status": "stopped",
        "application_id": application_id,
        "work_item_id": work_item_id,
        "reason_code": "SOURCE_EVIDENCE_UNAVAILABLE",
    }

    assert unreadable.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=102,
    ) == stopped
    assert unreadable.cohort_status() == {
        "track": "C-DEMO",
        "admission": "stopped",
        "reason_code": "S01_RUNTIME_UNHEALTHY",
        "failure_reason_code": "SOURCE_EVIDENCE_UNAVAILABLE",
    }
    assert service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=102,
    ) == before["queue"]
    assert service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    ) == before["work_item"]
    assert service.workspace_view(
        application_id,
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=102,
    ) == before["lifecycle"]
    audit_after = service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    )
    assert audit_after["events"][:-1] == before["audit"]["events"]
    assert audit_after["events"][-1]["action"] == "controlled_cohort_stop"
    assert audit_after["events"][-1]["context"] == {
        "reason_code": "S01_RUNTIME_UNHEALTHY",
        "failure_reason_code": "SOURCE_EVIDENCE_UNAVAILABLE",
        "admission_after_stop": {
            "track": "C-DEMO",
            "admission": "stopped",
            "reason_code": "S01_RUNTIME_UNHEALTHY",
            "failure_reason_code": "SOURCE_EVIDENCE_UNAVAILABLE",
        },
    }


def test_rejected_review_commands_do_not_stop_cohort_on_source_read_fault(
    tmp_path: Path,
) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    verification = {
        "schema_version": "human-decision/1",
        "outcome": "confirmed",
        "reason_code": "HUMAN_REVIEW_COMPLETED",
        "finding_decisions": [
            {"finding_id": finding["finding_id"], "outcome": "confirmed"}
            for finding in initial["automatic_findings"]
        ],
    }

    def fail_source_read(read_point: str) -> None:
        if read_point == "review.source_read":
            raise OSError("injected source-read failure")

    faulty = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
        fault_injector=fail_source_read,
    )
    before = {
        "queue": faulty.queue_view(
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=102,
        ),
        "work_item": faulty.review_work_item_view(
            principal=reviewer,
            work_item_id=work_item_id,
            now=102,
        ),
        "audit": faulty.audit_timeline(
            principal=S01CommandPrincipal(
                subject="tenant-auditor",
                role="auditor",
                scope=reviewer.scope,
                source_id="s03-audit-console",
            ),
            application_id=application_id,
        ),
    }
    stale_fence = claimed["claim_fence"] + 1

    renew = faulty.renew_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=stale_fence,
        expected_context=initial["command_context"],
        idempotency_key="s03-source-fault-stale-renew",
        now=102,
    )
    claim = faulty.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=102,
    )
    submit = faulty.submit_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=stale_fence,
        expected_context=initial["command_context"],
        idempotency_key="s03-source-fault-stale-submit",
        verification=verification,
        now=102,
    )
    stale = {
        "status": "stale",
        "application_id": application_id,
        "work_item_id": work_item_id,
        "reason_code": "STALE_WORK_ITEM_CLAIM",
    }

    assert renew == stale
    assert claim == {
        "status": "conflict",
        "application_id": application_id,
        "work_item_id": work_item_id,
        "reason_code": "WORK_ITEM_ALREADY_CLAIMED",
    }
    assert submit == {**stale, "replayed": False}
    assert faulty.cohort_status() == {"track": "C-DEMO", "admission": "open"}
    assert faulty.queue_view(
        role="reviewer", scope=reviewer.scope, subject=reviewer.subject, now=102
    ) == before["queue"]
    assert faulty.review_work_item_view(
        principal=reviewer, work_item_id=work_item_id, now=102
    ) == before["work_item"]
    assert faulty.audit_timeline(
        principal=S01CommandPrincipal(
            subject="tenant-auditor",
            role="auditor",
            scope=reviewer.scope,
            source_id="s03-audit-console",
        ),
        application_id=application_id,
    ) == before["audit"]


def test_runtime_stop_release_requeues_with_successor_fence_and_stales_old_fence(
    tmp_path: Path,
) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )
    operator = S01CommandPrincipal(
        subject="runtime-operator",
        role="operator",
        scope="C-DEMO",
        source_id="s03-control-plane",
    )
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    verification = {
        "schema_version": "human-decision/1",
        "outcome": "confirmed",
        "reason_code": "HUMAN_REVIEW_COMPLETED",
        "finding_decisions": [
            {"finding_id": finding["finding_id"], "outcome": "confirmed"}
            for finding in initial["automatic_findings"]
        ],
    }

    def fail_source_read(read_point: str) -> None:
        if read_point == "review.source_read":
            raise OSError("injected source-read failure")

    stopped_service = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
        fault_injector=fail_source_read,
    )
    stopped = stopped_service.renew_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=initial["command_context"],
        idempotency_key="s03-stop-renew",
        now=102,
    )
    released = stopped_service.release_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=initial["command_context"],
        idempotency_key="s03-stop-release",
        now=103,
    )
    queued = stopped_service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=103,
    )
    released_view = stopped_service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=103,
    )

    assert stopped["status"] == "stopped"
    assert stopped["reason_code"] == "SOURCE_EVIDENCE_UNAVAILABLE"
    assert released["status"] == "released"
    assert len(queued["items"]) == 1
    assert queued["items"][0]["work_item_id"] == work_item_id
    assert queued["items"][0]["claim_fence"] == claimed["claim_fence"]
    assert released_view["status"] == "released"
    assert released_view["claim_fence"] == claimed["claim_fence"]

    recovered_service = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
    )
    recovery = recovered_service.recover_runtime(
        principal=operator,
        expected_failure_reason_code="SOURCE_EVIDENCE_UNAVAILABLE",
    )
    reclaimed = recovered_service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=104,
    )
    before_stale_commands = {
        "queue": recovered_service.queue_view(
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=105,
        ),
        "work_item": recovered_service.review_work_item_view(
            principal=reviewer,
            work_item_id=work_item_id,
            now=105,
        ),
        "audit": recovered_service.audit_timeline(
            principal=auditor,
            application_id=application_id,
        ),
    }
    stale_claim = {
        "status": "stale",
        "application_id": application_id,
        "work_item_id": work_item_id,
        "reason_code": "STALE_WORK_ITEM_CLAIM",
    }

    assert recovery == {
        "track": "C-DEMO",
        "recovery": "scheduled",
        "reason_code": "S01_RUNTIME_RECOVERY_SCHEDULED",
        "failure_reason_code": "SOURCE_EVIDENCE_UNAVAILABLE",
        "requeued_jobs": 0,
    }
    assert recovered_service.cohort_status() == {
        "track": "C-DEMO",
        "admission": "open",
    }
    assert reclaimed["status"] == "claimed"
    assert reclaimed["claim_fence"] > claimed["claim_fence"]
    assert before_stale_commands["queue"]["items"][0]["claim_fence"] == reclaimed[
        "claim_fence"
    ]
    assert before_stale_commands["work_item"]["claim_fence"] == reclaimed[
        "claim_fence"
    ]
    assert recovered_service.renew_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=initial["command_context"],
        idempotency_key="s03-recovered-stale-renew",
        now=105,
    ) == stale_claim
    assert recovered_service.release_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=initial["command_context"],
        idempotency_key="s03-recovered-stale-release",
        now=105,
    ) == stale_claim
    assert recovered_service.submit_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claimed["claim_fence"],
        expected_context=initial["command_context"],
        idempotency_key="s03-stale-after-runtime-recovery",
        verification=verification,
        now=105,
    ) == {**stale_claim, "replayed": False}
    assert recovered_service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=105,
    ) == before_stale_commands["queue"]
    assert recovered_service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=105,
    ) == before_stale_commands["work_item"]
    assert recovered_service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    ) == before_stale_commands["audit"]


def test_stop_recovery_restart_requeue_preserves_single_and_batch_authority(
    tmp_path: Path,
) -> None:
    service, submission = _registered_service(tmp_path)
    reviewer, single_application_id, single_work_item_id = add_review_application(
        service,
        submission,
        principal=INTEGRATOR,
        suffix="preserve-single",
    )
    _, batch_application_id, batch_work_item_id = add_review_application(
        service,
        submission,
        principal=INTEGRATOR,
        suffix="preserve-batch",
    )
    _, open_application_id, open_work_item_id = add_review_application(
        service,
        submission,
        principal=INTEGRATOR,
        suffix="preserve-open",
    )
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )
    operator = S01CommandPrincipal(
        subject="runtime-operator",
        role="operator",
        scope="C-DEMO",
        source_id="s03-control-plane",
    )

    single_initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=single_work_item_id,
        now=101,
    )
    single_claim = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=single_work_item_id,
        expected_context=single_initial["command_context"],
        now=101,
    )
    single_verification = {
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
            for finding in single_initial["automatic_findings"]
        ],
    }
    assert service.submit_review_work_item(
        principal=reviewer,
        work_item_id=single_work_item_id,
        expected_fence=single_claim["claim_fence"],
        expected_context=single_initial["command_context"],
        idempotency_key="s03-preserve-single",
        verification=single_verification,
        now=102,
    )["status"] == "accepted"

    batch_initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=batch_work_item_id,
        now=101,
    )
    batch_claim = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=batch_work_item_id,
        expected_context=batch_initial["command_context"],
        now=101,
    )
    batch_claimed = service.review_work_item_view(
        principal=reviewer,
        work_item_id=batch_work_item_id,
        now=102,
    )
    batch_plan = service.preview_review_work_item_batch(
        principal=reviewer,
        items=review_batch_items(
            batch_claimed,
            expected_fence=batch_claim["claim_fence"],
        ),
        now=102,
    )
    assert service.submit_review_work_item_batch(
        principal=reviewer,
        idempotency_key="s03-preserve-batch",
        plan=batch_plan,
        now=102,
    )["status"] == "accepted"

    open_initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=open_work_item_id,
        now=101,
    )
    open_claim = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=open_work_item_id,
        expected_context=open_initial["command_context"],
        now=101,
    )
    open_claimed = service.review_work_item_view(
        principal=reviewer,
        work_item_id=open_work_item_id,
        now=102,
    )
    open_plan = service.preview_review_work_item_batch(
        principal=reviewer,
        items=review_batch_items(
            open_claimed,
            expected_fence=open_claim["claim_fence"],
        ),
        now=102,
    )

    def accepted_authority(
        current: ControlledScenarioService,
        application_id: str,
        work_item_id: str,
    ) -> dict[str, object]:
        timeline = current.audit_timeline(
            principal=auditor,
            application_id=application_id,
        )
        return {
            "work_item": current.review_work_item_view(
                principal=reviewer,
                work_item_id=work_item_id,
                now=103,
            ),
            "run_and_decision_events": [
                event
                for event in timeline["events"]
                if event["action"]
                in {"controlled_run_result", "human_decision_submitted"}
            ],
        }

    accepted_before = {
        "single": accepted_authority(
            service,
            single_application_id,
            single_work_item_id,
        ),
        "batch": accepted_authority(
            service,
            batch_application_id,
            batch_work_item_id,
        ),
    }
    assert accepted_before["single"]["work_item"]["status"] == "completed"
    assert len(accepted_before["single"]["work_item"]["decisions"]) == 1
    assert accepted_before["batch"]["work_item"]["status"] == "completed"
    assert len(accepted_before["batch"]["work_item"]["decisions"]) > 1

    def fail_source_read(read_point: str) -> None:
        if read_point == "review.source_read":
            raise OSError("injected source-read failure")

    stopped_service = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
        fault_injector=fail_source_read,
    )
    assert stopped_service.renew_review_work_item(
        principal=reviewer,
        work_item_id=open_work_item_id,
        expected_fence=open_claim["claim_fence"],
        expected_context=open_initial["command_context"],
        idempotency_key="s03-rollback-stop-renew",
        now=103,
    )["status"] == "stopped"
    assert stopped_service.submit_review_work_item_batch(
        principal=reviewer,
        idempotency_key="s03-blocked-batch-during-stop",
        plan=open_plan,
        now=103,
    ) == {
        "status": "stopped",
        "replayed": False,
        "application_id": open_application_id,
        "reason_code": "SOURCE_EVIDENCE_UNAVAILABLE",
    }
    assert stopped_service.release_review_work_item(
        principal=reviewer,
        work_item_id=open_work_item_id,
        expected_fence=open_claim["claim_fence"],
        expected_context=open_initial["command_context"],
        idempotency_key="s03-rollback-stop-release",
        now=104,
    )["status"] == "released"

    recovered_service = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
    )
    assert recovered_service.recover_runtime(
        principal=operator,
        expected_failure_reason_code="SOURCE_EVIDENCE_UNAVAILABLE",
    )["recovery"] == "scheduled"
    restarted = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
    )
    requeued = restarted.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=105,
    )
    assert [item["work_item_id"] for item in requeued["items"]] == [
        open_work_item_id
    ]
    reclaimed = restarted.claim_review_work_item(
        principal=reviewer,
        work_item_id=open_work_item_id,
        expected_context=open_initial["command_context"],
        now=105,
    )
    assert reclaimed["claim_fence"] > open_claim["claim_fence"]
    assert restarted.release_review_work_item(
        principal=reviewer,
        work_item_id=open_work_item_id,
        expected_fence=reclaimed["claim_fence"],
        expected_context=open_initial["command_context"],
        idempotency_key="s03-rollback-reclaimed-release",
        now=106,
    )["status"] == "released"
    assert restarted.review_work_item_view(
        principal=reviewer,
        work_item_id=open_work_item_id,
        now=106,
    )["decision"] is None

    accepted_after = {
        "single": accepted_authority(
            restarted,
            single_application_id,
            single_work_item_id,
        ),
        "batch": accepted_authority(
            restarted,
            batch_application_id,
            batch_work_item_id,
        ),
    }
    assert accepted_after == accepted_before


def test_human_decision_binds_frozen_legacy_compatibility_summary(
    tmp_path: Path,
) -> None:
    legacy = RuleEngine(load_rules(ROOT / "configs" / "rules_auto_lease.yaml"))
    oracle_state = {"rewrite": False}
    returned_reports = []

    def mutable_legacy_oracle(application: object) -> object:
        report = legacy.run(application)  # type: ignore[arg-type]
        if oracle_state["rewrite"]:
            report.checks.clear()
        returned_reports.append(report)
        return report

    state_path = tmp_path / "target.sqlite3"
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
        legacy_oracle_runner=mutable_legacy_oracle,
    )
    integrator = S01CommandPrincipal(
        subject="s03-compatibility-reviewer",
        role="integrator",
        scope="C-DEMO",
        source_id="s03-legacy-adapter",
    )
    reviewer = S01CommandPrincipal(
        subject=integrator.subject,
        role="reviewer",
        scope=integrator.scope,
        source_id="s03-review-console",
    )
    admitted = service.submit_demo(
        principal=integrator,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s03-compatibility-admission",
    )
    assert admitted.application_id is not None
    assert admitted.source_sha256 is not None
    assert len(returned_reports) == 1

    oracle_state["rewrite"] = True
    returned_reports[0].checks.clear()
    processed = service.process_next_job()
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=101,
    )
    assert processed.status == "complete"
    assert processed.semantic_differential is not None
    assert processed.semantic_differential["status"] == "match"
    assert len(queue["items"]) == 1
    work_item_id = queue["items"][0]["work_item_id"]
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claim = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    accepted = service.submit_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claim["claim_fence"],
        expected_context=initial["command_context"],
        idempotency_key="s03-compatibility-decision",
        verification={
            "schema_version": "human-decision/1",
            "outcome": "confirmed",
            "reason_code": "HUMAN_REVIEW_COMPLETED",
            "finding_decisions": [
                {
                    "finding_id": finding["finding_id"],
                    "outcome": "confirmed",
                }
                for finding in initial["automatic_findings"]
            ],
        },
        now=102,
    )
    observed = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    )

    assert accepted["status"] == "accepted"
    assert observed["route"] == "human_complete"
    assert observed["decision"]["compatibility"] == {
        "schema_version": "human-review-compatibility/1",
        "differential_source": "frozen_admission_oracle",
        "intent": "manual_review",
        "target_reason_code": "HUMAN_REVIEW_COMPLETED",
        "conformance": "match",
        "target_context": {
            "run_id": processed.run_id,
            "evidence_snapshot_id": processed.evidence_snapshot_id,
            "release_id": processed.release_id,
            "source_sha256": admitted.source_sha256,
        },
        "fact_counts": {
            "legacy_checks": 13,
            "target_findings": 13,
            "checks_compared": 13,
            "mismatches": 0,
        },
        "semantic_differential_digest": observed["decision"]["compatibility"][
            "semantic_differential_digest"
        ],
    }
    assert len(
        observed["decision"]["compatibility"]["semantic_differential_digest"]
    ) == 64

    restarted = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=state_path,
        legacy_oracle_runner=mutable_legacy_oracle,
    )
    assert restarted.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=103,
    ) == observed
    assert len(returned_reports) == 1


def test_review_rejects_reason_outside_the_structured_allowlist(
    tmp_path: Path,
) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claim = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    before = {
        "work_item": service.review_work_item_view(
            principal=reviewer,
            work_item_id=work_item_id,
            now=102,
        ),
        "queue": service.queue_view(
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=102,
        ),
        "audit": service.audit_timeline(
            principal=auditor,
            application_id=application_id,
        ),
    }

    with pytest.raises(
        ValueError,
        match="review verification contains an invalid structured value",
    ):
        service.submit_review_work_item(
            principal=reviewer,
            work_item_id=work_item_id,
            expected_fence=claim["claim_fence"],
            expected_context=initial["command_context"],
            idempotency_key="s03-unknown-reason",
            verification={
                "schema_version": "human-decision/1",
                "outcome": "confirmed",
                "reason_code": "UNREGISTERED_REVIEW_REASON",
                "finding_decisions": [
                    {
                        "finding_id": finding["finding_id"],
                        "outcome": "confirmed",
                    }
                    for finding in initial["automatic_findings"]
                ],
            },
            now=102,
        )

    assert service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    ) == before["work_item"]
    assert service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=102,
    ) == before["queue"]
    assert service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    ) == before["audit"]


def test_review_note_persists_only_bounded_metadata_at_the_success_limit(
    tmp_path: Path,
) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claim = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    prefix = "S03_NOTE_SENTINEL_"
    note = prefix + "界" * 1000 + "x" * (1000 - len(prefix))
    note_bytes = note.encode("utf-8")
    assert len(note) == 2000
    assert len(note_bytes) == 4000

    accepted = service.submit_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_fence=claim["claim_fence"],
        expected_context=initial["command_context"],
        idempotency_key="s03-bounded-note",
        verification={
            "schema_version": "human-decision/1",
            "outcome": "confirmed",
            "reason_code": "HUMAN_REVIEW_COMPLETED",
            "finding_decisions": [
                {
                    "finding_id": finding["finding_id"],
                    "outcome": "confirmed",
                }
                for finding in initial["automatic_findings"]
            ],
            "note": note,
        },
        now=102,
    )
    observed = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    )
    audit = service.audit_timeline(
        principal=S01CommandPrincipal(
            subject="tenant-auditor",
            role="auditor",
            scope=reviewer.scope,
            source_id="s03-audit-console",
        ),
        application_id=application_id,
    )

    assert accepted["status"] == "accepted"
    assert observed["decision"]["note_metadata"] == {
        "present": True,
        "character_count": 2000,
        "byte_count": 4000,
        "sha256": hashlib.sha256(note_bytes).hexdigest(),
    }
    assert note not in repr({"result": accepted, "work_item": observed, "audit": audit})

    restarted = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
    )
    assert restarted.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=103,
    ) == observed


@pytest.mark.parametrize(
    "note",
    (
        "x" * 2001,
        "\U0001f600" * 1024 + "x",
    ),
)
def test_review_note_rejects_character_or_utf8_byte_overflow_atomically(
    tmp_path: Path,
    note: str,
) -> None:
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claim = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    before_work_item = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    )
    before_audit = service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    )

    with pytest.raises(
        ValueError,
        match="review verification contains an invalid structured value",
    ):
        service.submit_review_work_item(
            principal=reviewer,
            work_item_id=work_item_id,
            expected_fence=claim["claim_fence"],
            expected_context=initial["command_context"],
            idempotency_key="s03-note-overflow",
            verification={
                "schema_version": "human-decision/1",
                "outcome": "confirmed",
                "reason_code": "HUMAN_REVIEW_COMPLETED",
                "finding_decisions": [
                    {
                        "finding_id": finding["finding_id"],
                        "outcome": "confirmed",
                    }
                    for finding in initial["automatic_findings"]
                ],
                "note": note,
            },
            now=102,
        )

    assert service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    ) == before_work_item
    assert service.audit_timeline(
        principal=auditor,
        application_id=application_id,
    ) == before_audit


def test_public_review_dtos_recursively_exclude_raw_and_cross_scope_sentinels(
    tmp_path: Path,
) -> None:
    raw_sentinel = "S03_RAW_VALUE_SENTINEL"
    locator_sentinel = "S03_LOCATOR_PATH_SENTINEL"
    credential_sentinel = "S03_CREDENTIAL_SENTINEL"
    note_sentinel = "S03_COMPLETE_NOTE_SENTINEL"
    cross_scope_sentinel = "S03_CROSS_SCOPE_SENTINEL"
    result = deepcopy(_detection_result())
    detection = result["per_image_results"][0]["detections"][0]
    detection["ocr_text"] = raw_sentinel
    detection["value"] = raw_sentinel
    service, submission = _registered_service(tmp_path, result=result)
    submission["attachments"][0]["page_ref"] = locator_sentinel
    submission["producer"]["run_id"] = "s03-safe-producer-run"
    submission["producer"]["task_id"] = credential_sentinel

    admission = service.submit_registered(
        submission=submission,
        idempotency_key="s03-leak-single",
        principal=INTEGRATOR,
    )
    assert admission.application_id is not None
    assert service.process_next_job().status == "complete"
    service.refresh_projection()
    reviewer = S01CommandPrincipal(
        subject=INTEGRATOR.subject,
        role="reviewer",
        scope=INTEGRATOR.scope,
        source_id="s03-review-console",
    )
    _, batch_application_id, batch_work_item_id = add_review_application(
        service,
        submission,
        principal=INTEGRATOR,
        suffix="leak-batch",
    )
    cross_scope_integrator = S01CommandPrincipal(
        subject=cross_scope_sentinel,
        role="integrator",
        scope="C-DEMO",
        source_id="s03-cross-scope-adapter",
    )
    assert service.submit_demo(
        principal=cross_scope_integrator,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s03-cross-scope-admission",
    ).disposition is AdmissionDisposition.ACCEPTED
    assert service.process_next_job().status == "complete"
    service.refresh_projection()

    queue = service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=101,
    )
    single_work_item_id = next(
        item["work_item_id"]
        for item in queue["items"]
        if item["application_id"] == admission.application_id
    )
    single_initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=single_work_item_id,
        now=101,
    )
    single_claim = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=single_work_item_id,
        expected_context=single_initial["command_context"],
        now=101,
    )
    workspace = service.workspace_view(
        admission.application_id,
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=102,
    )
    single_result = service.submit_review_work_item(
        principal=reviewer,
        work_item_id=single_work_item_id,
        expected_fence=single_claim["claim_fence"],
        expected_context=single_initial["command_context"],
        idempotency_key="s03-leak-note",
        verification={
            "schema_version": "human-decision/1",
            "outcome": "confirmed",
            "reason_code": "HUMAN_REVIEW_COMPLETED",
            "finding_decisions": [
                {
                    "finding_id": finding["finding_id"],
                    "outcome": "confirmed",
                }
                for finding in single_initial["automatic_findings"]
            ],
            "note": note_sentinel,
        },
        now=102,
    )
    single_after = service.review_work_item_view(
        principal=reviewer,
        work_item_id=single_work_item_id,
        now=102,
    )

    batch_initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=batch_work_item_id,
        now=101,
    )
    batch_claim = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=batch_work_item_id,
        expected_context=batch_initial["command_context"],
        now=101,
    )
    batch_plan = service.preview_review_work_item_batch(
        principal=reviewer,
        items=review_batch_items(
            batch_initial,
            expected_fence=batch_claim["claim_fence"],
        ),
        now=102,
    )
    batch_result = service.submit_review_work_item_batch(
        principal=reviewer,
        idempotency_key="s03-leak-batch",
        plan=batch_plan,
        now=102,
    )
    batch_after = service.review_work_item_view(
        principal=reviewer,
        work_item_id=batch_work_item_id,
        now=102,
    )
    auditor = S01CommandPrincipal(
        subject="tenant-auditor",
        role="auditor",
        scope=reviewer.scope,
        source_id="s03-audit-console",
    )
    surfaces = {
        "queue": queue,
        "work_item": single_after,
        "workspace": workspace,
        "lifecycle_result": single_result,
        "audit": service.audit_timeline(
            principal=auditor,
            application_id=admission.application_id,
        ),
        "batch_preview": batch_plan,
        "batch_result": batch_result,
        "batch_work_item": batch_after,
        "batch_audit": service.audit_timeline(
            principal=auditor,
            application_id=batch_application_id,
        ),
    }

    def strings(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [
                text
                for key, item in value.items()
                for text in (*strings(key), *strings(item))
            ]
        if isinstance(value, (list, tuple)):
            return [text for item in value for text in strings(item)]
        return []

    public_strings = strings(surfaces)
    for sentinel in (
        raw_sentinel,
        locator_sentinel,
        credential_sentinel,
        note_sentinel,
        cross_scope_sentinel,
    ):
        assert sentinel not in public_strings
    evidence_links = [
        link
        for finding in workspace["mandatory_blockers"]
        for link in finding["evidence_links"]
    ]
    assert evidence_links
    assert all(link["raw_masked"] == "[REDACTED]" for link in evidence_links)


def test_batch_human_decisions_bind_legacy_compatibility_summary(
    tmp_path: Path,
) -> None:
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
    )
    integrator = S01CommandPrincipal(
        subject="s03-batch-compatibility-integrator",
        role="integrator",
        scope="C-DEMO",
        source_id="s03-legacy-adapter",
    )
    reviewer = S01CommandPrincipal(
        subject=integrator.subject,
        role="reviewer",
        scope=integrator.scope,
        source_id="s03-review-console",
    )
    admitted = service.submit_demo(
        principal=integrator,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s03-batch-compatibility-admission",
    )
    assert admitted.application_id is not None
    processed = service.process_next_job()
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=101,
    )
    work_item_id = queue["items"][0]["work_item_id"]
    initial = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=101,
    )
    claim = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=initial["command_context"],
        now=101,
    )
    plan = service.preview_review_work_item_batch(
        principal=reviewer,
        items=review_batch_items(initial, expected_fence=claim["claim_fence"]),
        now=102,
    )
    result = service.submit_review_work_item_batch(
        principal=reviewer,
        idempotency_key="s03-batch-compatibility-decision",
        plan=plan,
        now=102,
    )
    observed = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item_id,
        now=102,
    )

    assert processed.status == "complete"
    assert result["status"] == "accepted"
    assert observed["decisions"]
    for decision in observed["decisions"]:
        compatibility = decision["compatibility"]
        assert compatibility["conformance"] == "match"
        assert compatibility["target_context"] == {
            "run_id": processed.run_id,
            "evidence_snapshot_id": processed.evidence_snapshot_id,
            "release_id": processed.release_id,
            "source_sha256": admitted.source_sha256,
        }
