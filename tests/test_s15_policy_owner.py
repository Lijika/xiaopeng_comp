"""Focused S15 R2 authority, audit, C19 and legacy-boundary tests."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import sqlite3

from task4_consistency.controlled.s01 import (
    AdmissionDisposition,
    ControlledScenarioService,
    QueryNotFound,
    S01CommandPrincipal,
    _StoreWriteFailure,
)
from task4_consistency.controlled.s08 import PolicyGovernanceService
from task4_consistency.web.app import S01RevealResult, S01ReviewRevealBody
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


def _persisted_baseline(service: ControlledScenarioService) -> dict[str, object]:
    """Deep snapshot of the complete persisted-authority state.  Callers take
    it after a reload so it reflects the database, and compare it again after
    a later reload to prove the database is unchanged."""
    store = service._store
    return {
        "store_revision": store._store_revision,
        "projections": copy.deepcopy(store.projections),
        "applications": copy.deepcopy(store.applications),
        "lifecycle_events": copy.deepcopy(store.lifecycle_events),
        "evidence_events": copy.deepcopy(store.evidence_events),
        "review_records": copy.deepcopy(store.review_records),
        "work_items": copy.deepcopy(store.work_items),
        "audit_events": copy.deepcopy(store.audit_events),
        "idempotency": copy.deepcopy(store.idempotency),
    }


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


def test_governed_c19_vocabulary_is_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, reviewer, application_id, work_item_id, work, claimed, link, now = (
        _claimed_reveal_context(tmp_path)
    )
    decision = service._resolve_governed_c19_reveal_policy(
        principal=reviewer,
        app=service._store.applications[application_id],
        now=now,
    )
    assert decision is not None
    governed = {
        **decision,
        "purposes": ("CASE_TRIAGE",),
        "reasons": ("SOURCE_CONFIRMATION",),
        "classifications": ("LIMITED",),
    }
    monkeypatch.setattr(
        service,
        "_resolve_governed_c19_reveal_policy",
        lambda **_kwargs: governed,
    )
    body = S01ReviewRevealBody.model_validate(
        {
            "application_id": application_id,
            **_reveal_args(
                work,
                claimed,
                link,
                purpose="CASE_TRIAGE",
                reason="SOURCE_CONFIRMATION",
                classification="LIMITED",
            ),
        }
    )
    result = service.reveal_field_observation(
        principal=reviewer,
        application_id=body.application_id,
        work_item_id=work_item_id,
        observation_id=body.observation_id,
        expected_fence=body.expected_fence,
        expected_context=body.expected_context.model_dump(mode="json"),
        idempotency_key=body.idempotency_key,
        purpose=body.purpose,
        reason=body.reason,
        classification=body.classification,
        expected_source_region=body.expected_source_region,
        now=now,
    )
    assert result["status"] == "revealed"
    assert (result["purpose"], result["reason"], result["classification"]) == (
        "CASE_TRIAGE",
        "SOURCE_CONFIRMATION",
        "LIMITED",
    )
    response = S01RevealResult.model_validate(result)
    assert (response.purpose, response.reason, response.classification) == (
        "CASE_TRIAGE",
        "SOURCE_CONFIRMATION",
        "LIMITED",
    )


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


def test_successful_reveal_replay_after_effective_expiry_returns_no_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ControlledScenarioService, "_REVIEW_CLAIM_TTL_SECONDS", 3_600
    )
    service, reviewer, application_id, work_item_id, work, claimed, link, now = (
        _claimed_reveal_context(tmp_path)
    )
    first = service.reveal_field_observation(
        principal=reviewer,
        application_id=application_id,
        work_item_id=work_item_id,
        now=now,
        **_reveal_args(work, claimed, link, idempotency_key="s15-expired-success"),
    )
    replay = service.reveal_field_observation(
        principal=reviewer,
        application_id=application_id,
        work_item_id=work_item_id,
        now=now + 901,
        **_reveal_args(work, claimed, link, idempotency_key="s15-expired-success"),
    )
    assert first["status"] == "revealed"
    assert first["claim_expires_at"] == now + 900
    assert replay["status"] == "stale"
    assert replay["reason_code"] == "REVEAL_TERM_EXPIRED"
    assert "source_text" not in replay


def test_reveal_audit_projection_excludes_caller_idempotency_text(tmp_path: Path) -> None:
    service, reviewer, application_id, work_item_id, work, claimed, link, now = (
        _claimed_reveal_context(tmp_path)
    )
    marker = "idempotency-audit-sentinel"
    result = service.reveal_field_observation(
        principal=reviewer,
        application_id=application_id,
        work_item_id=work_item_id,
        now=now,
        **_reveal_args(work, claimed, link, idempotency_key=marker),
    )
    timeline = service.audit_timeline(
        principal=_auditor(reviewer.scope), application_id=application_id
    )
    assert result["status"] == "revealed"
    assert marker not in json.dumps(timeline, sort_keys=True)

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
        malformed = server.raw_request(
            "POST",
            "/api/check",
            body=b'{"fixture_id":',
            headers={"Cookie": cookie},
            use_session=False,
        )
        bulk = server.request(
            "POST",
            "/api/check/batch",
            body={
                "applications": [
                    {"application_id": "cross-tenant-raw", "documents": []}
                ]
            },
            headers={"Cookie": cookie},
            use_session=False,
        )
        rules_path = server.request(
            "POST",
            "/api/check",
            body={
                "fixture_id": "app_r53_bad_engine",
                "rules_path": "configs/rules_auto_lease.yaml",
            },
            headers={"Cookie": cookie},
            use_session=False,
        )
    assert fixture.status == 404
    assert arbitrary.status == 404
    assert malformed.status == 404
    assert bulk.status == 404
    assert rules_path.status == 404
    assert fixture.json() == {"detail": {"error": "SYNTHETIC_ONLY"}}
    assert arbitrary.json() == {"detail": {"error": "SYNTHETIC_ONLY"}}
    assert malformed.json() == {"detail": {"error": "SYNTHETIC_ONLY"}}
    assert bulk.json() == {"detail": {"error": "SYNTHETIC_ONLY"}}
    assert rules_path.json() == {"detail": {"error": "SYNTHETIC_ONLY"}}
    assert "cross-tenant-raw" not in arbitrary.text


def test_legacy_demo_check_accepts_only_manifest_fixture_identity(tmp_path: Path) -> None:
    # The legacy route is retained only as a synthetic manifest adapter.
    # The fixed fixture identity is accepted, an unknown identity is hidden,
    # and the closed request envelope declines a caller-selected rules path.
    with UvicornLoopback() as server:
        accepted = server.request(
            "POST",
            "/api/check",
            body={"fixture_id": "app_r53_bad_engine"},
            use_session=False,
        )
        unknown = server.request(
            "POST",
            "/api/check",
            body={"fixture_id": "arbitrary"},
            use_session=False,
        )
        caller_rules = server.request(
            "POST",
            "/api/check",
            body={
                "fixture_id": "app_r53_bad_engine",
                "rules_path": "configs/rules_auto_lease.yaml",
            },
            use_session=False,
        )
    assert accepted.status == 200
    assert accepted.headers.get("cache-control") == "no-store"
    assert unknown.status == 404
    assert unknown.json() == {"detail": {"error": "SYNTHETIC_ONLY"}}
    assert caller_rules.status == 422
    assert "rules_auto_lease.yaml" not in caller_rules.text


def test_ineligible_observation_cannot_reveal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2: isolated phases.

    Phase 1/2 mutate finding/link metadata to False / missing and inject
    raising guards on _admitted_evidence, _assemble_evidence and read_object
    inside a monkeypatch.context(), proving the metadata-only guard runs
    before any raw-bearing evidence load.  A code-shaped caller sentinel must
    not appear in the response, the audit, or the replay storage.

    Phase 3 restores real reload and real immutable persistence, injects a
    distinct sibling observation with its own object reference, and asserts
    the eligible reveal reads exactly the result object and the selected
    observation object (zero sibling reads).  The audit and idempotency
    binding must survive a real reload.
    """
    service, reviewer, application_id, work_item_id, work, claimed, link, now = (
        _claimed_reveal_context(tmp_path)
    )
    before = copy.deepcopy(service._store.applications[application_id])
    orig_read = service._registered_source_boundary.read_object
    orig_admitted = service._admitted_evidence
    store_cls = service._store.__class__
    sentinel = "CALLER_R2_SENTINEL_9"

    def _mutate_links(eligible: bool | None) -> None:
        mutated = 0
        for finding in service._store.findings:
            if finding.get("application_id") != application_id:
                continue
            for ev_link in finding.get("evidence_links", []) or []:  # type: ignore[union-attr]
                if not isinstance(ev_link, dict):
                    continue
                if ev_link.get("observation_id") != link["observation_id"]:
                    continue
                if eligible is None:
                    ev_link.pop("evidence_eligible", None)  # type: ignore[attr-defined]
                    ev_link.pop("eligibility_reason", None)  # type: ignore[attr-defined]
                else:
                    ev_link["evidence_eligible"] = eligible  # type: ignore[index]
                    ev_link["eligibility_reason"] = (  # type: ignore[index]
                        "PROVENANCE_MISSING"
                        if not eligible
                        else "REGISTERED_SOURCE_PROVENANCE_VERIFIED"
                    )
                mutated += 1
        assert mutated >= 1, "no link mutated"

    # ---- Phase 1: link evidence_eligible False (metadata-only guard) ----
    with monkeypatch.context() as ctx:
        _mutate_links(eligible=False)
        ctx.setattr(service, "_reload_store", lambda: None)  # type: ignore[method-assign]
        ctx.setattr(
            store_cls,
            "_sync_immutable_rows",
            classmethod(lambda cls, connection, table, staged, cache: None),  # type: ignore[assignment]
        )
        ctx.setattr(
            service,
            "_admitted_evidence",
            lambda _app: (_ for _ in ()).throw(
                AssertionError("_admitted_evidence must not be called for ineligible link")
            ),
        )
        ctx.setattr(
            service,
            "_assemble_evidence",
            lambda _evidence: (_ for _ in ()).throw(
                AssertionError("_assemble_evidence must not be called for ineligible link")
            ),
        )
        ctx.setattr(
            service._registered_source_boundary,
            "read_object",
            lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("read_object must not be called for ineligible link")
            ),
        )
        result = service.reveal_field_observation(
            principal=reviewer,
            application_id=application_id,
            work_item_id=work_item_id,
            now=NOW,
            **_reveal_args(
                work,
                claimed,
                link,
                idempotency_key="s15-ineligible-false",
                purpose=sentinel,
                reason=sentinel,
                classification=sentinel,
            ),
        )
        assert result["status"] == "rejected"
        assert result["reason_code"] == "SOURCE_REVEAL_UNAVAILABLE"
        assert "source_text" not in result
        assert "source_location" not in result
        # Code-shaped caller codes never enter the response or replay storage.
        assert sentinel not in json.dumps(result, sort_keys=True)
        assert sentinel not in str(service._store.idempotency)
        timeline = service.audit_timeline(
            principal=_auditor(reviewer.scope), application_id=application_id
        )
        events = [
            e
            for e in timeline["events"]
            if e["action"] == "evidence_source_revealed"
        ]
        assert len(events) == 1
        body = json.dumps(events, sort_keys=True)
        assert sentinel not in body
        assert "TEST-VIN-A" not in body
        assert "bbox:[" not in body
        assert "source_object_ref" not in body
        # Vocabulary was not governed yet: no caller-controlled purpose is
        # persisted at all.
        assert "purpose" not in events[0]["context"]
    # Context exit restores real reload and real immutable sync.  The
    # in-memory audit fact of phase 1 is intentionally NOT reloaded away
    # before phase 2 so the two rejected facts can be counted together;
    # phase 2 keeps the same no-op reload inside its own context.
    # ---- Phase 2: missing evidence_eligible key (metadata-only guard) ----
    with monkeypatch.context() as ctx:
        _mutate_links(eligible=None)
        ctx.setattr(service, "_reload_store", lambda: None)  # type: ignore[method-assign]
        ctx.setattr(
            store_cls,
            "_sync_immutable_rows",
            classmethod(lambda cls, connection, table, staged, cache: None),  # type: ignore[assignment]
        )
        ctx.setattr(
            service,
            "_admitted_evidence",
            lambda _app: (_ for _ in ()).throw(
                AssertionError("_admitted_evidence must not be called for missing eligibility")
            ),
        )
        ctx.setattr(
            service,
            "_assemble_evidence",
            lambda _evidence: (_ for _ in ()).throw(
                AssertionError("_assemble_evidence must not be called for missing eligibility")
            ),
        )
        ctx.setattr(
            service._registered_source_boundary,
            "read_object",
            lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("read_object must not be called for missing eligibility")
            ),
        )
        second = service.reveal_field_observation(
            principal=reviewer,
            application_id=application_id,
            work_item_id=work_item_id,
            now=NOW + 1,
            **_reveal_args(
                work,
                claimed,
                link,
                idempotency_key="s15-ineligible-missing",
                purpose=sentinel,
                reason=sentinel,
                classification=sentinel,
            ),
        )
        assert second["status"] == "rejected"
        assert second["reason_code"] == "SOURCE_REVEAL_UNAVAILABLE"
        assert "source_text" not in second
        assert sentinel not in json.dumps(second, sort_keys=True)
        assert sentinel not in str(service._store.idempotency)
        timeline2 = service.audit_timeline(
            principal=_auditor(reviewer.scope), application_id=application_id
        )
        events2 = [
            e
            for e in timeline2["events"]
            if e["action"] == "evidence_source_revealed"
        ]
        assert len(events2) == 2
        assert sentinel not in json.dumps(events2, sort_keys=True)
        assert "purpose" not in events2[1]["context"]
    service._reload_store()

    # ---- Phase 3: eligible path with real reload + real immutable
    # persistence; a distinct sibling observation proves no bulk read. ----
    with monkeypatch.context() as ctx:
        real_evidence = orig_admitted(service._store.applications[application_id])
        result_ref = service._store.applications[application_id].get("source", {}).get(
            "source_result_object_ref"
        )
        target_ref = None
        for doc in real_evidence:
            for obs in doc.get("observations", []) or []:  # type: ignore[union-attr]
                if (
                    isinstance(obs, dict)
                    and obs.get("observation_id") == link["observation_id"]
                ):
                    target_ref = obs.get("source_object_ref")
        assert isinstance(result_ref, str)
        assert isinstance(target_ref, str)
        sibling_ref = "sibling-object"
        sibling_obs = None
        for doc in real_evidence:
            for obs in doc.get("observations", []) or []:  # type: ignore[union-attr]
                if (
                    isinstance(obs, dict)
                    and obs.get("observation_id") == link["observation_id"]
                ):
                    sibling_obs = copy.deepcopy(obs)
        assert sibling_obs is not None
        sibling_obs["observation_id"] = "sibling_r2_observation"
        sibling_obs["source_object_ref"] = sibling_ref

        def _with_sibling(app: dict[str, object]) -> list[dict[str, object]]:  # type: ignore[no-untyped-def]
            evidence = orig_admitted(app)  # type: ignore[arg-type]
            for doc in evidence:
                observations = doc.get("observations", []) or []  # type: ignore[union-attr]
                if any(
                    isinstance(obs, dict)
                    and obs.get("observation_id") == link["observation_id"]
                    for obs in observations  # type: ignore[union-attr]
                ):
                    doc["observations"] = [*observations, copy.deepcopy(sibling_obs)]  # type: ignore[index]
                    break
            return evidence

        ctx.setattr(service, "_admitted_evidence", _with_sibling)
        read_calls: list[str] = []

        def _counting_read(*, tenant_id: str, source_system_id: str, object_ref: str) -> bytes:  # type: ignore[no-untyped-def]
            read_calls.append(object_ref)
            return orig_read(
                tenant_id=tenant_id,
                source_system_id=source_system_id,
                object_ref=object_ref,
            )

        ctx.setattr(service._registered_source_boundary, "read_object", _counting_read)
        ok = service.reveal_field_observation(
            principal=reviewer,
            application_id=application_id,
            work_item_id=work_item_id,
            now=NOW + 2,
            **_reveal_args(work, claimed, link, idempotency_key="s15-eligible-targeted"),
        )
        assert ok["status"] == "revealed"
        # Exactly the result object and the selected observation object, in
        # that order; zero sibling reads and no other evidence objects.
        assert read_calls == [result_ref, target_ref], f"unexpected reads: {read_calls}"
        assert sibling_ref not in read_calls
    # Real reload: audit and idempotency binding must survive.
    service._reload_store()
    replay = service.reveal_field_observation(
        principal=reviewer,
        application_id=application_id,
        work_item_id=work_item_id,
        now=NOW + 3,
        **_reveal_args(work, claimed, link, idempotency_key="s15-eligible-targeted"),
    )
    assert replay["replayed"] is True
    assert replay["status"] == "revealed"
    timeline3 = service.audit_timeline(
        principal=_auditor(reviewer.scope), application_id=application_id
    )
    events3 = [
        e
        for e in timeline3["events"]
        if e["action"] == "evidence_source_revealed"
    ]
    # Only the eligible success was persisted through the real immutable
    # path; the in-memory rejected facts from phases 1-2 are gone and the
    # replay added no second audit fact.
    assert len(events3) == 1
    assert events3[0]["result"] == "revealed"
    assert sentinel not in json.dumps(events3, sort_keys=True)
    after = service._store.applications[application_id]
    assert after["lifecycle_revision"] == before["lifecycle_revision"]
    assert after["evidence_revision"] == before["evidence_revision"]


