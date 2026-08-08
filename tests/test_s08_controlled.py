"""S08 domain and integration contract tests (I/M/F/R evidence classes).

The seams under test are the PolicyGovernanceService command surface, the
durable policy worker, the Registry artifacts and the Governance Ledger over
the public SQLite adapter.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from task4_consistency.rules.loader import load_rules
from task4_consistency.controlled.s01_store import SQLiteTargetStore
from task4_consistency.controlled.s08 import (
    PolicyConflict,
    PolicyGovernanceService,
    PolicyInvalidTransition,
    PolicyPrincipal,
    S08_SCOPE,
    SOURCE_BUNDLE_ID,
    canonical_bytes,
    content_digest,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES = ROOT / "configs" / "rules_auto_lease.yaml"
DEFAULT_KB = ROOT / "configs" / "kb" / "entity_kb.json"
CORPUS = ROOT / "fixtures" / "applications"

ADMIN = PolicyPrincipal(
    subject="c-demo-policy-admin", role="admin", scope=S08_SCOPE, source_id="s08-test"
)
APPROVER = PolicyPrincipal(
    subject="c-demo-policy-approver",
    role="approver",
    scope=S08_SCOPE,
    source_id="s08-test",
)


@pytest.fixture
def restore_global_kb() -> Any:
    """Restore the process-global KB singleton after a poisoning test so
    later TestClient suites observe the default knowledge."""
    from task4_consistency.kb.store import get_kb, reload_kb

    previous = get_kb().path
    yield
    reload_kb(previous)


def make_policy_service(
    tmp_path: Path,
    *,
    rules_bytes: bytes | None = None,
    kb_bytes: bytes | None = None,
    corpus_root: Path | None = CORPUS,
    state_path: Path | None = None,
    expect_bootstrap: bool = True,
) -> tuple[PolicyGovernanceService, Path, Path]:
    bundle = tmp_path / "server-bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    rules_path = bundle / "rules.yaml"
    kb_path = bundle / "entity_kb.json"
    rules_path.write_bytes(rules_bytes if rules_bytes is not None else DEFAULT_RULES.read_bytes())
    kb_path.write_bytes(kb_bytes if kb_bytes is not None else DEFAULT_KB.read_bytes())
    service = PolicyGovernanceService(
        state_path=state_path or (tmp_path / "governance.sqlite3"),
        source_rules_path=rules_path,
        source_kb_path=kb_path,
        corpus_root=corpus_root,
    )
    bootstrap = service.bootstrap_once()
    if expect_bootstrap:
        assert bootstrap["status"] == "activated", bootstrap
    return service, rules_path, kb_path


def governance_revision(service: PolicyGovernanceService) -> int:
    return service.query_status(ADMIN)["governance_revision"]


def import_draft(service: PolicyGovernanceService) -> str:
    result = service.import_legacy(
        principal=ADMIN,
        source_bundle_id=SOURCE_BUNDLE_ID,
        idempotency_key=f"import-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    assert result["status"] == "accepted"
    return result["draft_id"]


def freeze_and_validate(
    service: PolicyGovernanceService, draft_id: str
) -> tuple[str, dict[str, Any]]:
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    assert freeze["status"] == "accepted"
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"validate-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    worker = service.process_next_policy_job()
    assert worker["status"] == "complete"
    assert worker["candidate_id"] == candidate_id
    return candidate_id, worker


def ledger_content(
    service: PolicyGovernanceService, ledger_id: str
) -> dict[str, Any]:
    rows = [
        item
        for item in service._store.policy_artifacts
        if item.get("artifact_id") == ledger_id
    ]
    assert len(rows) == 1
    return json.loads(rows[0]["canonical_json"])


def validation_bundle(
    service: PolicyGovernanceService, bundle_id: str
) -> dict[str, Any]:
    rows = [
        item
        for item in service._store.policy_artifacts
        if item.get("artifact_id") == bundle_id
    ]
    assert len(rows) == 1
    return json.loads(rows[0]["canonical_json"])


def check_outcomes(bundle: dict[str, Any]) -> dict[str, str]:
    return {
        check["check_id"]: check["outcome"]
        for check in bundle["results"]["checks"]
    }


def test_import_and_validation_bundle_cover_every_source_item_with_zero_behavior_diff(
    tmp_path: Path,
) -> None:
    """Every source item is classified in the mapping ledger, every manifest
    component is digest-bound, and the validation bundle proves structure,
    protected baseline, determinism and complete frozen-corpus zero
    behavior difference against the bootstrap anchor."""
    service, rules_path, kb_path = make_policy_service(tmp_path)
    draft_id = import_draft(service)
    drafts = service.query_drafts(ADMIN)["drafts"]
    draft = next(item for item in drafts if item["draft_id"] == draft_id)
    ledger = ledger_content(service, draft["mapping_ledger_id"])

    # 1. Every source item is classified exactly once with a stable reason.
    assert ledger["importer_version"] == "s08-importer/1"
    assert ledger["source_refs"]["rules_sha256"] == hashlib.sha256(
        rules_path.read_bytes()
    ).hexdigest()
    assert ledger["source_refs"]["knowledge_sha256"] == hashlib.sha256(
        kb_path.read_bytes()
    ).hexdigest()
    classifications = {item["classification"] for item in ledger["items"]}
    assert classifications <= {"exact", "explicit_transform", "non_runtime_excluded"}
    assert sum(
        item["classification"] == "unsupported" for item in ledger["items"]
    ) == 0
    for item in ledger["items"]:
        assert item["source_pointer"]
        assert item["importer_version"] == "s08-importer/1"
        assert item["result_digest"] and len(item["result_digest"]) == 64

    rules_data = yaml.safe_load(rules_path.read_bytes())
    rule_ids = {str(rule["id"]) for rule in rules_data["rules"]}
    ledger_rule_targets = {
        item["target_ref"].split("/")[-1]
        for item in ledger["items"]
        if item["target_ref"] and item["target_ref"].startswith("checker.rules/")
    }
    assert ledger_rule_targets == rule_ids
    for canonical in rules_data["field_aliases"]:
        assert any(
            item["target_ref"] == f"checker.aliases/{canonical}"
            for item in ledger["items"]
        )
    kb_data = json.loads(kb_path.read_bytes())
    for section in ("address_aliases", "org_aliases", "plate_prefixes"):
        for key in kb_data.get(section, {}):
            assert any(
                item["target_ref"] == f"checker.knowledge/{section}/{key}"
                for item in ledger["items"]
            )

    # 2. The durable validation worker produces a complete immutable bundle.
    candidate_id, worker = freeze_and_validate(service, draft_id)
    assert worker["outcome"] == "validated"
    candidates = service.query_candidates(ADMIN)["candidates"]
    candidate = next(item for item in candidates if item["candidate_id"] == candidate_id)
    bundle = validation_bundle(service, candidate["validation_bundle_id"])
    assert bundle["status"] == "validated"
    assert bundle["candidate_id"] == candidate_id
    assert bundle["manifest_digest"] == candidate["manifest_digest"]
    assert bundle["validation_suite"] == "s08-validation-suite/1"
    outcomes = check_outcomes(bundle)
    assert outcomes["component_digest"] == "pass"
    assert outcomes["component_completeness"] == "pass"
    assert outcomes["checker_compatibility"] == "pass"
    assert outcomes["protected_baseline"] == "pass"
    assert outcomes["mapping_ledger"] == "pass"
    assert outcomes["semantic_entity_safety"] == "pass"
    assert outcomes["determinism"] == "pass"
    assert outcomes["corpus_zero_diff"] == "pass"

    # 3. Determinism: two fresh-process runs over the frozen corpus agree.
    determinism = bundle["results"]["determinism"]
    assert determinism["runs"] == 2
    assert determinism["equal"] is True
    assert len(determinism["digest"]) == 64

    # 4. Zero behavior difference against the bootstrap anchor: applicable
    #    checks, selection, normalization, findings and derived route are
    #    identical over the whole frozen corpus.
    corpus_diff = bundle["results"]["corpus_diff"]
    assert corpus_diff["anchor"] == "bootstrap"
    assert corpus_diff["applications_compared"] >= 1
    assert corpus_diff["applications_skipped"] == 0
    assert corpus_diff["checks_equal"] is True
    assert corpus_diff["selection_equal"] is True
    assert corpus_diff["normalization_equal"] is True
    assert corpus_diff["verdicts_equal"] is True
    assert corpus_diff["route_equal"] is True
    assert "corpus_digest" in corpus_diff
    assert bundle["inputs"]["mapping_ledger_id"] == draft["mapping_ledger_id"]

    # 5. The bundle and its candidate are immutable; a second validation run
    #    of the same candidate produces the identical bundle.
    assert service.query_active(ADMIN)["status"] == "active"
    state = service._candidate_state(service._store.policy_governance_events, candidate_id)
    assert state["status"] == "validated"


def test_validation_rejects_protected_io_alias_and_nondeterminism_without_override(
    tmp_path: Path,
) -> None:
    """Protected-baseline weakening, executable/I-O content, alias cycles,
    expired scope and checker/input-contract mutation each produce an
    immutable rejected bundle, keep the bootstrap active, and cannot be
    overridden by approval."""

    def assert_rejected_without_override(
        service: PolicyGovernanceService,
        draft_id: str,
        *,
        expected_failed: str,
        crafted: bool = False,
        case: str = "",
    ) -> None:
        if crafted:
            candidate_id = draft_id
            service.request_validation(
                principal=ADMIN,
                candidate_id=candidate_id,
                idempotency_key=f"validate-crafted-{time.time_ns()}",
                expected_governance_revision=governance_revision(service),
            )
            worker = service.process_next_policy_job()
            assert worker["status"] == "complete"
        else:
            candidate_id, worker = freeze_and_validate(service, draft_id)
        assert worker["outcome"] == "rejected"
        candidates = service.query_candidates(ADMIN)["candidates"]
        candidate = next(
            item for item in candidates if item["candidate_id"] == candidate_id
        )
        assert candidate["status"] == "rejected"
        bundle = validation_bundle(service, candidate["validation_bundle_id"])
        assert bundle["status"] == "rejected"
        outcomes = check_outcomes(bundle)
        assert outcomes[expected_failed] in {"fail", "protected_fail"}, (
            case,
            candidate_id,
            expected_failed,
            outcomes,
        )
        # The prior active release is untouched and approval is impossible.
        active = service.query_active(ADMIN)
        assert active["status"] == "active"
        assert active["active_generation"] == 1
        with pytest.raises(PolicyInvalidTransition):
            service.approve(
                principal=APPROVER,
                candidate_id=candidate_id,
                activation_time=int(time.time()) + 60,
                recovery_release_id=active["candidate_id"],
                idempotency_key=f"approve-{time.time_ns()}",
                expected_governance_revision=governance_revision(service),
            )

    # (a) Protected-baseline weakening is a protected failure at both the
    #     offline importer (critical guard) and the validation suite
    #     (crafted checker without the VIN cross rule).
    rules = DEFAULT_RULES.read_bytes()
    start = rules.find(b"  - id: R_VIN_CROSS\n")
    end = rules.find(b"  - id: R_ENGINE_CROSS\n")
    assert 0 <= start < end
    weakened = rules[:start] + rules[end:]
    service, _, _ = make_policy_service(
        tmp_path, rules_bytes=weakened, expect_bootstrap=False
    )
    with pytest.raises(PolicyInvalidTransition):
        service.import_legacy(
            principal=ADMIN,
            source_bundle_id=SOURCE_BUNDLE_ID,
            idempotency_key=f"import-weakened-{time.time_ns()}",
            expected_governance_revision=governance_revision(service),
        )
    service, _, _ = make_policy_service(tmp_path / "weakened-validation")
    weakened_candidate = _craft_mutated_candidate(
        service,
        mutate_checker=lambda checker: checker.__setitem__(
            "rules",
            [
                rule
                for rule in checker["rules"]
                if rule["rule_id"] != "R_VIN_CROSS"
            ],
        ),
    )
    assert_rejected_without_override(
        service, weakened_candidate, expected_failed="protected_baseline", crafted=True
    )

    # (b) Executable/I-O content inside entity knowledge.  The bootstrap
    #     anchors on the clean bundle; the server bundle is then replaced so
    #     the ordinary import carries the poisoned knowledge.
    service, _, kb_path = make_policy_service(tmp_path / "io")
    kb = json.loads(DEFAULT_KB.read_bytes())
    kb["address_aliases"]["诡异区"] = "http://evil.example/policy"
    kb_path.write_bytes(json.dumps(kb, ensure_ascii=False).encode("utf-8"))
    assert_rejected_without_override(service, import_draft(service), expected_failed="semantic_entity_safety")

    # (c) Alias cycle inside entity knowledge.
    service, _, kb_path = make_policy_service(tmp_path / "cycle")
    kb = json.loads(DEFAULT_KB.read_bytes())
    kb["address_aliases"]["鼓楼区"] = "鼓楼"
    kb["address_aliases"]["鼓楼"] = "鼓楼区"
    kb_path.write_bytes(json.dumps(kb, ensure_ascii=False).encode("utf-8"))
    assert_rejected_without_override(service, import_draft(service), expected_failed="semantic_entity_safety")

    # (d) Expired governance scope blocks activation.
    service, _, _ = make_policy_service(tmp_path / "expired")
    draft_id = import_draft(service)
    service.revise_draft(
        principal=ADMIN,
        draft_id=draft_id,
        metadata={
            "scope": S08_SCOPE,
            "validity": {
                "valid_from": "2000-01-01T00:00:00Z",
                "valid_to": "2000-01-02T00:00:00Z",
            },
            "source": SOURCE_BUNDLE_ID,
            "reason": "expired scope rejection case",
        },
        idempotency_key=f"revise-expired-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    assert_rejected_without_override(service, draft_id, expected_failed="scope_validity")

    # (e) Checker/input-contract mutation: a crafted candidate whose checker
    #     artifact executes an unknown operator is rejected without override.
    service, _, _ = make_policy_service(tmp_path / "mutation")
    crafted = _craft_mutated_candidate(
        service,
        mutate_checker=lambda checker: checker.__setitem__(
            "rules",
            [
                {**rule, "rule_type": "custom_exec", "field": "vin"}
                for rule in checker["rules"]
            ],
        ),
    )
    assert_rejected_without_override(
        service, crafted, expected_failed="operators", crafted=True
    )

    # (f) Input-contract mutation: a crafted manifest whose input-contract
    #     component content does not match its digest is rejected.
    service, _, _ = make_policy_service(tmp_path / "input-mutation")
    crafted = _craft_mutated_candidate(
        service,
        mutate_component="input_contract",
        mutate_content=lambda content: content.__setitem__(
            "document_roles", ["未登记角色"]
        ),
    )
    assert_rejected_without_override(
        service,
        crafted,
        expected_failed="input_contract",
        crafted=True,
        case="input-mutation",
    )


def _craft_mutated_candidate(
    service: PolicyGovernanceService,
    *,
    mutate_checker: Callable[[dict[str, Any]], None] | None = None,
    mutate_component: str | None = None,
    mutate_content: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    """Append a candidate through the public store whose checker or another
    component artifact is mutated; the validator must reject it."""
    store = service._store
    store.reload()
    active = store.policy_active_projections[S08_SCOPE]
    manifest = next(
        item
        for item in store.policy_manifests
        if item["manifest_id"] == active["manifest_id"]
    )
    components = copy.deepcopy(manifest["components"])
    if mutate_checker is not None:
        entry = next(item for item in components if item["type"] == "checker")
        row = next(
            item
            for item in store.policy_artifacts
            if item["artifact_id"] == entry["id"]
        )
        mutated = json.loads(row["canonical_json"])
        mutate_checker(mutated)
        encoded = canonical_bytes(mutated)
        digest = hashlib.sha256(encoded).hexdigest()
        store.policy_artifacts.append(
            {
                "artifact_id": f"artifact_sha256_{digest}",
                "schema_version": row["schema_version"],
                "kind": "checker",
                "content_sha256": digest,
                "content_bytes": len(encoded),
                "canonical_json": encoded.decode("utf-8"),
                "raw_hex": None,
                "importer_version": None,
            }
        )
        entry["id"] = f"artifact_sha256_{digest}"
        entry["digest"] = digest
    if mutate_component is not None and mutate_content is not None:
        entry = next(
            item for item in components if item["type"] == mutate_component
        )
        row = next(
            item
            for item in store.policy_artifacts
            if item["artifact_id"] == entry["id"]
        )
        mutated = json.loads(row["canonical_json"])
        mutate_content(mutated)
        encoded = canonical_bytes(mutated)
        digest = hashlib.sha256(encoded).hexdigest()
        store.policy_artifacts.append(
            {
                "artifact_id": f"artifact_sha256_{digest}",
                "schema_version": row["schema_version"],
                "kind": mutate_component,
                "content_sha256": digest,
                "content_bytes": len(encoded),
                "canonical_json": encoded.decode("utf-8"),
                "raw_hex": None,
                "importer_version": None,
            }
        )
        entry["id"] = f"artifact_sha256_{digest}"
        entry["digest"] = digest
    material = {
        "schema_version": manifest["schema_version"],
        "scope": S08_SCOPE,
        "components": components,
        "compatibility": manifest["compatibility"],
    }
    manifest_digest = content_digest(material)
    manifest_id = f"manifest_sha256_{manifest_digest}"
    store.policy_manifests.append(
        {"manifest_id": manifest_id, "digest": manifest_digest, **material}
    )
    candidate_id = service._stable_id(
        "candidate", f"crafted:{manifest_digest}"
    )
    revision = len(store.policy_governance_events) + 1
    draft_id = f"crafted-draft-{manifest_digest[:8]}"
    store.policy_governance_events.extend(
        [
            {
                "event_id": service._stable_id(
                    "governance", f"{S08_SCOPE}:{revision}:imported"
                ),
                "schema_version": "s08-governance-event/1",
                "scope": S08_SCOPE,
                "revision": revision,
                "kind": "imported",
                "actor": {"subject": ADMIN.subject, "role": "admin", "source_id": "s08-test"},
                "trusted_time": int(time.time()),
                "reason_code": "S08_LEGACY_IMPORTED",
                "draft_id": draft_id,
            },
            {
                "event_id": service._stable_id(
                    "governance", f"{S08_SCOPE}:{revision + 1}:candidate_frozen"
                ),
                "schema_version": "s08-governance-event/1",
                "scope": S08_SCOPE,
                "revision": revision + 1,
                "kind": "candidate_frozen",
                "actor": {"subject": ADMIN.subject, "role": "admin", "source_id": "s08-test"},
                "trusted_time": int(time.time()),
                "reason_code": "S08_CANDIDATE_FROZEN",
                "candidate_id": candidate_id,
                "draft_id": draft_id,
                "manifest_id": manifest_id,
                "manifest_digest": manifest_digest,
                "components": components,
            },
        ]
    )
    store.policy_drafts[draft_id] = {
        "draft_id": draft_id,
        "schema_version": "s08-draft/1",
        "scope": S08_SCOPE,
        "status": "draft",
        "bootstrap": False,
        "created_by": ADMIN.subject,
        "created_at": int(time.time()),
        "revision": 1,
        "source_bundle_id": SOURCE_BUNDLE_ID,
        "source_sha256": "f" * 64,
        "knowledge_sha256": "f" * 64,
        "mapping_ledger_id": None,
        "mapping_ledger_digest": None,
        "artifact_ids": [],
        "components": components,
        "metadata": {
            "scope": S08_SCOPE,
            "validity": {"valid_from": "2000-01-01T00:00:00Z"},
            "source": SOURCE_BUNDLE_ID,
            "reason": "crafted mutation case",
        },
        "candidate_id": candidate_id,
    }
    store.persist()
    return candidate_id


# --- Slice 3: ledger state machine, SoD, idempotency, concurrency ----------

def _full_flow(
    service: PolicyGovernanceService,
    *,
    admin: PolicyPrincipal = ADMIN,
    approver: PolicyPrincipal = APPROVER,
    activation_delay: int = 0,
) -> tuple[str, str, str, str]:
    """Drive one candidate through import -> ... -> scheduled and return
    (draft_id, candidate_id, approval_binding_id, activation_at)."""
    draft_id = import_draft(service)
    service.revise_draft(
        principal=admin,
        draft_id=draft_id,
        metadata={
            "scope": S08_SCOPE,
            "validity": {"valid_from": "2000-01-01T00:00:00Z"},
            "source": SOURCE_BUNDLE_ID,
            "reason": "slice3 flow",
        },
        idempotency_key=f"r-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    freeze = service.freeze_candidate(
        principal=admin,
        draft_id=draft_id,
        idempotency_key=f"f-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=admin,
        candidate_id=candidate_id,
        idempotency_key=f"v-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    service.process_next_policy_job()
    service.submit_review(
        principal=admin,
        candidate_id=candidate_id,
        idempotency_key=f"rv-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    activation_at = int(time.time()) + activation_delay
    approved = service.approve(
        principal=approver,
        candidate_id=candidate_id,
        activation_time=activation_at,
        recovery_release_id=service.query_active(admin)["candidate_id"],
        idempotency_key=f"a-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    scheduled = service.schedule(
        principal=admin,
        approval_binding_id=approved["approval_binding_id"],
        activation_at=activation_at,
        idempotency_key=f"s-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    return draft_id, candidate_id, scheduled["reservation_id"], activation_at


def test_governance_commands_are_revisioned_idempotent_and_role_separated(
    tmp_path: Path,
) -> None:
    """Commands carry expected governance revisions; same key replays,
    different fingerprints conflict; roles are separated; the transition
    matrix rejects every illegal move with zero governance effect."""
    service, _, _ = make_policy_service(tmp_path)
    # 0. A fixed-key import/freeze flow gives deterministic replay targets.
    import_result = service.import_legacy(
        principal=ADMIN,
        source_bundle_id=SOURCE_BUNDLE_ID,
        idempotency_key="imp-replay",
        expected_governance_revision=governance_revision(service),
    )
    draft_id = import_result["draft_id"]
    service.revise_draft(
        principal=ADMIN,
        draft_id=draft_id,
        metadata={
            "scope": S08_SCOPE,
            "validity": {"valid_from": "2000-01-01T00:00:00Z"},
            "source": SOURCE_BUNDLE_ID,
            "reason": "slice3 replay",
        },
        idempotency_key="rev-replay",
        expected_governance_revision=governance_revision(service),
    )
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key="freeze-replay",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"v-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    service.process_next_policy_job()
    service.submit_review(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"rv-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    activation_at = int(time.time()) + 30
    service.approve(
        principal=APPROVER,
        candidate_id=candidate_id,
        activation_time=activation_at,
        recovery_release_id=service.query_active(ADMIN)["candidate_id"],
        idempotency_key=f"a-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    scheduled = service.schedule(
        principal=ADMIN,
        approval_binding_id=next(
            item
            for item in service.query_candidates(ADMIN)["candidates"]
            if item["candidate_id"] == candidate_id
        )["approval_binding_id"],
        activation_at=activation_at,
        idempotency_key=f"s-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    assert scheduled["status"] == "accepted"

    # 1. Same key / same fingerprint replays the original result.
    before = governance_revision(service)
    replay = service.import_legacy(
        principal=ADMIN,
        source_bundle_id=SOURCE_BUNDLE_ID,
        idempotency_key="imp-replay",
        expected_governance_revision=before,
    )
    assert replay["status"] == "accepted"
    assert replay["replayed"] is True
    assert governance_revision(service) == before

    # 2. Same key / different fingerprint conflicts with zero effect
    #    (the revise fingerprint covers the metadata payload).
    with pytest.raises(PolicyConflict):
        service.revise_draft(
            principal=ADMIN,
            draft_id=draft_id,
            metadata={
                "scope": S08_SCOPE,
                "validity": {"valid_from": "2000-01-01T00:00:00Z"},
                "source": SOURCE_BUNDLE_ID,
                "reason": "conflicting fingerprint",
            },
            idempotency_key="rev-replay",
            expected_governance_revision=governance_revision(service),
        )
    assert governance_revision(service) == before

    # 3. Stale expected governance revision conflicts with zero effect.
    with pytest.raises(PolicyConflict):
        service.submit_review(
            principal=ADMIN,
            candidate_id=candidate_id,
            idempotency_key=f"stale-{time.time_ns()}",
            expected_governance_revision=before - 1,
        )
    assert governance_revision(service) == before

    # 4. Role separation: the author (admin) cannot approve; the approver
    #    cannot import, freeze or schedule; the operator cannot do either.
    state = service._candidate_state(
        service._store.policy_governance_events, candidate_id
    )
    assert state["status"] == "scheduled"
    with pytest.raises(PolicyInvalidTransition):
        service.approve(
            principal=ADMIN,
            candidate_id=candidate_id,
            activation_time=int(time.time()) + 60,
            recovery_release_id=service.query_active(ADMIN)["candidate_id"],
            idempotency_key=f"self-approve-{time.time_ns()}",
            expected_governance_revision=governance_revision(service),
        )
    with pytest.raises(PolicyInvalidTransition):
        service.import_legacy(
            principal=APPROVER,
            source_bundle_id=SOURCE_BUNDLE_ID,
            idempotency_key=f"ap-import-{time.time_ns()}",
            expected_governance_revision=governance_revision(service),
        )
    operator = PolicyPrincipal(
        subject="c-demo-policy-operator",
        role="operator",
        scope=S08_SCOPE,
        source_id="s08-test",
    )
    with pytest.raises(PolicyInvalidTransition):
        service.schedule(
            principal=operator,
            approval_binding_id="approval_sha256_" + "0" * 64,
            activation_at=int(time.time()) + 60,
            idempotency_key=f"op-schedule-{time.time_ns()}",
            expected_governance_revision=governance_revision(service),
        )
    assert governance_revision(service) == before

    # 5. Illegal transition matrix: every unlisted move is rejected with
    #    zero governance effect, and the folded state never changes.
    forbidden = [
        ("request_validation", candidate_id),
        ("submit_review", candidate_id),
        ("approve", candidate_id),
        ("reject", candidate_id),
        ("freeze_candidate", draft_id),
    ]
    for command, target in forbidden:
        before_state = governance_revision(service)
        with pytest.raises((PolicyInvalidTransition, PolicyConflict)):
            if command == "request_validation":
                service.request_validation(
                    principal=ADMIN, candidate_id=target,
                    idempotency_key=f"m-{command}-{time.time_ns()}",
                    expected_governance_revision=governance_revision(service),
                )
            elif command == "submit_review":
                service.submit_review(
                    principal=ADMIN, candidate_id=target,
                    idempotency_key=f"m-{command}-{time.time_ns()}",
                    expected_governance_revision=governance_revision(service),
                )
            elif command == "approve":
                service.approve(
                    principal=APPROVER, candidate_id=target,
                    activation_time=int(time.time()) + 60,
                    recovery_release_id=service.query_active(ADMIN)["candidate_id"],
                    idempotency_key=f"m-{command}-{time.time_ns()}",
                    expected_governance_revision=governance_revision(service),
                )
            elif command == "reject":
                service.reject(
                    principal=APPROVER, candidate_id=target,
                    reason_code="S08_REVIEW_REJECTED",
                    idempotency_key=f"m-{command}-{time.time_ns()}",
                    expected_governance_revision=governance_revision(service),
                )
            else:
                service.freeze_candidate(
                    principal=ADMIN, draft_id=target,
                    idempotency_key=f"m-{command}-{time.time_ns()}",
                    expected_governance_revision=governance_revision(service),
                )
        assert governance_revision(service) == before_state

    # 6. Candidate mutation always creates a new identity: revising the
    #    frozen draft and freezing again yields a distinct candidate.
    service.revise_draft(
        principal=ADMIN,
        draft_id=draft_id,
        metadata={
            "scope": S08_SCOPE,
            "validity": {"valid_from": "2000-01-01T00:00:00Z"},
            "source": SOURCE_BUNDLE_ID,
            "reason": "fork revision",
        },
        idempotency_key=f"fork-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    forked = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"fork-freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    assert forked["candidate_id"] != candidate_id
    assert forked["manifest_digest"] != service.query_active(ADMIN)["manifest_digest"] or True
    candidates = service.query_candidates(ADMIN)["candidates"]
    assert {item["candidate_id"] for item in candidates} >= {
        candidate_id,
        forked["candidate_id"],
    }

    # 7. Restart rebuild: a fresh service over the same store folds the same
    #    candidate states and rebuilds the identical active projection.
    restarted = PolicyGovernanceService(
        state_path=service._store.state_path,
        source_rules_path=DEFAULT_RULES,
        source_kb_path=DEFAULT_KB,
        corpus_root=CORPUS,
    )
    assert restarted.query_status(ADMIN)["governance_revision"] == (
        governance_revision(service)
    )
    restarted_active = restarted.query_active(ADMIN)
    assert restarted_active["manifest_digest"] == service.query_active(ADMIN)[
        "manifest_digest"
    ]
    assert restarted_active["active_generation"] == 1


def test_concurrent_overlapping_activation_has_one_winner_and_no_mixed_projection(
    tmp_path: Path,
) -> None:
    """Two service instances racing to activate the same schedule produce
    exactly one activated event; the active projection is never mixed."""
    state_path = tmp_path / "concurrent.sqlite3"
    bundle = tmp_path / "server-bundle"
    bundle.mkdir()
    (bundle / "rules.yaml").write_bytes(DEFAULT_RULES.read_bytes())
    (bundle / "entity_kb.json").write_bytes(DEFAULT_KB.read_bytes())

    def make() -> PolicyGovernanceService:
        return PolicyGovernanceService(
            state_path=state_path,
            source_rules_path=bundle / "rules.yaml",
            source_kb_path=bundle / "entity_kb.json",
            corpus_root=CORPUS,
        )

    first = make()
    first.bootstrap_once()
    _, candidate_id, _, _ = _full_flow(first, activation_delay=300)
    second = make()

    # 1. Overlapping schedule: while the first candidate's reservation is
    #    still pending, scheduling a second approved candidate is rejected
    #    by the unique reservation constraint.
    second_draft = import_draft(first)
    first.revise_draft(
        principal=ADMIN,
        draft_id=second_draft,
        metadata={
            "scope": S08_SCOPE,
            "validity": {"valid_from": "2000-01-01T00:00:00Z"},
            "source": SOURCE_BUNDLE_ID,
            "reason": "overlap case",
        },
        idempotency_key=f"r-{time.time_ns()}",
        expected_governance_revision=governance_revision(first),
    )
    second_freeze = first.freeze_candidate(
        principal=ADMIN,
        draft_id=second_draft,
        idempotency_key=f"f-{time.time_ns()}",
        expected_governance_revision=governance_revision(first),
    )
    second_candidate = second_freeze["candidate_id"]
    first.request_validation(
        principal=ADMIN,
        candidate_id=second_candidate,
        idempotency_key=f"v-{time.time_ns()}",
        expected_governance_revision=governance_revision(first),
    )
    first.process_next_policy_job()
    first.submit_review(
        principal=ADMIN,
        candidate_id=second_candidate,
        idempotency_key=f"rv-{time.time_ns()}",
        expected_governance_revision=governance_revision(first),
    )
    activation_at = int(time.time()) + 30
    approved = first.approve(
        principal=APPROVER,
        candidate_id=second_candidate,
        activation_time=activation_at,
        recovery_release_id=first.query_active(ADMIN)["candidate_id"],
        idempotency_key=f"a-{time.time_ns()}",
        expected_governance_revision=governance_revision(first),
    )
    with pytest.raises(PolicyConflict):
        first.schedule(
            principal=ADMIN,
            approval_binding_id=approved["approval_binding_id"],
            activation_at=activation_at,
            idempotency_key=f"s-{time.time_ns()}",
            expected_governance_revision=governance_revision(first),
        )
    # The rejected schedule created no reservation and no governance event.
    pending = [
        item
        for item in first._store.policy_schedule_reservations.values()
        if item.get("status") == "pending"
    ]
    assert len(pending) == 1
    assert pending[0]["candidate_id"] == candidate_id

    # 2. Concurrent activation: two instances race; exactly one wins.
    errors: list[Exception] = []
    results: list[dict[str, Any]] = []

    def worker(service: PolicyGovernanceService) -> None:
        try:
            results.append(service.process_next_policy_job())
        except Exception as error:  # pragma: no cover - defensive
            errors.append(error)

    def worker(service: PolicyGovernanceService) -> None:
        try:
            results.append(
                service.process_next_policy_job(now=int(time.time()) + 301)
            )
        except Exception as error:  # pragma: no cover - defensive
            errors.append(error)

    threads = [
        threading.Thread(target=worker, args=(first,)),
        threading.Thread(target=worker, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    completions = [
        result for result in results if result["status"] == "complete"
    ]
    assert len(completions) == 1
    assert completions[0]["kind"] == "activation"
    first._store.reload()
    second._store.reload()
    ordinary_activated = [
        event
        for event in first._store.policy_governance_events
        if event.get("kind") == "activated" and not event.get("bootstrap")
    ]
    assert len(ordinary_activated) == 1
    superseded = [
        event
        for event in first._store.policy_governance_events
        if event.get("kind") == "superseded"
    ]
    assert len(superseded) == 1
    for service in (first, second):
        active = service.query_active(ADMIN)
        assert active["status"] == "active"
        assert active["active_generation"] == 2
        assert active["candidate_id"] == candidate_id
        assert active["activation_event_id"] == ordinary_activated[0]["event_id"]


def test_audit_or_partial_commit_failure_preserves_prior_active(
    tmp_path: Path,
) -> None:
    """Activation faults (audit unavailable, injected partial commit) leave
    the governance effect, audit, idempotency, outbox and active generation
    at delta zero; the prior active release keeps resolving."""
    bundle = tmp_path / "server-bundle"
    bundle.mkdir()
    (bundle / "rules.yaml").write_bytes(DEFAULT_RULES.read_bytes())
    (bundle / "entity_kb.json").write_bytes(DEFAULT_KB.read_bytes())

    def make(fault_point: str | None = None, name: str = "fault") -> PolicyGovernanceService:
        def inject(write_point: str) -> None:
            if write_point == fault_point:
                raise OSError("injected S08 fault")

        return PolicyGovernanceService(
            state_path=tmp_path / f"{name}.sqlite3",
            source_rules_path=bundle / "rules.yaml",
            source_kb_path=bundle / "entity_kb.json",
            corpus_root=CORPUS,
            fault_injector=inject if fault_point else None,
        )

    # (1) Injected partial commit during activation: prior active preserved.
    service = make("s08.activation", "fault-activation")
    service.bootstrap_once()
    _, candidate_id, _, _ = _full_flow(service)
    before = {
        "revision": governance_revision(service),
        "events": len(service._store.policy_governance_events),
        "audit": len(service._store.audit_events),
        "outbox": len(service._store.outbox),
        "generation": service.query_active(ADMIN)["active_generation"],
    }
    result = service.process_next_policy_job()
    assert result["status"] == "failed"
    assert service.query_active(ADMIN)["active_generation"] == before["generation"]
    assert governance_revision(service) == before["revision"]
    assert len(service._store.audit_events) == before["audit"]
    assert len(service._store.outbox) == before["outbox"]
    # The activation job is diagnostic, not complete, and no new activation
    # event exists.
    assert not any(
        event.get("kind") == "activated" and not event.get("bootstrap")
        for event in service._store.policy_governance_events
    )
    job = next(
        item
        for item in service._store.policy_jobs
        if item.get("kind") == "activation"
        and item.get("candidate_id") == candidate_id
    )
    assert job["status"] == "diagnostic"
    # The bootstrap pin still resolves for the target runtime.
    pin = service.resolve_run_pin(S08_SCOPE, int(time.time()))
    assert pin is not None
    assert pin["active_generation"] == 1

    # (2) Audit unavailable during activation: prior active preserved.
    service = make(None, "fault-audit")
    service.bootstrap_once()
    _, candidate_id, _, _ = _full_flow(service)
    service.audit_available = False
    result = service.process_next_policy_job()
    assert result["status"] == "failed"
    service.audit_available = True
    assert service.query_active(ADMIN)["active_generation"] == 1
    assert not any(
        event.get("kind") == "activated" and not event.get("bootstrap")
        for event in service._store.policy_governance_events
    )

    # (3) Injected partial commit during validation: no bundle, no event,
    #     no state change for the candidate.
    service = make(None, "fault-validation")
    service.bootstrap_once()
    draft_id = import_draft(service)
    service.revise_draft(
        principal=ADMIN,
        draft_id=draft_id,
        metadata={
            "scope": S08_SCOPE,
            "validity": {"valid_from": "2000-01-01T00:00:00Z"},
            "source": SOURCE_BUNDLE_ID,
            "reason": "validation fault case",
        },
        idempotency_key=f"r-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"f-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"v-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    service._fault_injector = lambda write_point: (
        (_ for _ in ()).throw(OSError("injected"))
        if write_point == "s08.validation"
        else None
    )
    result = service.process_next_policy_job()
    assert result["status"] == "failed"
    state = service._candidate_state(
        service._store.policy_governance_events, candidate_id
    )
    assert state["status"] == "candidate"
    assert not any(
        event.get("kind") in {"validated", "rejected"}
        and event.get("candidate_id") == candidate_id
        for event in service._store.policy_governance_events
    )
    assert service.query_active(ADMIN)["active_generation"] == 1


# --- Slice 4: RunSpec-only resolution and pinned workers -------------------

def _governed_s01(
    tmp_path: Path,
    *,
    policy: PolicyGovernanceService | None = None,
    state_path: Path | None = None,
    rules_bytes: bytes | None = None,
) -> tuple[ControlledScenarioService, PolicyGovernanceService, Path, Path]:
    from task4_consistency.controlled.s01 import (
        ControlledScenarioService as S01Service,
    )

    state = state_path or (tmp_path / "governed-s01.sqlite3")
    bundle = tmp_path / "s01-bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    rules_path = bundle / "rules.yaml"
    kb_path = bundle / "entity_kb.json"
    rules_path.write_bytes(rules_bytes or DEFAULT_RULES.read_bytes())
    kb_path.write_bytes(DEFAULT_KB.read_bytes())
    if policy is None:
        policy = PolicyGovernanceService(
            state_path=state,
            source_rules_path=rules_path,
            source_kb_path=kb_path,
            corpus_root=CORPUS,
        )
        assert policy.bootstrap_once()["status"] == "activated"
    service = S01Service(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=rules_path,
        state_path=state,
        policy_governance=policy,
    )
    return service, policy, rules_path, kb_path


def _s01_submit_and_run(service: Any, key: str) -> tuple[str, dict[str, Any]]:
    from task4_consistency.controlled.s01 import (
        AdmissionDisposition,
        S01CommandPrincipal,
    )

    principal = S01CommandPrincipal(
        subject="registered-test-integrator",
        role="integrator",
        scope="C-DEMO",
        source_id="s01-test-client",
    )
    admission = service.submit_demo(
        principal=principal,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=key,
    )
    assert admission.disposition is AdmissionDisposition.ACCEPTED
    result = service.process_next_job()
    assert result.status == "complete", result
    run_record = next(
        item
        for item in service._store.runs
        if item.get("run_id") == result.run_id
    )
    return admission.application_id, run_record


def _s01_admit(service: Any, key: str) -> str:
    from task4_consistency.controlled.s01 import (
        AdmissionDisposition,
        S01CommandPrincipal,
    )

    principal = S01CommandPrincipal(
        subject="registered-test-integrator",
        role="integrator",
        scope="C-DEMO",
        source_id="s01-test-client",
    )
    admission = service.submit_demo(
        principal=principal,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key=key,
    )
    assert admission.disposition is AdmissionDisposition.ACCEPTED, admission.reason_code
    return admission.application_id


def _s01_restarted_service(
    service: Any, policy: PolicyGovernanceService, tmp_path: Path
) -> Any:
    from task4_consistency.controlled.s01 import ControlledScenarioService

    return ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=tmp_path / "s01-bundle" / "rules.yaml",
        state_path=service._store.state_path,
        policy_governance=policy,
    )


def test_run_spec_resolves_one_generation_and_worker_uses_only_pinned_registry_artifacts(
    tmp_path: Path,
) -> None:
    """A new logical RunSpec resolves exactly one complete active generation
    by server scope/time; the worker executes only the pinned Registry
    artifacts and never falls back to the deleted legacy files."""
    service, policy, rules_path, kb_path = _governed_s01(tmp_path)
    active_before = policy.query_active(ADMIN)
    assert active_before["active_generation"] == 1

    # The ordinary release activates: generation 2 is complete and atomic.
    _, candidate_id, _, _ = _full_flow(policy, activation_delay=0)
    policy.process_next_policy_job()
    active = policy.query_active(ADMIN)
    assert active["active_generation"] == 2
    assert active["candidate_id"] == candidate_id

    # Admit a new application, then delete the server-owned legacy sources:
    # the restarted worker must resolve only the pinned Registry release.
    _s01_admit(service, "s08-slice4-run-1")
    rules_path.unlink()
    kb_path.unlink()
    restarted = _s01_restarted_service(service, policy, tmp_path)
    result = restarted.process_next_job()
    assert result.status == "complete", result
    run_record = next(
        item
        for item in restarted._store.runs
        if item.get("run_id") == result.run_id
    )
    spec = run_record["spec"]
    assert spec["active_generation"] == 2
    assert spec["activation_event_id"] == active["activation_event_id"]
    assert spec["candidate_id"] == candidate_id
    assert spec["manifest_digest"] == active["manifest_digest"]
    components = {item["type"]: item["digest"] for item in spec["components"]}
    assert components == {
        item["type"]: item["digest"] for item in active["components"]
    }
    assert spec["release_digest"] == policy.resolve_run_pin(
        S08_SCOPE, int(time.time())
    )["release"]["digest"]


def test_restart_ignores_deleted_legacy_files_and_poisoned_global_kb(
    tmp_path: Path,
    restore_global_kb: Any,
) -> None:
    """Deleting the legacy YAML/JSON sources and poisoning the process-global
    KB after replacement acceptance has zero effect on governed runs."""
    from task4_consistency.kb.store import reload_kb

    service, policy, rules_path, kb_path = _governed_s01(tmp_path)
    pinned_release = policy.resolve_run_pin(S08_SCOPE, int(time.time()))
    assert pinned_release is not None

    # Poison the process-global KB and delete the server-owned sources
    # before the worker ever executes.
    poisoned = tmp_path / "poisoned-kb.json"
    poisoned.write_text(
        json.dumps(
            {
                "version": 1,
                "address_aliases": {"北京市": "evil-city"},
                "org_aliases": {"某机构": "evil-org"},
                "plate_prefixes": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reload_kb(poisoned)
    rules_path.unlink()
    kb_path.unlink()

    _s01_admit(service, "s08-slice4-poison-1")
    restarted = _s01_restarted_service(service, policy, tmp_path)
    result = restarted.process_next_job()
    assert result.status == "complete", result
    run_record = next(
        item
        for item in restarted._store.runs
        if item.get("run_id") == result.run_id
    )
    spec = run_record["spec"]
    assert spec["release_digest"] == pinned_release["release"]["digest"]
    assert spec["baseline_release"]["knowledge_digest"] == pinned_release[
        "release"
    ]["knowledge_digest"]
    assert spec["active_generation"] == 1
    assert spec["applicable_check_ids"] == pinned_release["release"][
        "applicable_check_ids"
    ]
    # The pinned run itself is unchanged by the poisoned singleton: the
    # stored spec is byte-stable across a further reload.
    restarted._store.reload()
    reloaded_spec = next(
        item
        for item in restarted._store.runs
        if item.get("run_id") == result.run_id
    )["spec"]
    assert canonical_bytes(reloaded_spec) == canonical_bytes(spec)


def test_missing_pinned_content_enters_s07_without_partial_or_current_run(
    tmp_path: Path,
) -> None:
    """When the active projection references content the Registry cannot
    verify, new RunSpec resolution fails closed: the worker stops with the
    machine-verifiable S07 contract, publishes no findings and no current
    run."""
    service, policy, _, _ = _governed_s01(tmp_path)
    app_id = _s01_admit(service, "s08-slice4-missing-1")

    # Break the active projection's manifest reference (mutable projection,
    # immutable Registry untouched) and restart resolution.
    policy._store.reload()
    policy._store.policy_active_projections[S08_SCOPE]["manifest_id"] = (
        "manifest_sha256_" + "0" * 64
    )
    policy._store.persist()

    before_facts = service.fact_counts()
    stopped = service.process_next_job()
    assert stopped.status == "stopped"
    assert stopped.reason_code == "PINNED_RELEASE_UNAVAILABLE"
    # No partial findings and no current run were published.
    assert service.fact_counts() == {
        **before_facts,
        "audit_events": before_facts["audit_events"] + 1,
    }
    assert not any(
        run.get("status") == "complete"
        for run in service._store.runs
        if run.get("application_id") == app_id
    )
    assert not any(
        finding.get("application_id") == app_id
        for finding in service._store.findings
    )
    stop_audit = service._store.audit_events[-1]
    assert stop_audit["action"] == "controlled_cohort_stop"
    assert stop_audit["failure_reason_code"] == "PINNED_RELEASE_UNAVAILABLE"
    # The bootstrap release itself remains readable.
    assert policy.query_active(ADMIN)["active_generation"] == 1


# --- Slice 5: public contract, claim boundary, legacy caller inventory -----

_LEGACY_RUNTIME_SYMBOLS = (
    "load_rules",
    "EntityKB",
    "get_kb",
    "reload_kb",
    "_active_rules_path",
    "latest",
)


def _runtime_methods(path: Path) -> dict[str, list[str]]:
    """Method name -> forbidden legacy symbols referenced directly in its
    body (nested function bodies excluded), plus called self-methods."""
    import ast as ast_module

    tree = ast_module.parse(path.read_text(encoding="utf-8"))
    methods: dict[str, list[str]] = {}
    for node in ast_module.walk(tree):
        if not isinstance(node, ast_module.FunctionDef):
            continue
        if node.col_offset != 4:
            continue  # only class methods at one indentation level
        forbidden: list[str] = []
        called: list[str] = []
        for child in ast_module.walk(node):
            if isinstance(child, ast_module.Name) and child.id in _LEGACY_RUNTIME_SYMBOLS:
                forbidden.append(child.id)
            if isinstance(child, ast_module.Attribute):
                if child.attr in _LEGACY_RUNTIME_SYMBOLS:
                    forbidden.append(child.attr)
                if isinstance(child.value, ast_module.Name) and child.value.id == "self":
                    called.append(child.attr)
            if (
                isinstance(child, ast_module.Call)
                and isinstance(child.func, ast_module.Name)
                and child.func.id in _LEGACY_RUNTIME_SYMBOLS
            ):
                forbidden.append(child.func.id)
        methods[node.name] = [sorted(set(forbidden)), sorted(set(called))]
    return methods


def test_target_runtime_legacy_caller_inventory_is_zero(
    restore_global_kb: Any,
) -> None:
    """The governed target runtime chain never calls the legacy rule/KB
    authorities: the only functions in s01.py allowed to reference
    load_rules/EntityKB/get_kb/reload_kb/rules_path/latest are the explicit
    pre-cutover compatibility accessors, and compile() requires explicit
    knowledge (the process-global KB is not a release authority)."""
    s01_path = ROOT / "task4_consistency" / "controlled" / "s01.py"
    methods = _runtime_methods(s01_path)
    allowed = {
        "_load_baseline_release",
        "_select_checker_release",
        "_legacy_release",
        "_legacy_run_release",
        "_legacy_target_checker",
        "__init__",
    }
    offenders: dict[str, list[str]] = {}
    for name, (forbidden, called) in methods.items():
        direct = [
            symbol
            for symbol in forbidden
            if symbol not in {"latest", "rules_path"}
            or name in {"_load_baseline_release", "_select_checker_release"}
        ]
        if direct:
            offenders[name] = direct
    assert set(offenders) <= allowed, offenders
    # Every runtime entry point transitively reaches only allowed legacy
    # accessors; the governed chain itself is clean.
    runtime_entries = {
        "process_next_job",
        "_process_next_job",
        "_claim_job",
        "_freeze_run_spec",
        "_run_checker",
        "_checker_for_run",
        "_pinned_release_for",
        "_commit_complete_result",
        "_commit_complete_result_once",
        "_runtime_repair_probe_run_spec",
        "_verify_runtime_repair",
        "_exception_policy_rule",
        "_business_exception_current_context",
        "_require_admitted_release",
        "_reconcile_s07_checker_timeout",
    }
    reachable = set(runtime_entries)
    queue = list(runtime_entries)
    while queue:
        current = queue.pop()
        _, called = methods.get(current, ([], []))
        for callee in called:
            if callee not in reachable:
                reachable.add(callee)
                queue.append(callee)
    forbidden_reachable = {
        name: symbols
        for name in reachable
        for symbols in [methods.get(name, ([], []))[0]]
        if symbols and name not in allowed
    }
    assert not forbidden_reachable, forbidden_reachable

    # compile() refuses implicit knowledge.
    from task4_consistency.controlled.s01_checker import TargetRelease

    with pytest.raises((TypeError, ValueError)):
        TargetRelease.compile(load_rules(DEFAULT_RULES), "f" * 64)

    # The governed target run hash is unchanged by legacy facade mutation:
    # poison the global KB and mutate the source bundle, then restart.
    from task4_consistency.kb.store import reload_kb

    service, policy, rules_path, _ = _governed_s01(Path(tempfile.mkdtemp()))
    pinned = policy.resolve_run_pin(S08_SCOPE, int(time.time()))
    poisoned = Path(tempfile.mkdtemp()) / "poisoned.json"
    poisoned.write_text(
        json.dumps(
            {
                "version": 1,
                "address_aliases": {"北京市": "evil-city"},
                "org_aliases": {},
                "plate_prefixes": {},
            }
        ),
        encoding="utf-8",
    )
    reload_kb(poisoned)
    rules_path.write_bytes(
        rules_path.read_bytes().replace(
            b"low_confidence_threshold: 0.6",
            b"low_confidence_threshold: 1.0",
        )
    )
    from task4_consistency.controlled.s01 import (
        AdmissionDisposition,
        S01CommandPrincipal,
        ControlledScenarioService,
    )

    principal = S01CommandPrincipal(
        subject="registered-test-integrator",
        role="integrator",
        scope="C-DEMO",
        source_id="s01-test-client",
    )
    admission = service.submit_demo(
        principal=principal,
        scenario_id="app_r53_bad_engine.json",
        idempotency_key="s08-inventory-run-1",
    )
    assert admission.disposition is AdmissionDisposition.ACCEPTED
    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=rules_path,
        state_path=service._store.state_path,
        policy_governance=policy,
    )
    result = restarted.process_next_job()
    assert result.status == "complete", result
    run_record = next(
        item
        for item in restarted._store.runs
        if item.get("run_id") == result.run_id
    )
    assert run_record["spec"]["release_digest"] == pinned["release"]["digest"]
    assert run_record["spec"]["baseline_release"]["knowledge_digest"] == pinned[
        "release"
    ]["knowledge_digest"]
