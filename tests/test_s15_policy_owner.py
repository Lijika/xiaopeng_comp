"""Focused S15 R2 authority, audit, C19 and legacy-boundary tests."""
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
from task4_consistency.controlled.s08 import PolicyGovernanceService
from tests.test_s01_http import UvicornLoopback
from tests.test_s02_http import _configured_http_source, _open_session
from tests.test_s02_controlled import INTEGRATOR, ROOT, TENANT_SCOPE, _registered_service


NOW = 1_800_000_000


def _governed_ready(tmp_path: Path) -> tuple[ControlledScenarioService, S01CommandPrincipal, str, str]:
    """Registered R-OBSERVED work item with the same store backed by an
    approved/activated S08 release.  No local C19 JSON participates in this
    authority; the release checker artifact carries the immutable reveal
    policy and term."""
    service, submission = _registered_service(tmp_path)
    governance = PolicyGovernanceService(
        state_path=tmp_path / "target.sqlite3",
        source_rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        source_kb_path=ROOT / "configs" / "kb" / "entity_kb.json",
        corpus_root=ROOT / "fixtures" / "applications",
        clock=lambda: NOW,
    )
    bootstrapped = governance.bootstrap_once()
    assert bootstrapped["status"] in {"activated", "already_active"}
    service._policy_governance = governance
    reviewer = S01CommandPrincipal(
        subject=INTEGRATOR.subject,
        role="reviewer",
        scope=TENANT_SCOPE,
        source_id="s15-review-console",
        expires_at=float(NOW + 10_000),
    )
    admitted = service.submit_registered(
        submission=submission,
        idempotency_key="s15-governed-admission",
        principal=INTEGRATOR,
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    assert admitted.application_id is not None
    completed = service.process_next_job()
    assert completed.status == "complete"
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=NOW,
    )
    assert len(queue["items"]) == 1
    return service, reviewer, admitted.application_id, queue["items"][0]["work_item_id"]


def _claimed_reveal_context(tmp_path: Path):
    service, reviewer, application_id, work_item_id = _governed_ready(tmp_path)
    now = NOW
    work = service.review_work_item_view(
        principal=reviewer, work_item_id=work_item_id, now=now
    )
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=work["command_context"],
        now=now,
    )
    workspace = service.workspace_view(
        application_id,
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=now,
    )
    link = workspace["selected_finding"]["evidence_links"][0]
    return service, reviewer, application_id, work_item_id, work, claimed, link, now


def _reveal_args(work, claimed, link, **overrides):
    return {
        "observation_id": link["observation_id"],
        "expected_fence": claimed["claim_fence"],
        "expected_context": work["command_context"],
        "idempotency_key": "s15-reveal-key",
        "purpose": "MANUAL_REVIEW",
        "reason": "EVIDENCE_VERIFICATION",
        "classification": "RESTRICTED",
        "expected_source_region": link["source_region"],
        **overrides,
    }


def _auditor(scope: str) -> S01CommandPrincipal:
    return S01CommandPrincipal(
        subject="s15-auditor", role="auditor", scope=scope, source_id="s15-audit"
    )


def test_governed_c19_release_authorizes_registered_reveal_for_tenant_resource(tmp_path: Path) -> None:
    service, reviewer, application_id, work_item_id, work, claimed, link, now = _claimed_reveal_context(tmp_path)
    decision = service._resolve_governed_c19_reveal_policy(
        principal=reviewer,
        app=service._store.applications[application_id],
        now=NOW,
    )
    assert decision is not None
    assert decision["policy_id"] == "s15-controlled-reveal/1"
    assert decision["policy_version"] == "1"
    assert len(decision["policy_digest"]) == 64
    assert decision["tenant_id"] == "tenant-test"
    assert decision["resource_id"] == application_id
    assert decision["max_term_seconds"] == 900
    result = service.reveal_field_observation(
        principal=reviewer,
        application_id=application_id,
        work_item_id=work_item_id,
        now=NOW,
        **_reveal_args(work, claimed, link),
    )
    assert result["status"] == "revealed"
    assert result["claim_expires_at"] == NOW + 900  # min(identity, claim, release term)