def test_reveal_admitted_evidence_damage_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R7: _admitted_evidence authority damage at the reveal's own evidence
    load (after the C19 governed decision) maps to exactly
    stopped/SOURCE_EVIDENCE_UNAVAILABLE with one safe audit carrying the
    governed MANUAL_REVIEW / EVIDENCE_VERIFICATION / RESTRICTED vocabulary,
    no raw, no business revision change, and stable replay."""
    service, reviewer, application_id, work_item_id, work, claimed, link, now = (
        _claimed_reveal_context(tmp_path)
    )
    before = copy.deepcopy(service._store.applications[application_id])

    original_admitted = service._admitted_evidence
    admitted_calls = {"n": 0}

    def _admitted_then_broken(app: dict[str, object]) -> list[dict[str, object]]:  # type: ignore[no-untyped-def]
        # Verified runtime call graph (probe: 2 invocations per registered
        # reveal): call 1 comes from _review_current_context ->
        # _require_application_state_authority (s01.py:10022 -> :24936) and
        # must succeed so the reveal reaches the C19 governed decision;
        # call 2 is the reveal's own evidence load (s01.py:11926), where the
        # digest failure fires after governed_vocabulary was confirmed.
        admitted_calls["n"] += 1
        if admitted_calls["n"] >= 2:
            raise RuntimeError("evidence event digest mismatch")
        return original_admitted(app)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_admitted_evidence", _admitted_then_broken)
    # Governed vocabulary (the _reveal_args defaults) is required for the
    # C19 decision to confirm before the fault; caller-sentinel leak proofs
    # live in the pre-C19 eligibility/region cases.
    result = service.reveal_field_observation(
        principal=reviewer,
        application_id=application_id,
        work_item_id=work_item_id,
        now=NOW,
        **_reveal_args(work, claimed, link, idempotency_key="s15-admitted-damage"),
    )
    assert admitted_calls["n"] == 2
    assert result["status"] == "stopped"
    assert result["reason_code"] == "SOURCE_EVIDENCE_UNAVAILABLE"
    assert "source_text" not in result
    assert "source_location" not in result
    after = service._store.applications[application_id]
    assert after["lifecycle_revision"] == before["lifecycle_revision"]
    assert after["evidence_revision"] == before["evidence_revision"]
    timeline = service.audit_timeline(
        principal=_auditor(reviewer.scope), application_id=application_id
    )
    events = [
        e
        for e in timeline["events"]
        if e["action"] == "evidence_source_revealed"
    ]
    assert len(events) == 1
    assert events[0]["result"] == "stopped"
    body = json.dumps(events, sort_keys=True)
    assert "source_text" not in body
    assert "bbox:[" not in body
    assert "TEST-VIN-A" not in body
    # The fault fires after the C19 decision, so the audit carries the exact
    # governed vocabulary and the authentic unchanged revisions.
    assert events[0]["context"]["schema_version"] == "s15-reveal-audit/2"
    assert events[0]["context"]["purpose"] == "MANUAL_REVIEW"
    assert events[0]["context"]["verification_reason"] == "EVIDENCE_VERIFICATION"
    assert events[0]["context"]["classification"] == "RESTRICTED"
    assert events[0]["context"]["lifecycle_revision"] == before["lifecycle_revision"]
    assert events[0]["context"]["evidence_revision"] == before["evidence_revision"]
    # Idempotent replay of the stopped outcome after a real reload.
    service._reload_store()
    replay = service.reveal_field_observation(
        principal=reviewer,
        application_id=application_id,
        work_item_id=work_item_id,
        now=NOW,
        **_reveal_args(work, claimed, link, idempotency_key="s15-admitted-damage"),
    )
    assert replay["replayed"] is True
    assert replay["status"] == "stopped"
    assert (
        len(
            [
                e
                for e in service.audit_timeline(
                    principal=_auditor(reviewer.scope), application_id=application_id
                )["events"]
                if e["action"] == "evidence_source_revealed"
            ]
        )
        == 1
    )


def test_reveal_assemble_evidence_damage_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2: _assemble_evidence supersession damage maps to exactly
    stopped/SOURCE_EVIDENCE_UNAVAILABLE with one safe audit and no raw."""
    service, reviewer, application_id, work_item_id, work, claimed, link, now = (
        _claimed_reveal_context(tmp_path)
    )
    before = copy.deepcopy(service._store.applications[application_id])

    def _broken_assemble(evidence: list[dict[str, object]]) -> list[dict[str, object]]:  # type: ignore[no-untyped-def]
        raise RuntimeError("evidence supersession authority is unavailable")

    monkeypatch.setattr(service, "_assemble_evidence", _broken_assemble)
    result = service.reveal_field_observation(
        principal=reviewer,
        application_id=application_id,
        work_item_id=work_item_id,
        now=NOW,
        **_reveal_args(work, claimed, link, idempotency_key="s15-assemble-damage"),
    )
    assert result["status"] == "stopped"
    assert result["reason_code"] == "SOURCE_EVIDENCE_UNAVAILABLE"
    assert "source_text" not in result
    assert "source_location" not in result
    after = service._store.applications[application_id]
    assert after["lifecycle_revision"] == before["lifecycle_revision"]
    assert after["evidence_revision"] == before["evidence_revision"]
    timeline = service.audit_timeline(
        principal=_auditor(reviewer.scope), application_id=application_id
    )
    events = [
        e
        for e in timeline["events"]
        if e["action"] == "evidence_source_revealed"
    ]
    assert len(events) == 1
    assert events[0]["result"] == "stopped"
    assert "source_text" not in json.dumps(events, sort_keys=True)


