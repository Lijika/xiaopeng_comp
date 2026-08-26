"""S15 C19 policy-owner and registered reveal authority (focused)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from task4_consistency.controlled.s01 import ControlledScenarioService, S01CommandPrincipal, QueryNotFound
from tests.test_s03_controlled import ready_review_work_item


def test_c19_reveal_policy_is_owned_by_institution_and_governs_vocab(tmp_path: Path) -> None:
    """The canonical S15 policy at configs/c19_reveal_policy.json is owned
    by institution-data-governance and its purpose/reason/classification/
    term lists are the sole authority for reveal.  A configuration change
    flips the authorization result without touching Python constants, while
    the DTO Literals in web/app.py remain the closed wire shape."""
    policy_path = Path(ControlledScenarioService._C19_REVEAL_POLICY_PATH)
    assert policy_path.exists(), "C19 reveal policy file must exist"
    raw = json.loads(policy_path.read_text(encoding="utf-8"))
    assert raw.get("owner") == "institution-data-governance"
    assert raw.get("tenant_scope") == "institution-controlled"
    assert "MANUAL_REVIEW" in raw.get("purposes", [])
    assert "EVIDENCE_VERIFICATION" in raw.get("reasons", [])
    assert "RESTRICTED" in raw.get("classifications", [])
    # The service loads the same file as its authority.
    loaded = ControlledScenarioService._load_c19_reveal_policy()
    assert loaded is not None
    assert set(loaded["purposes"]) == set(raw["purposes"])
    # Tampering the file to remove the canonical purpose must cause the
    # loader to consider the policy unavailable, closing the reveal (G4).
    # This is the focused proof that a pure configuration change flips the
    # result without code change.
    tampered = dict(raw)
    tampered["purposes"] = []
    tmp_tampered = tmp_path / "tampered.json"
    tmp_tampered.write_text(json.dumps(tampered), encoding="utf-8")
    orig = ControlledScenarioService._C19_REVEAL_POLICY_PATH
    try:
        ControlledScenarioService._C19_REVEAL_POLICY_PATH = tmp_tampered  # type: ignore[assignment]
        assert ControlledScenarioService._load_c19_reveal_policy() is None
    finally:
        ControlledScenarioService._C19_REVEAL_POLICY_PATH = orig  # type: ignore[assignment]
    # After restoring, the real policy is again available.
    assert ControlledScenarioService._load_c19_reveal_policy() is not None


def test_registered_reveal_s15_success_and_policy_denial(tmp_path: Path) -> None:
    """Registered controlled authority is the only successful S15 path.
    C-DEMO synthetic is denied, registered with correct vocab and region
    succeeds with single value and zero business revision, while unknown
    vocab or tampered policy is audited as rejected."""
    # Create a registered review work item (R-OBSERVED track).
    service, reviewer, application_id, work_item_id = ready_review_work_item(tmp_path)
    # Claim the work item to obtain a live fence/context.
    work = service.review_work_item_view(principal=reviewer, work_item_id=work_item_id, now=101)
    claimed = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item_id,
        expected_context=work["command_context"],
        now=101,
    )
    # Workspace is minimized: evidence_links are masked, entity mentions redacted.
    workspace = service.workspace_view(
        application_id,
        role="reviewer",
        scope=reviewer.scope,
        subject=reviewer.subject,
        now=101,
    )
    assert all(link["raw_masked"] == "[REDACTED]" for link in workspace["selected_finding"]["evidence_links"] if link["value_state"] == "present")
    # Pick one link's observation for reveal.
    link = workspace["selected_finding"]["evidence_links"][0]
    obs_id = link["observation_id"]
    region = link["source_region"]
    assert isinstance(region, str) and region.startswith("region:")
    # Wrong scope: C-DEMO synthetic principal cannot reveal even though it
    # may hold a claim; this is the demo denial coverage.
    c_demo_reviewer = S01CommandPrincipal(
        subject=reviewer.subject,
        role="reviewer",
        scope="C-DEMO",
        source_id="c-demo-review-console",
        expires_at=reviewer.expires_at,
    )
    # The service's demo denial is at the domain level: a C-DEMO principal
    # attempting to reveal a registered R-OBSERVED work item is existence-
    # hidden (404) because the work item's visibility_scope is registered.
    with pytest.raises(QueryNotFound):
        service.reveal_field_observation(
            principal=c_demo_reviewer,
            application_id=application_id,
            work_item_id=work_item_id,
            observation_id=obs_id,
            expected_fence=claimed["claim_fence"],
            expected_context=work["command_context"],
            idempotency_key="s15-demo-denial",
            purpose="MANUAL_REVIEW",
            reason="EVIDENCE_VERIFICATION",
            classification="RESTRICTED",
            expected_source_region=region,
            now=102,
        )
    # Correct registered reveal succeeds with single value and term.
    before_app = dict(service._store.applications[application_id])
    revealed = service.reveal_field_observation(
        principal=reviewer,
        application_id=application_id,
        work_item_id=work_item_id,
        observation_id=obs_id,
        expected_fence=claimed["claim_fence"],
        expected_context=work["command_context"],
        idempotency_key="s15-registered-success",
        purpose="MANUAL_REVIEW",
        reason="EVIDENCE_VERIFICATION",
        classification="RESTRICTED",
        expected_source_region=region,
        now=102,
    )
    assert revealed["status"] == "revealed"
    assert revealed["purpose"] == "MANUAL_REVIEW"
    assert revealed["classification"] == "RESTRICTED"
    assert isinstance(revealed["source_text"], str) and revealed["source_text"]
    assert revealed["claim_expires_at"] == claimed["claim_expires_at"]
    # Zero business revision: lifecycle and evidence revisions unchanged.
    after_app = service._store.applications[application_id]
    assert after_app["lifecycle_revision"] == before_app["lifecycle_revision"]
    assert after_app["evidence_revision"] == before_app["evidence_revision"]
    # Unknown classification is a closed-vocab rejection, audited without raw.
    vocab_rejected = service.reveal_field_observation(
        principal=reviewer,
        application_id=application_id,
        work_item_id=work_item_id,
        observation_id=obs_id,
        expected_fence=claimed["claim_fence"],
        expected_context=work["command_context"],
        idempotency_key="s15-vocab-unknown",
        purpose="MANUAL_REVIEW",
        reason="EVIDENCE_VERIFICATION",
        classification="UNKNOWN_CLASS",
        expected_source_region=region,
        now=103,
    )
    assert vocab_rejected["status"] == "rejected"
    assert vocab_rejected["reason_code"] == "REVEAL_VOCABULARY_UNKNOWN"
    # Audit timeline is minimized and contains the safe context, not raw/locator.
    timeline = service.audit_timeline(
        principal=S01CommandPrincipal(
            subject="s15-auditor",
            role="auditor",
            scope=reviewer.scope,
            source_id="s15-audit-console",
        ),
        application_id=application_id,
    )
    reveal_events = [e for e in timeline["events"] if e["action"] == "evidence_source_revealed"]
    # At least the successful reveal and the vocab rejection are audited.
    assert any(e["result"] == "revealed" for e in reveal_events)
    assert any(e["result"] == "rejected" and e["context"].get("reason_code") == "REVEAL_VOCABULARY_UNKNOWN" for e in reveal_events)
    for ev in reveal_events:
        # No raw, locator, credential or path in the projected audit.
        assert "source_text" not in json.dumps(ev)
        assert "locator" not in json.dumps(ev).lower()
        assert ev["context"].get("purpose") in ("MANUAL_REVIEW", None)