def test_c19_release_denial_and_term_bound_close_reveal(tmp_path: Path) -> None:
    service, reviewer, application_id, work_item_id, work, claimed, link, now = _claimed_reveal_context(tmp_path)
    wrong_tenant = S01CommandPrincipal(
        subject=reviewer.subject,
        role="reviewer",
        scope="R-OBSERVED/another-tenant",
        source_id=reviewer.source_id,
        expires_at=reviewer.expires_at,
    )
    with pytest.raises(QueryNotFound):
        service.reveal_field_observation(
            principal=wrong_tenant,
            application_id=application_id,
            work_item_id=work_item_id,
            now=NOW,
            **_reveal_args(work, claimed, link),
        )
    # Session expiry is the smallest term, so it closes before claim expiry.
    short_identity = S01CommandPrincipal(
        subject=reviewer.subject,
        role="reviewer",
        scope=reviewer.scope,
        source_id=reviewer.source_id,
        expires_at=NOW,
    )
    with pytest.raises(QueryNotFound):
        service.reveal_field_observation(
            principal=short_identity,
            application_id=application_id,
            work_item_id=work_item_id,
            now=NOW,
            **_reveal_args(work, claimed, link, idempotency_key="s15-term-expired"),
        )


def test_registered_reveal_returns_requested_public_region_without_bbox_locator(tmp_path: Path) -> None:
    service, reviewer, application_id, work_item_id, work, claimed, link, now = _claimed_reveal_context(tmp_path)
    assert link["source_region"] == "region:1"
    result = service.reveal_field_observation(
        principal=reviewer,
        application_id=application_id,
        work_item_id=work_item_id,
        now=NOW,
        **_reveal_args(work, claimed, link),
    )
    serialized = json.dumps(result, sort_keys=True)
    assert result["source_location"]["source_region"] == link["source_region"]
    assert "bbox:[" not in serialized
    assert "source_pointer" not in serialized
    timeline = service.audit_timeline(
        principal=_auditor(reviewer.scope), application_id=application_id
    )
    assert "bbox:[" not in json.dumps(timeline, sort_keys=True)
    assert "region:1" not in json.dumps(timeline, sort_keys=True)
    mismatch = service.reveal_field_observation(
        principal=reviewer,
        application_id=application_id,
        work_item_id=work_item_id,
        now=NOW + 1,
        **_reveal_args(work, claimed, link, idempotency_key="s15-region-mismatch", expected_source_region="region:2"),
    )
    assert mismatch["status"] == "rejected"
    assert mismatch["reason_code"] == "REVEAL_REGION_MISMATCH"
    assert "bbox:[" not in json.dumps(mismatch)


def test_reveal_failure_outcomes_are_audited_once_without_values(tmp_path: Path) -> None:
    service, reviewer, application_id, work_item_id, work, claimed, link, now = _claimed_reveal_context(tmp_path)
    failures = [
        service.reveal_field_observation(
            principal=reviewer, application_id=application_id, work_item_id=work_item_id,
            now=NOW, **_reveal_args(work, claimed, link, idempotency_key="s15-stale", expected_context={}),
        ),
        service.reveal_field_observation(
            principal=reviewer, application_id=application_id, work_item_id=work_item_id,
            now=NOW, **_reveal_args(work, claimed, link, idempotency_key="s15-region", expected_source_region="region:2"),
        ),
        service.reveal_field_observation(
            principal=reviewer, application_id=application_id, work_item_id=work_item_id,
            now=NOW, **_reveal_args(work, claimed, link, idempotency_key="s15-vocab", classification="UNKNOWN"),
        ),
    ]
    assert [item["status"] for item in failures] == ["stale", "rejected", "rejected"]
    timeline = service.audit_timeline(principal=_auditor(reviewer.scope), application_id=application_id)
    events = [event for event in timeline["events"] if event["action"] == "evidence_source_revealed"]
    assert len(events) == 3
    body = json.dumps(events, sort_keys=True)
    for forbidden in ("TEST-VIN-A", "bbox:[", "source_pointer", "source_object_ref", "expected_source_region"):
        assert forbidden not in body
    assert {event["context"]["reason_code"] for event in events} == {
        "STALE_REVIEW_CONTEXT", "REVEAL_REGION_MISMATCH", "REVEAL_VOCABULARY_UNKNOWN"
    }