def test_reveal_authority_damage_http_maps_to_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2: admitted-evidence damage over HTTP maps to a stable 503
    S03_STOPPED with SOURCE_EVIDENCE_UNAVAILABLE - never an unaudited 500."""
    import asyncio

    import httpx

    from task4_consistency.web import app as web_app_module
    from tests.test_s02_http import TENANT

    environment, submission = _configured_http_source(tmp_path)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    asgi_app = web_app_module.create_s02_test_app()
    service = web_app_module.S01_SERVICE
    assert service is not None

    async def drive() -> tuple[int, dict[str, Any], str, str]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_app), base_url="http://s15-r2-test"
        ) as client:
            session_resp = await client.post(
                "/controlled/s02/api/session",
                headers={"Authorization": f"Bearer {web_app_module.S02_CREDENTIAL}"},
            )
            assert session_resp.status_code == 204
            cookie = "s02_session=" + session_resp.cookies.get("s02_session", "")
            headers = {"Cookie": cookie}
            admission = await client.post(
                "/controlled/s02/api/commands/submit",
                json={"idempotency_key": "r2-http-admission", "submission": submission},
                headers=headers,
            )
            assert admission.status_code == 200, admission.text
            receipt = admission.json()
            application_id = receipt["application_id"]
            completed = service.process_next_job()
            assert completed.status == "complete"
            service.refresh_projection()
            queue = await client.get("/controlled/s01/api/queries/queue", headers=headers)
            assert queue.status_code == 200
            items = queue.json().get("items") or []
            assert len(items) == 1
            work_item_id = items[0]["work_item_id"]
            view_resp = await client.get(
                f"/controlled/s01/api/queries/review-work-items/{work_item_id}",
                headers=headers,
            )
            assert view_resp.status_code == 200
            view = view_resp.json()
            claimed = await client.post(
                f"/controlled/s02/api/commands/review-work-items/{work_item_id}/claim",
                json={"expected_context": view["command_context"]},
                headers=headers,
            )
            assert claimed.status_code == 200, claimed.text
            claim_body = claimed.json()
            workspace_resp = await client.get(
                f"/controlled/s01/api/queries/applications/{application_id}/workspace",
                headers=headers,
            )
            assert workspace_resp.status_code == 200
            link = workspace_resp.json()["selected_finding"]["evidence_links"][0]

            def _broken_admitted(app: dict[str, object]) -> list[dict[str, object]]:  # type: ignore[no-untyped-def]
                raise RuntimeError("evidence event digest mismatch")

            monkeypatch.setattr(service, "_admitted_evidence", _broken_admitted)
            reveal = await client.post(
                f"/controlled/s01/api/commands/review-work-items/{work_item_id}/reveal-field-observation",
                json={
                    "application_id": application_id,
                    "observation_id": link["observation_id"],
                    "expected_fence": claim_body["claim_fence"],
                    "expected_context": view["command_context"],
                    "idempotency_key": "r2-http-damage",
                    "purpose": "MANUAL_REVIEW",
                    "reason": "EVIDENCE_VERIFICATION",
                    "classification": "RESTRICTED",
                    "expected_source_region": link["source_region"],
                },
                headers=headers,
            )
            return (
                reveal.status_code,
                reveal.json(),
                reveal.text,
                reveal.headers.get("cache-control", ""),
                application_id,
            )

    status_code, payload, text, cache_control, application_id = asyncio.run(drive())
    assert status_code == 503, payload
    assert payload["detail"]["error"] == "S03_STOPPED"
    assert payload["detail"]["reason_code"] == "SOURCE_EVIDENCE_UNAVAILABLE"
    assert cache_control == "no-store"
    assert "SAFE-VIN-A" not in text
    # One safe attempted-action audit fact was persisted.
    timeline = service.audit_timeline(
        principal=_auditor(f"R-OBSERVED/{TENANT}"), application_id=application_id
    )
    events = [
        e
        for e in timeline["events"]
        if e["action"] == "evidence_source_revealed"
    ]
    assert len(events) == 1
    assert events[0]["result"] == "stopped"
    assert "SAFE-VIN-A" not in json.dumps(events, sort_keys=True)


def test_reveal_audit_schema_v2_and_v1_compatibility(
    tmp_path: Path,
) -> None:
    """R3: the reveal audit writer publishes s15-reveal-audit/2 events with
    nullable revisions and optional governed vocabulary, while historical
    s15-reveal-audit/1 events stay readable with their original field shape."""
    service, reviewer, application_id, work_item_id, work, claimed, link, now = (
        _claimed_reveal_context(tmp_path)
    )
    # A governed successful reveal emits a v2 event with integer revisions
    # and the three vocabulary fields.
    ok = service.reveal_field_observation(
        principal=reviewer,
        application_id=application_id,
        work_item_id=work_item_id,
        now=NOW,
        **_reveal_args(work, claimed, link, idempotency_key="s15-schema-v2"),
    )
    assert ok["status"] == "revealed"
    timeline = service.audit_timeline(
        principal=_auditor(reviewer.scope), application_id=application_id
    )
    v2_events = [
        e
        for e in timeline["events"]
        if e["action"] == "evidence_source_revealed"
        and e["context"].get("schema_version") == "s15-reveal-audit/2"
    ]
    assert len(v2_events) >= 1
    assert v2_events[0]["context"]["schema_version"] == "s15-reveal-audit/2"
    assert v2_events[0]["context"]["purpose"] == "MANUAL_REVIEW"
    assert v2_events[0]["context"]["lifecycle_revision"] is not None

    # Historical v1 readability: inject one v1-shaped event and verify the
    # projection returns it unchanged (always integer revisions and the
    # three vocabulary fields).
    v1_event = {
        "event_id": "audit_v1_historical_reveal",
        "action": "evidence_source_revealed",
        "subject": reviewer.subject,
        "role": reviewer.role,
        "scope": reviewer.scope,
        "source_id": reviewer.source_id,
        "application_id": application_id,
        "work_item_id": work_item_id,
        "observation_id": link["observation_id"],
        "result": "revealed",
        "reason_code": "SOURCE_REVEAL_AUTHORIZED",
        "purpose": "MANUAL_REVIEW",
        "verification_reason": "EVIDENCE_VERIFICATION",
        "classification": "RESTRICTED",
        "idempotency_fingerprint": "f" * 64,
        "idempotency_binding": "b" * 64,
        "context_digest": "c" * 64,
        "lifecycle_revision": 7,
        "evidence_revision": 2,
        "claim_fence": 1,
        "claim_expires_at": NOW + 900,
        "policy_id": "s15-controlled-reveal/1",
        "policy_digest": "d" * 64,
        "policy_version": "1",
        "policy_term_seconds": 900,
        "schema_version": "s15-reveal-audit/1",
        "event_time": NOW,
        "event_sequence": 9999,
        "event_time_key": f"{NOW:020d}:{9999:010d}",
    }
    service._store.audit_events.append(v1_event)
    service._store.persist()
    timeline3 = service.audit_timeline(
        principal=_auditor(reviewer.scope), application_id=application_id
    )
    v1_events = [
        e
        for e in timeline3["events"]
        if e["action"] == "evidence_source_revealed"
        and e["event_id"] == "audit_v1_historical_reveal"
    ]
    assert len(v1_events) == 1
    assert v1_events[0]["context"]["schema_version"] == "s15-reveal-audit/1"
    assert v1_events[0]["context"]["purpose"] == "MANUAL_REVIEW"
    assert v1_events[0]["context"]["verification_reason"] == "EVIDENCE_VERIFICATION"
    assert v1_events[0]["context"]["classification"] == "RESTRICTED"
    assert v1_events[0]["context"]["lifecycle_revision"] == 7
    assert v1_events[0]["context"]["evidence_revision"] == 2


def test_reveal_missing_application_faults_return_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3: a visible work item with missing application authority combined
    with audit-write, persistence, or recovery-reload failure must converge
    on a stable no-value unavailable built from the authentic work-item
    reference - no TypeError, no partial audit, no idempotency binding."""
    def _drop_app_and_freeze_reload(
        service, ctx, application_id
    ) -> None:
        service._store.applications.pop(application_id, None)
        ctx.setattr(service, "_reload_store", lambda: None)  # type: ignore[method-assign]

    # Each case needs an isolated store (the governed fixture uses the same
    # state path), so give every case its own subdirectory.
    # Case A: audit-write failure (audit unavailable).
    service_a, reviewer_a, app_a, wi_a, work_a, claimed_a, link_a, _ = (
        _claimed_reveal_context(tmp_path / "case-a")
    )
    with monkeypatch.context() as ctx:
        _drop_app_and_freeze_reload(service_a, ctx, app_a)
        ctx.setattr(service_a, "audit_available", False)
        original_before_write_a = service_a._before_write

        def _fail_audit_write(write_point: str) -> None:
            if write_point == "reveal.audit":
                raise _StoreWriteFailure("reveal.audit")
            original_before_write_a(write_point)

        ctx.setattr(service_a, "_before_write", _fail_audit_write)
        result = service_a.reveal_field_observation(
            principal=reviewer_a,
            application_id=app_a,
            work_item_id=wi_a,
            now=NOW,
            **_reveal_args(work_a, claimed_a, link_a, idempotency_key="s15-fault-audit"),
        )
        assert result["status"] == "unavailable"
        assert result["reason_code"] == "AUDIT_UNAVAILABLE"
        assert result["application_id"] == app_a
        assert result["work_item_id"] == wi_a
        assert "source_text" not in result
        assert "source_location" not in result
        assert "s15-fault-audit" not in str(service_a._store.idempotency)

    # Case B: persistence failure raised INSIDE the live staged
    # SQLiteTargetStore.persist() transaction, after the audit and
    # idempotency SQL has executed.  The _sync_idempotency seam runs the
    # original writer first (audit and idempotency rows are actually
    # written), then raises a genuine sqlite3.OperationalError so the
    # transaction's connection.rollback() and the recovery path run.
    service_b, reviewer_b, app_b, wi_b, work_b, claimed_b, link_b, _ = (
        _claimed_reveal_context(tmp_path / "case-b")
    )
    store_cls_b = service_b._store.__class__
    baseline_b = _persisted_baseline(service_b)
    with monkeypatch.context() as ctx:
        _drop_app_and_freeze_reload(service_b, ctx, app_b)
        original_sync_idem_b = store_cls_b._sync_idempotency

        def _faulting_sync_idempotency(self, connection):  # type: ignore[no-untyped-def]
            original_sync_idem_b(self, connection)
            raise sqlite3.OperationalError("database is locked")

        ctx.setattr(
            store_cls_b, "_sync_idempotency", _faulting_sync_idempotency
        )
        result = service_b.reveal_field_observation(
            principal=reviewer_b,
            application_id=app_b,
            work_item_id=wi_b,
            now=NOW,
            **_reveal_args(work_b, claimed_b, link_b, idempotency_key="s15-fault-persist"),
        )
        assert result["status"] == "unavailable"
        assert result["reason_code"] == "STORAGE_UNAVAILABLE"
        assert result["application_id"] == app_b
        assert "source_text" not in result
        # No partial idempotency binding installed for this attempt.
        assert "s15-fault-persist" not in str(service_b._store.idempotency)
    # Real recovery reload restores the complete unmodified persisted
    # baseline: store revision, projections, lifecycle/evidence/review
    # facts, work items, audit rows and idempotency bindings are all equal
    # to the pre-attempt database state (rollback removed every partial
    # write including the application-row delete staged by the same
    # transaction).
    service_b._reload_store()
    assert _persisted_baseline(service_b) == baseline_b
    # Stable replay behavior: because the failed attempt installed no
    # binding, a later retry of the same key executes a fresh attempt and
    # succeeds instead of replaying a poisoned result.
    retry_b = service_b.reveal_field_observation(
        principal=reviewer_b,
        application_id=app_b,
        work_item_id=wi_b,
        now=NOW,
        **_reveal_args(work_b, claimed_b, link_b, idempotency_key="s15-fault-persist"),
    )
    assert retry_b["status"] == "revealed"
    assert retry_b["replayed"] is False
    assert "source_text" in retry_b

    # Case C: recovery reload itself fails after a persistence failure.
    service_c, reviewer_c, app_c, wi_c, work_c, claimed_c, link_c, _ = (
        _claimed_reveal_context(tmp_path / "case-c")
    )
    with monkeypatch.context() as ctx:
        _drop_app_and_freeze_reload(service_c, ctx, app_c)

        def _fail_before_write(write_point: str) -> None:
            raise _StoreWriteFailure(write_point)

        original_reload_c = service_c._reload_store
        reload_count = {"n": 0}

        def _flaky_reload() -> None:
            # First reload (bootstrap) succeeds; the recovery reload inside
            # the outcome writer's exception path fails.
            reload_count["n"] += 1
            if reload_count["n"] > 1:
                raise RuntimeError("recovery reload failed")
            original_reload_c()

        ctx.setattr(service_c, "_before_write", _fail_before_write)
        ctx.setattr(service_c, "_reload_store", _flaky_reload)
        result = service_c.reveal_field_observation(
            principal=reviewer_c,
            application_id=app_c,
            work_item_id=wi_c,
            now=NOW,
            **_reveal_args(work_c, claimed_c, link_c, idempotency_key="s15-fault-reload"),
        )
        assert result["status"] == "unavailable"
        assert result["reason_code"] == "STORAGE_UNAVAILABLE"
        assert result["application_id"] == app_c
        assert result["work_item_id"] == wi_c
        assert "source_text" not in result
        assert "s15-fault-reload" not in str(service_c._store.idempotency)


def test_reveal_bootstrap_storage_outage_is_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3: a bootstrap storage outage returns the stable unavailable
    response without an audit (nothing can be proven or written)."""
    service, reviewer, application_id, work_item_id, work, claimed, link, now = (
        _claimed_reveal_context(tmp_path)
    )

    def _broken_reload() -> None:
        raise RuntimeError("storage reload failed")

    monkeypatch.setattr(service, "_reload_store", _broken_reload)
    result = service.reveal_field_observation(
        principal=reviewer,
        application_id=application_id,
        work_item_id=work_item_id,
        now=NOW,
        **_reveal_args(work, claimed, link, idempotency_key="s15-bootstrap-outage"),
    )
    assert result["status"] == "unavailable"
    assert result["reason_code"] == "STORAGE_UNAVAILABLE"
    assert result["application_id"] == application_id
    assert result["work_item_id"] == work_item_id
    assert "source_text" not in result
    assert "source_location" not in result


def test_reveal_visible_work_authority_damage_is_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3: damage during full work-item authority reconstruction of a
    visible, scope-checked resource is one safe audited stopped outcome with
    stable idempotent replay and zero raw content."""
    service, reviewer, application_id, work_item_id, work, claimed, link, now = (
        _claimed_reveal_context(tmp_path)
    )
    before = copy.deepcopy(service._store.applications[application_id])

    def _broken_authority(principal, work_item_id, now):  # type: ignore[no-untyped-def]
        raise RuntimeError("work-item authority reconstruction failed")

    monkeypatch.setattr(service, "_review_work_item_authority", _broken_authority)
    result = service.reveal_field_observation(
        principal=reviewer,
        application_id=application_id,
        work_item_id=work_item_id,
        now=NOW,
        **_reveal_args(work, claimed, link, idempotency_key="s15-authority-damage-r3"),
    )
    assert result["status"] == "stopped"
    assert result["reason_code"] == "SOURCE_EVIDENCE_UNAVAILABLE"
    assert "source_text" not in result
    assert "source_location" not in result
    after = service._store.applications[application_id]
    assert after["lifecycle_revision"] == before["lifecycle_revision"]
    assert after["evidence_revision"] == before["evidence_revision"]
    timeline = service.audit_timeline(
        principal=_auditor(reviewer.scope), application_id=application_id
    )
    events = [
        e
        for e in timeline["events"]
        if e["action"] == "evidence_source_revealed"
    ]
    assert len(events) == 1
    assert events[0]["result"] == "stopped"
    assert events[0]["context"]["schema_version"] == "s15-reveal-audit/2"
    assert "source_text" not in json.dumps(events, sort_keys=True)
    # Stable replay through the protected idempotency binding.
    service._reload_store()
    replay = service.reveal_field_observation(
        principal=reviewer,
        application_id=application_id,
        work_item_id=work_item_id,
        now=NOW,
        **_reveal_args(work, claimed, link, idempotency_key="s15-authority-damage-r3"),
    )
    assert replay["replayed"] is True
    assert replay["status"] == "stopped"
    assert (
        len(
            [
                e
                for e in service.audit_timeline(
                    principal=_auditor(reviewer.scope), application_id=application_id
                )["events"]
                if e["action"] == "evidence_source_revealed"
            ]
        )
        == 1
    )