def test_reveal_audit_fault_is_atomic_and_returns_unavailable(tmp_path: Path) -> None:
    service, reviewer, application_id, work_item_id, work, claimed, link, now = _claimed_reveal_context(tmp_path)
    before = copy.deepcopy(service._store.applications[application_id])
    def fail(point: str) -> None:
        if point == "reveal.audit":
            raise OSError("audit down")
    service._fault_injector = fail
    result = service.reveal_field_observation(
        principal=reviewer, application_id=application_id, work_item_id=work_item_id,
        now=NOW, **_reveal_args(work, claimed, link),
    )
    assert result["status"] == "unavailable"
    assert "source_text" not in result
    after = service._store.applications[application_id]
    assert after["lifecycle_revision"] == before["lifecycle_revision"]
    assert after["evidence_revision"] == before["evidence_revision"]


def test_failed_reveal_replay_and_conflict_keep_one_audit_fact(tmp_path: Path) -> None:
    service, reviewer, application_id, work_item_id, work, claimed, link, now = _claimed_reveal_context(tmp_path)
    first = service.reveal_field_observation(
        principal=reviewer, application_id=application_id, work_item_id=work_item_id,
        now=NOW, **_reveal_args(work, claimed, link, idempotency_key="s15-one", classification="UNKNOWN"),
    )
    replay = service.reveal_field_observation(
        principal=reviewer, application_id=application_id, work_item_id=work_item_id,
        now=NOW, **_reveal_args(work, claimed, link, idempotency_key="s15-one", classification="UNKNOWN"),
    )
    conflict = service.reveal_field_observation(
        principal=reviewer, application_id=application_id, work_item_id=work_item_id,
        now=NOW, **_reveal_args(work, claimed, link, idempotency_key="s15-one", expected_source_region="region:2"),
    )
    assert first["status"] == "rejected"
    assert replay == {**first, "replayed": True}
    assert conflict["status"] == "conflict"
    events = [event for event in service.audit_timeline(principal=_auditor(reviewer.scope), application_id=application_id)["events"] if event["action"] == "evidence_source_revealed"]
    # failure + conflict (the same-fingerprint replay creates no second event)
    assert len(events) == 2

def test_registered_session_cannot_reach_legacy_raw_routes(tmp_path: Path) -> None:
    environment, _submission = _configured_http_source(tmp_path)
    with UvicornLoopback(environment, app_target="task4_consistency.web.app:create_s02_test_app", app_factory=True) as server:
        cookie = _open_session(server)
        fixture = server.request(
            "GET",
            "/api/fixtures/app_r53_bad_engine.json",
            headers={"Cookie": cookie},
            use_session=False,
        )
        arbitrary = server.request(
            "POST",
            "/api/check",
            body={"application": {"application_id": "cross-tenant-raw", "documents": []}},
            headers={"Cookie": cookie},
            use_session=False,
        )
    assert fixture.status == 404
    assert arbitrary.status == 404
    assert fixture.json() == {"detail": {"error": "SYNTHETIC_ONLY"}}
    assert arbitrary.json() == {"detail": {"error": "SYNTHETIC_ONLY"}}
    assert "cross-tenant-raw" not in arbitrary.text


def test_legacy_demo_check_accepts_only_manifest_fixture_identity(tmp_path: Path) -> None:
    # The legacy route is retained only as a synthetic manifest adapter.
    # The exact fixture payload is accepted; an arbitrary raw Application and
    # caller-selected rules path are existence-hidden.
    fixture_path = ROOT / "fixtures" / "applications" / "app_r53_bad_engine.json"
    fixture_payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    with UvicornLoopback() as server:
        accepted = server.request(
            "POST",
            "/api/check",
            body={"application": fixture_payload},
            use_session=False,
        )
        arbitrary = server.request(
            "POST",
            "/api/check",
            body={
                "application": {"application_id": "arbitrary", "documents": []},
                "rules_path": "configs/rules_auto_lease.yaml",
            },
            use_session=False,
        )
    assert accepted.status == 200
    assert accepted.headers.get("cache-control") == "no-store"
    assert arbitrary.status == 404
    assert arbitrary.json() == {"detail": {"error": "SYNTHETIC_ONLY"}}