def test_reveal_unauthorized_and_cross_tenant_existence_hiding(
    tmp_path: Path,
) -> None:
    """R3: unauthorized and cross-tenant reveal attempts keep QueryNotFound
    existence hiding with zero audit facts."""
    service, reviewer, application_id, work_item_id, work, claimed, link, now = (
        _claimed_reveal_context(tmp_path)
    )
    unassigned = S01CommandPrincipal(
        subject="other-reviewer",
        role="reviewer",
        scope=reviewer.scope,
        source_id="s15-other",
        expires_at=reviewer.expires_at,
    )
    with pytest.raises(QueryNotFound):
        service.reveal_field_observation(
            principal=unassigned,
            application_id=application_id,
            work_item_id=work_item_id,
            now=NOW,
            **_reveal_args(work, claimed, link, idempotency_key="s15-unauthorized"),
        )
    cross_tenant = S01CommandPrincipal(
        subject=reviewer.subject,
        role="reviewer",
        scope="R-OBSERVED/other-tenant",
        source_id=reviewer.source_id,
        expires_at=reviewer.expires_at,
    )
    with pytest.raises(QueryNotFound):
        service.reveal_field_observation(
            principal=cross_tenant,
            application_id=application_id,
            work_item_id=work_item_id,
            now=NOW,
            **_reveal_args(work, claimed, link, idempotency_key="s15-cross-tenant"),
        )
    # Unidentifiable work item: unknown identifier keeps QueryNotFound
    # existence hiding.  The complete persisted state - store revision,
    # projections, lifecycle/evidence/review facts, work items, audit rows
    # and idempotency bindings - is unchanged immediately after the attempt
    # and again after a real reload.
    baseline = _persisted_baseline(service)
    with pytest.raises(QueryNotFound):
        service.reveal_field_observation(
            principal=reviewer,
            application_id=application_id,
            work_item_id="work_item_unknown_r4",
            now=NOW,
            **_reveal_args(work, claimed, link, idempotency_key="s15-unknown-work"),
        )
    assert _persisted_baseline(service) == baseline
    service._reload_store()
    assert _persisted_baseline(service) == baseline
    timeline = service.audit_timeline(
        principal=_auditor(reviewer.scope), application_id=application_id
    )
    events = [
        e
        for e in timeline["events"]
        if e["action"] == "evidence_source_revealed"
    ]
    assert events == []
    assert "s15-unknown-work" not in str(service._store.idempotency)


def test_reveal_unavailable_http_maps_to_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3: a visible work item with missing application authority plus an
    audit-write failure maps over HTTP to a stable 503 S03_UNAVAILABLE /
    AUDIT_UNAVAILABLE with no-store and zero raw content."""
    import asyncio

    import httpx

    from task4_consistency.web import app as web_app_module

    environment, submission = _configured_http_source(tmp_path)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    asgi_app = web_app_module.create_s02_test_app()
    service = web_app_module.S01_SERVICE
    assert service is not None

    async def drive() -> tuple[int, dict[str, Any], str, str]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_app), base_url="http://s15-r2-test"
        ) as client:
            session_resp = await client.post(
                "/controlled/s02/api/session",
                headers={"Authorization": f"Bearer {web_app_module.S02_CREDENTIAL}"},
            )
            assert session_resp.status_code == 204
            cookie = "s02_session=" + session_resp.cookies.get("s02_session", "")
            headers = {"Cookie": cookie}
            admission = await client.post(
                "/controlled/s02/api/commands/submit",
                json={"idempotency_key": "r2-http-unavailable", "submission": submission},
                headers=headers,
            )
            assert admission.status_code == 200, admission.text
            receipt = admission.json()
            application_id = receipt["application_id"]
            completed = service.process_next_job()
            assert completed.status == "complete"
            service.refresh_projection()
            queue = await client.get("/controlled/s01/api/queries/queue", headers=headers)
            items = queue.json().get("items") or []
            work_item_id = items[0]["work_item_id"]
            view_resp = await client.get(
                f"/controlled/s01/api/queries/review-work-items/{work_item_id}",
                headers=headers,
            )
            view = view_resp.json()
            claimed = await client.post(
                f"/controlled/s02/api/commands/review-work-items/{work_item_id}/claim",
                json={"expected_context": view["command_context"]},
                headers=headers,
            )
            claim_body = claimed.json()
            workspace_resp = await client.get(
                f"/controlled/s01/api/queries/applications/{application_id}/workspace",
                headers=headers,
            )
            link = workspace_resp.json()["selected_finding"]["evidence_links"][0]
            # Missing application authority + audit-write failure.
            service._store.applications.pop(application_id, None)
            service._reload_store = lambda: None  # type: ignore[method-assign]
            service.audit_available = False
            original_before_write = service._before_write

            def _fail_audit_write(write_point: str) -> None:
                if write_point == "reveal.audit":
                    raise _StoreWriteFailure("reveal.audit")
                original_before_write(write_point)

            service._before_write = _fail_audit_write  # type: ignore[method-assign]
            reveal = await client.post(
                f"/controlled/s01/api/commands/review-work-items/{work_item_id}/reveal-field-observation",
                json={
                    "application_id": application_id,
                    "observation_id": link["observation_id"],
                    "expected_fence": claim_body["claim_fence"],
                    "expected_context": view["command_context"],
                    "idempotency_key": "r2-http-unavailable-reveal",
                    "purpose": "MANUAL_REVIEW",
                    "reason": "EVIDENCE_VERIFICATION",
                    "classification": "RESTRICTED",
                    "expected_source_region": link["source_region"],
                },
                headers=headers,
            )
            return reveal.status_code, reveal.json(), reveal.text, reveal.headers.get(
                "cache-control", ""
            )

    status_code, payload, text, cache_control = asyncio.run(drive())
    assert status_code == 503, payload
    assert payload["detail"]["error"] == "S03_UNAVAILABLE"
    assert payload["detail"]["reason_code"] == "AUDIT_UNAVAILABLE"
    assert cache_control == "no-store"
    assert "SAFE-VIN-A" not in text
    # No partial idempotency binding was installed.
    assert "r2-http-unavailable-reveal" not in str(service._store.idempotency)


def test_reveal_sqlite_persistence_fault_http_maps_to_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: a real sqlite3.OperationalError from the staged persist()
    transaction maps over HTTP to a stable 503 S03_UNAVAILABLE /
    STORAGE_UNAVAILABLE with no-store and no raw/locator/credential/path;
    the recovery-reload failure variant stays on the same contract."""
    import asyncio

    import httpx

    from task4_consistency.web import app as web_app_module

    environment, submission = _configured_http_source(tmp_path)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    asgi_app = web_app_module.create_s02_test_app()
    service = web_app_module.S01_SERVICE
    assert service is not None
    store_cls = service._store.__class__
    credential = web_app_module.S02_CREDENTIAL
    internal_path = str(tmp_path)

    async def prepare() -> tuple[str, dict[str, Any], dict[str, Any], str]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_app), base_url="http://s15-r4-test"
        ) as client:
            session_resp = await client.post(
                "/controlled/s02/api/session",
                headers={"Authorization": f"Bearer {credential}"},
            )
            assert session_resp.status_code == 204
            cookie = "s02_session=" + session_resp.cookies.get("s02_session", "")
            headers = {"Cookie": cookie}
            admission = await client.post(
                "/controlled/s02/api/commands/submit",
                json={"idempotency_key": "r4-http-admission", "submission": submission},
                headers=headers,
            )
            assert admission.status_code == 200, admission.text
            receipt = admission.json()
            application_id = receipt["application_id"]
            completed = service.process_next_job()
            assert completed.status == "complete"
            service.refresh_projection()
            queue = await client.get("/controlled/s01/api/queries/queue", headers=headers)
            items = queue.json().get("items") or []
            work_item_id = items[0]["work_item_id"]
            view_resp = await client.get(
                f"/controlled/s01/api/queries/review-work-items/{work_item_id}",
                headers=headers,
            )
            view = view_resp.json()
            claimed = await client.post(
                f"/controlled/s02/api/commands/review-work-items/{work_item_id}/claim",
                json={"expected_context": view["command_context"]},
                headers=headers,
            )
            claim_body = claimed.json()
            workspace_resp = await client.get(
                f"/controlled/s01/api/queries/applications/{application_id}/workspace",
                headers=headers,
            )
            link = workspace_resp.json()["selected_finding"]["evidence_links"][0]
            reveal_body = {
                "application_id": application_id,
                "observation_id": link["observation_id"],
                "expected_fence": claim_body["claim_fence"],
                "expected_context": view["command_context"],
                "idempotency_key": "r4-http-persist-fault",
                "purpose": "MANUAL_REVIEW",
                "reason": "EVIDENCE_VERIFICATION",
                "classification": "RESTRICTED",
                "expected_source_region": link["source_region"],
            }
            return (
                f"/controlled/s01/api/commands/review-work-items/{work_item_id}/reveal-field-observation",
                reveal_body,
                headers,
                application_id,
            )

    async def run_async() -> tuple[Any, Any, Any, Any]:
        reveal_path, reveal_body, headers, application_id = await prepare()
        results = {}
        # Variant 1: genuine sqlite3.OperationalError raised INSIDE the live
        # staged persist() transaction after the audit and idempotency SQL
        # has executed (the _sync_idempotency seam runs the real writer
        # first, then fails); the transaction rollback and the recovery
        # reload both run.
        with monkeypatch.context() as ctx:
            original_sync = store_cls._sync_idempotency

            def _faulting_sync(self, connection):  # type: ignore[no-untyped-def]
                original_sync(self, connection)
                raise sqlite3.OperationalError("database is locked")

            ctx.setattr(store_cls, "_sync_idempotency", _faulting_sync)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=asgi_app),
                base_url="http://s15-r4-test",
            ) as client:
                reveal = await client.post(
                    reveal_path, json=reveal_body, headers=headers
                )
            results["persist"] = (
                reveal.status_code,
                reveal.json(),
                reveal.text,
                reveal.headers.get("cache-control", ""),
            )
        # Variant 2: the same in-transaction persistence fault plus a
        # recovery-reload failure.  The connection counter follows the real
        # call sequence of one reveal POST: resolve_session reload (1),
        # reveal bootstrap reload (2), staged persist (3 - healthy connect,
        # in-transaction sync raises), recovery reload (4 - raises and is
        # contained).  Session resolution and bootstrap stay healthy.
        with monkeypatch.context() as ctx:
            original_connect = store_cls._connect
            original_sync2 = store_cls._sync_idempotency
            connect_count = {"n": 0}

            def _flaky_connect(self):  # type: ignore[no-untyped-def]
                connect_count["n"] += 1
                if connect_count["n"] >= 4:
                    raise sqlite3.OperationalError("database is locked")
                return original_connect(self)

            def _faulting_sync2(self, connection):  # type: ignore[no-untyped-def]
                original_sync2(self, connection)
                raise sqlite3.OperationalError("database is locked")

            ctx.setattr(store_cls, "_connect", _flaky_connect)
            ctx.setattr(store_cls, "_sync_idempotency", _faulting_sync2)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=asgi_app),
                base_url="http://s15-r4-test",
            ) as client:
                reveal = await client.post(
                    reveal_path, json=reveal_body, headers=headers
                )
            results["recovery"] = (
                reveal.status_code,
                reveal.json(),
                reveal.text,
                reveal.headers.get("cache-control", ""),
            )
        return (
            results["persist"],
            results["recovery"],
            application_id,
            reveal_body,
        )

    persist_result, recovery_result, application_id, reveal_body = asyncio.run(run_async())
    for label, (status_code, payload, text, cache_control) in (
        ("persist", persist_result),
        ("recovery-reload", recovery_result),
    ):
        assert status_code == 503, (label, payload)
        assert payload["detail"]["error"] == "S03_UNAVAILABLE"
        assert payload["detail"]["reason_code"] == "STORAGE_UNAVAILABLE"
        assert cache_control == "no-store"
        # No raw value, source locator, credential, or internal path.
        assert "SAFE-VIN-A" not in text
        assert "http-page-object" not in text
        assert "http-result-object" not in text
        assert credential not in text
        assert internal_path not in text


def test_reveal_missing_application_persists_v2_nullable_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: a safe pre-C19 attempted-action audit with app=None is persisted
    as s15-reveal-audit/2 and survives a real reload with nullable
    lifecycle/evidence revisions and omitted vocabulary fields."""
    service, reviewer, application_id, work_item_id, work, claimed, link, now = (
        _claimed_reveal_context(tmp_path)
    )
    class _HiddenAppDict(dict):
        """Lookup view that hides one application from the reveal while the
        staged deepcopy keeps every entry, so persistence is authentic."""

        def get(self, key: str, default: object = None) -> object:  # type: ignore[override]
            if key == application_id:
                return None
            return super().get(key, default)

    with monkeypatch.context() as ctx:
        ctx.setattr(service, "_reload_store", lambda: None)  # type: ignore[method-assign]
        # Hide the application from the reveal's lookup only; the staged
        # deepcopy preserves the entries, so the immutable persistence path
        # writes the authentic audit and idempotency rows unchanged.
        ctx.setattr(
            service._store,
            "applications",
            _HiddenAppDict(service._store.applications),
        )
        result = service.reveal_field_observation(
            principal=reviewer,
            application_id=application_id,
            work_item_id=work_item_id,
            now=NOW,
            **_reveal_args(work, claimed, link, idempotency_key="s15-v2-nullable"),
        )
        assert result["status"] == "stopped"
        assert result["reason_code"] == "SOURCE_EVIDENCE_UNAVAILABLE"
        assert "source_text" not in result
        assert "source_location" not in result
    # Real reload: the v2 event persists with nullable revisions and no
    # caller vocabulary; the application row is untouched.
    service._reload_store()
    timeline = service.audit_timeline(
        principal=_auditor(reviewer.scope), application_id=application_id
    )
    events = [
        e
        for e in timeline["events"]
        if e["action"] == "evidence_source_revealed"
    ]
    assert len(events) == 1
    context = events[0]["context"]
    assert context["schema_version"] == "s15-reveal-audit/2"
    assert context["lifecycle_revision"] is None
    assert context["evidence_revision"] is None
    assert "purpose" not in context
    assert "verification_reason" not in context
    assert "classification" not in context
    assert events[0]["result"] == "stopped"
    # The idempotency binding also survived the real reload.
    replay = service.reveal_field_observation(
        principal=reviewer,
        application_id=application_id,
        work_item_id=work_item_id,
        now=NOW,
        **_reveal_args(work, claimed, link, idempotency_key="s15-v2-nullable"),
    )
    assert replay["replayed"] is True
    assert replay["status"] == "stopped"
